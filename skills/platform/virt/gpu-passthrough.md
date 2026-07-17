# Virt lane: GPU passthrough VM (vng + VFIO)

The discovery-iteration lane: virtme-ng boots the kernel-under-test
with the RX 7900 XT passed through; vkmark/glmark2 render in-guest
under MangoHud (`[measurement] mode = "game"`). Verified on real
hardware 2026-06-12 (RADV NAVI31, real frames, parseable CSV); the
game-mode kernel-patch loop (patch -> rebuild -> reboot -> re-measure
on the passthrough GPU) works. This file is the source of truth for
the lane's granular constraints; CLAUDE.md only summarizes.

## Host runtime setup (reset by every host reboot)

Required before any GPU run (`CRUCIBLE_E2E_GPU=1` or a game-mode
grind):

- `testbed/virt/setup-host.sh <gpu-addr> --bind` -- binds EVERY
  function of the GPU slot to vfio-pci and chowns the
  `/dev/vfio/<group>` nodes.
- A memlock limit >= guest RAM on the orchestrator process
  (unprivileged QEMU needs it; the 8 MB default fails DMA mapping):

  ```bash
  pid=$(pgrep -x crucible-orches | head -1)
  sudo prlimit --pid "$pid" --memlock=unlimited
  grep -i "max locked" /proc/$pid/limits   # must say unlimited
  ```

  **Never `pgrep -f crucible-orchestrator`** -- the pattern matches the
  shell running the pgrep itself (the config path is on your own
  command line), prlimit silently lands on the wrong process, and QEMU
  later dies with `vfio_container_dma_map ... = -12 (Cannot allocate
  memory)`. Use the exact comm name (15-char truncated) and verify the
  effect on the target, not the prlimit exit status. Permanent
  alternative: a `/etc/security/limits.d/` entry raising memlock for
  the login user (setup-host.sh prints the recipe).
- `~/.cache/virtme-ng` must exist -- QEMU dies instantly on the
  missing 9p fsdev dir if a cache cleaner removed it.

## Hardware constraints (encoded in code -- do not regress)

