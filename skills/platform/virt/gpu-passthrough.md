# Virt lane: GPU passthrough VM (vng + VFIO)

The discovery-iteration lane: virtme-ng boots the kernel-under-test
with the RX 7900 XT passed through; vkmark/glmark2 render in-guest
under MangoHud.

CLAUDE.md is authoritative for the operational constraints already
encoded in code -- 4-function VFIO bind, rombar=0, memlock/prlimit,
`--winsys headless`, MangoHud flush semantics, modules_install overlay,
process-group shutdown drain, boot-log tee, profiler no-fabrication
rule. Read "Common commands" + the game-mode status block there first.
This file holds only what CLAUDE.md does not.

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

## Bench-vs-game rootfs split

- ~/.crucible/rootfs (bookworm) -- synthetic/bench: stress-ng, perf,
  schbench, toolchain. No GPU needed. Used for winner validation runs.
- ~/.crucible/game-rootfs (trixie, Mesa 25.x) -- vkmark/glmark2 +
  MangoHud. Needs passthrough for real frames.
- ~/.crucible/steam-rootfs -- see steam-mode.md.
