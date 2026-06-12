# Crucible Game-Mode Rootfs Implementation Plan

**Goal:** Real GPU workload measurement in the guest: a native OSS benchmark
(vkmark/glmark2) runs under MangoHud in a Mesa/Vulkan-capable rootfs, frame
timing flows back to the host profiler over vsock, and the full
SelectGame → … → Evaluate pipeline produces non-zero fps metrics.

**Phasing rationale:** Steam/Wine/Proton is deliberately deferred. The
GPU-native benchmark phase closes the genuinely novel risk — VFIO
passthrough, a real GPU rendering in the guest, MangoHud frame timing
crossing the vsock boundary — while staying license-free, scriptable, and
compositor-free (vkmark/glmark2 render via DRM/KMS; MangoHud hooks the
Vulkan layer / LD_PRELOAD headlessly).

**Plan series:**
- Plans 1–4: foundation, VM management, core agents, orchestration loop (complete)
- **Plan 5 (this plan):** game-mode rootfs (G0 protocol/transport, G1 rootfs, G2 VFIO)
- Plan 6 (future): Steam + Proton + gamescope (`CRUCIBLE_E2E_GAME`)

---

## Completed tasks

- [x] **G0.1 — byte-returning fetch_file.** The old handler returned only the
  file size; game mode produces the MangoHud CSV *inside* the guest, so
  `_handle_fetch_file` now returns `contents_b64` + `truncated` (8 MiB cap,
  `FETCH_FILE_MAX_BYTES`). Wire-contract test on the Rust side.
- [x] **G0.2 — `LaunchBenchmark` RPC.** `GuestCommand::LaunchBenchmark
  { name, args, mangohud_output }`; guest handler allow-lists
  `NATIVE_BENCHMARKS = (vkmark, glmark2)`, runs under
  `MANGOHUD_CONFIG=output_folder=…,autostart_log=1,log_duration=0,no_display`,
  and renames the newest generated CSV to the deterministic
  `mangohud_output` path (MangoHud has no fixed-output-file option). PSI
  deltas mirror `_handle_run_benchmark`.
- [x] **G0.3 — profiler game tools.** `agents/profiler/game_tools.py`:
  `launch_benchmark` + `fetch_mangohud_log` (base64-decode → existing
  `parse_mangohud_csv`). Parser fixes: skip MangoHud's two system-info
  header rows; nearest-rank percentiles (the floor-based index hid the
  worst frame in small samples). Prescriptive game branch in
  `ProfilerAgent.build_user_message`.
- [x] **G0.4 — config + threading.** `[measurement] game_benchmark`
  (default `vkmark`); shared `measurement_context()` builds both baseline
  and comparison profiler contexts and adds
  `game_benchmark`/`mangohud_output` in game mode
  (`GUEST_MANGOHUD_OUTPUT = /tmp/crucible_mangohud.csv`).
- [x] **G0.5 — game_selector native benchmarks.** `list_native_benchmarks`
  tool + prompt pivot when `workload_kind == "game"` (no Steam library in
  the guest); orchestrator threads measurement mode into the selector
  context.