- The 7900 XT is a **4-function PCI device** (VGA/audio/USB/UCSI, each
  in its own IOMMU group); QEMU's bus reset needs all of them on
  vfio-pci ("Cannot reset device, depends on group N which is not
  owned"). `VmManager::vfio_sibling_functions` discovers them,
  `validate_passthrough` checks each, `setup-host.sh` binds the slot.
- `rombar=0` on the VGA function is mandatory -- QEMU hangs forever
  reading the option ROM of a GPU the host previously drove.
- The guest runs vkmark with `--winsys headless`: the default kms
  winsys presents via raw DRM atomic commits with no VkSwapchainKHR,
  so MangoHud's present hook records nothing.
- MangoHud only flushes its CSV when logging stops BEFORE the app
  exits, and `no_display` starves the logger. The guest derives a
  finite `log_duration` from `LaunchBenchmark.duration_secs` (wire
  field, three-file rule) and keeps the HUD enabled.
- `KernelBuilder` runs `make modules_install
  INSTALL_MOD_PATH=.virtme_mods` and `VmManager` overlays it via
  `--rodir` -- vng with `--root` resolves modules only from inside the
  rootfs, and the test kernel needs `CONFIG_DRM_AMDGPU=m` for the
  guest to drive the card.
- vng's QEMU grandchild survives `kill_on_drop` (`sh -c` chain);
  `VmManager` spawns into a process group and `shutdown` kills the
  group, otherwise a leaked QEMU holds vsock CID 3 across runs.
  **`shutdown` must also drain the group before returning**
  (`wait_for_process_group_exit` polls `kill(-pgid, 0)` until ESRCH +
  a 300ms settle): `child.wait()` reaps only the direct vng wrapper,
  but the QEMU grandchild dies asynchronously and keeps the GPU's
  `/dev/vfio/<group>` open. This is what lost GPU passthrough across a
  kernel-patch reboot -- boot 2 raced the dying QEMU and hit
  `Could not open '/dev/vfio/14': Device or resource busy` -> GPU
  never attached -> guest never booted -> downstream vsock ENODEV
  (misleading; the real error is only in the vng/QEMU console).
  `boot` tees that console to `<kernel_src>/../crucible-vm-boot.log`.
  Verified: baseline 14k fps -> amdgpu patch -> rebuild -> reboot ->
  comparison 14k fps, 0 vfio-busy (held across a 10-reboot grind).
- The profiler prompt forbids fabricating metrics on tool failure
  (`{"error": ...}` instead of zeros); a zero `fps_avg` once masked a
  complete VFIO failure as a successful cycle.

## Game-mode data flow details

The profiler calls `launch_benchmark(name, args, mangohud_output)` --
the guest runs an allow-listed native benchmark (`vkmark`/`glmark2`,
per `[measurement] game_benchmark`) under MangoHud with
`autostart_log=1` and renames the newest generated CSV to the
deterministic `mangohud_output` path (MangoHud has no fixed-output
option) -- then `fetch_mangohud_log(log_path)`, which pulls the CSV
over vsock as base64 (`fetch_file` returns `contents_b64`/`truncated`,
8 MiB cap) and parses it with `parse_mangohud_csv` (nearest-rank
percentiles; skips MangoHud's two system-info header rows). Non-zero
`fps_avg` is the discriminator that real frames flowed. The
game_selector is told `workload_kind` and pivots to
`list_native_benchmarks` in game mode (no Steam library in the guest).

## Additional constraints

- Software-render fallback is NOT a meaningful gate for game mode:
  vkmark cannot present without a DRM device, and the test kernel
  exposes none without a passed-through GPU (2026-06-12 finding; the
  earlier "lavapipe still exercises the path" assumption was wrong).
  The GPU e2e (`CRUCIBLE_E2E_GPU=1`) is the only gate that proves the
  frame path.
- Firmware/kernel skew: the game rootfs ships trixie's
  firmware-amd-graphics; it must satisfy the kernel-under-test's
  amdgpu. If optimizer kernels outrun packaged firmware, amdgpu init
  fails in-guest -- refresh the rootfs firmware before blaming the
  patch.
- vng already passes `-display none -vga none` to QEMU; adding another
  display option is a duplicate-option error. Headless is the default.

## Game rootfs

Built by `testbed/virt/setup-game-rootfs.sh` into
`~/.crucible/game-rootfs` on Debian **trixie** (bookworm's Mesa 22.x
predates usable RDNA3 support; trixie ships Mesa 25.x) with
`mesa-vulkan-drivers`, `vulkan-tools`, `vkmark`, `glmark2`,
`glmark2-drm`, `mangohud`, and `firmware-amd-graphics`
(non-free-firmware component). vkmark/glmark2 render via DRM/KMS
directly -- no compositor in the guest. Stamp `.crucible-game-built`.

Rootfs split:

- ~/.crucible/rootfs (bookworm) -- synthetic/bench: stress-ng, perf,
  schbench, toolchain. No GPU needed. Used for winner validation runs.
- ~/.crucible/game-rootfs (trixie) -- vkmark/glmark2 + MangoHud.
  Needs passthrough for real frames.
- ~/.crucible/steam-rootfs -- see steam-mode.md.

## Running long grinds (known robustness gaps, not yet fixed)

- **No backoff on agent failure**: when the Claude API hits a spend
  cap, GameSelector fails instantly and the loop burns all
  `--max-cycles` in seconds; a rate-limit/backoff branch would pause
  instead.
- A **harness-killed orchestrator leaks its QEMU** (the process is not
  reaped through `shutdown`), holding the GPU + vsock CID; manual
  cleanup is `pkill qemu` + `git checkout` the kernel tree. Launch
  long grinds with `setsid` so they survive the parent shell's
  process-group teardown.
