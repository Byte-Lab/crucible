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

- [ ] Build the game rootfs: `scripts/setup-game-rootfs.sh` (sudo + network).
- [ ] Boot check: `vng --root ~/.crucible/game-rootfs -- vulkaninfo` shows
  lavapipe (or RADV once VFIO is bound); `vkmark --list-scenes` works.
- [ ] Software-render e2e: `CRUCIBLE_E2E_GPU=1 cargo test --test e2e -- --nocapture`
  green with non-zero fps via lavapipe.
- [ ] VFIO checkpoint: confirm host display is on the Raphael iGPU
  (13:00.0), then `scripts/setup-host.sh 03:00.0 --bind`, set
  `CRUCIBLE_VFIO_DEVICE=03:00.0`, rerun the GPU e2e on real RDNA3.
- [ ] Synthetic regression: `CRUCIBLE_E2E=1 cargo test --test e2e` still green.

## Notes / decisions

- vng already passes `-display none -vga none` to QEMU
  (virtme/architectures.py); adding our own `-display none` would be a
  duplicate-option error. Headless is the default.
- The host 7900 XT sits alone in IOMMU group 14 (no HDMI-audio sibling to
  drag along) — clean passthrough topology.
- Firmware/kernel skew risk: trixie's `firmware-amd-graphics` must satisfy
  the kernel-under-test's amdgpu; revisit if optimizer kernels outrun
  packaged firmware.
- Steam/Wine/Proton/gamescope and the legacy perfetto profiling tools stay
  out of scope until the next milestone.