- [x] **G1.1 — game rootfs script.** `scripts/setup-game-rootfs.sh`:
  Debian trixie (Mesa 25.x for RDNA3; bookworm's 22.x predates gfx1100),
  `mesa-vulkan-drivers vulkan-tools vkmark glmark2 glmark2-drm mangohud
  firmware-amd-graphics` (non-free-firmware). Shared logic extracted to
  `scripts/lib/rootfs-common.sh`; `setup-rootfs.sh` behavior unchanged.
- [x] **G2.1 — VFIO fail-fast.** `VmManager::validate_passthrough()` reads
  the sysfs driver symlink before QEMU spawns (a GPU still on amdgpu hangs
  boot with no diagnostic); injected-reader unit tests.
  `scripts/setup-host.sh`: IOMMU/group prechecks, prints driver_override
  bind commands by default, `--bind` executes after confirmation.
- [x] **e2e gate.** `gpu_game_cycle_produces_nonzero_fps` behind
  `CRUCIBLE_E2E_GPU=1` — same pipeline with `mode = "game"`, asserts
  non-zero `fps_avg` (synthetic emits 0, so that's the discriminator) and
  runs the tool-leak scan. Game rootfs via `CRUCIBLE_GAME_ROOTFS_PATH`
  (stamp `.crucible-game-built`); `CRUCIBLE_VFIO_DEVICE` optional —
  without it lavapipe renders in software, which still exercises the path.

## Remaining verification (hardware checkpoints)

- [x] Build the game rootfs: `scripts/setup-game-rootfs.sh` (sudo + network).
  Done 2026-06-12.
- [x] Boot check: RADV reports "AMD Radeon RX 7900 XT (RADV NAVI31)" with
  VFIO passthrough; `vkmark --list-scenes` works. (llvmpipe before bind.)
- [ ] ~~Software-render e2e~~ — invalidated: vkmark cannot present without a
  DRM device, and the test kernel exposes none without a passed-through
  GPU. The "lavapipe still exercises the path" assumption was wrong (see
  hardware findings below). The GPU e2e is the only meaningful gate.
- [x] VFIO checkpoint: monitor moved to iGPU output, display-manager
  restart, `setup-host.sh` bind of **all four** GPU functions, GPU e2e on
  real RDNA3. vkmark scores 14k (kms) / 40k (headless) FPS in-guest;
  MangoHud CSV produced and parsed. Done 2026-06-12.
- [x] Synthetic regression: `CRUCIBLE_E2E=1 cargo test --test e2e` green
  (298s, 2026-06-12) — needs `.venv/bin` on PATH since the Agent SDK moved
  into the uv venv.

## Hardware findings (2026-06-12, first real-GPU run)

Chain of failures found and fixed getting the first frames out:

- **7900 XT is a 4-function PCI device** (VGA 03:00.0 + HDMI audio .1 +
  USB .2 + UCSI .3), each alone in its own IOMMU group. QEMU's bus reset
  needs *all* of them on vfio-pci: "Cannot reset device, depends on group
  N which is not owned". `setup-host.sh` now walks the whole slot;
  `VmManager` discovers siblings and validates each.
- **QEMU hangs forever reading the GPU option ROM** of a card the host
  drove earlier (live-unbound amdgpu). `rombar=0` on the VGA function
  fixes it; vm.rs now always sets it.
- **Unprivileged QEMU needs `/dev/vfio/<group>` ownership and memlock ≥
  guest RAM** (8 MB default → `vfio_container_dma_map = -12`).
  `setup-host.sh --bind` now chowns the nodes and prints the prlimit hint.
- **The test kernel needs `CONFIG_DRM_AMDGPU=m`** and the modules must be
  visible in the guest: vng with `--root` resolves modules only from
  inside the rootfs, ignoring `.virtme_mods`. `KernelBuilder` now runs
  `make modules_install INSTALL_MOD_PATH=.virtme_mods`; `VmManager`
  overlays it via `--rodir /lib/modules/<kver>=…`.
- **vkmark's default kms winsys is invisible to MangoHud**: it presents
  via raw DRM atomic commits and never creates a VkSwapchainKHR, so the
  layer's QueuePresentKHR hook records zero frames. The packaged vkmark
  ships an undocumented `headless` winsys (VK_EXT_headless_surface) that
  creates a real swapchain — the guest now forces `--winsys headless`.
- **MangoHud `log_duration=0` never writes the CSV** (the file is only
  flushed when logging *stops* while the app is alive) and **`no_display`
  suppresses the HUD update loop that feeds the logger**. The guest now
  derives a finite window from `duration_secs` (new LaunchBenchmark field)
  and leaves the HUD enabled.
- **vng's qemu grandchild survives `kill_on_drop`** (vng wraps virtme-run
  in `sh -c`); a leaked QEMU held vsock CID 3 across runs. vm.rs now
  spawns into a dedicated process group and shutdown kills the group.
- Profiler prompt now forbids fabricating metrics: tool errors or
  `log_found=false` must produce `{"error": …}`, not zeros — a zero
  `fps_avg` masked the entire VFIO failure as a "successful" cycle.

## Notes / decisions

- vng already passes `-display none -vga none` to QEMU
  (virtme/architectures.py); adding our own `-display none` would be a
  duplicate-option error. Headless is the default.
- Firmware/kernel skew risk: trixie's `firmware-amd-graphics` must satisfy
  the kernel-under-test's amdgpu; revisit if optimizer kernels outrun
  packaged firmware.
- Steam/Wine/Proton/gamescope and the legacy perfetto profiling tools stay
  out of scope until the next milestone.
