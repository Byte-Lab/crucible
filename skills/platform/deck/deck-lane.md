# Steam Deck as a bare-metal validation lane

Goal: validate winner patches on the canonical Linux gaming device.
Division of labor: VM (virtme-ng) = discovery iteration; Deck =
real-silicon validation. Orchestrator side: DeckBackend in
crates/crucible-orchestrator/src/deck.rs; harness scripts and A/B
tooling in testbed/deck/ (inventory in testbed/README.md).

## Kernel build

Cross-compile on the workstation (the Deck's 4-core Zen2 is not a
build machine) against Valve's linux-neptune source and the Deck's own
config:

- Source = Valve's src package from
  steamdeck-packages.steamos.cloud (archlinux-mirror/sources/
  jupiter-3.8.1x/linux-neptune-616-<ver>.src.tar.gz), which contains a
  bare git repo `archlinux-linux-neptune`. NOT
  github.com/ValveSoftware/steamos_kernel -- that is the old Steam
  Machine repo.
- Config = the Deck's own `zcat /proc/config.gz` (the `config-neptune`
  file in the package is only a small overlay).
- LOCALVERSION discipline: set CONFIG_LOCALVERSION to match the target
  release, LOCALVERSION_AUTO off, and build with `make LOCALVERSION=`
  or the dirty-tree `+` suffix breaks the release-string match --
  mismatched vermagic means modules refuse to load, the boot drops to
  an initramfs shell, and an off-network Deck is a brick.
- Test kernels installed on the Deck MUST use a distinct LOCALVERSION
  (e.g. `-drmsched`) and never overwrite the stock kernel or modules.

## Boot chain (background facts)

ESP steamcl.efi chainloader -> slot A/B selection via A.conf/B.conf
(fields: boot-requested-at, boot-other, boot-attempts, boot-count,
image-invalid) -> per-slot GRUB -> /boot/vmlinuz-linux-neptune. Failed
boots exhaust boot-attempts, the slot is marked image-invalid, and
steamcl falls back to the other slot.

## Test-boot mechanism: grub one-shot on slot A (v3, the one that works)

Slot-B testing is RETIRED: steamcl's `set-mode reboot-other` flag
persists until a boot COMPLETES, so a boots-but-hangs slot B re-traps
every hard reset back into B (physical boot-picker recovery each time).
The working mechanism instead boots test kernels via a one-shot GRUB
selector on slot A:

- Test kernel installed alongside stock on A
  (/boot/vmlinuz-crucible-test + initramfs + its own module dir); a
  'crucible-test' menuentry via /etc/grub.d/40_custom with the stock
  cmdline plus `panic=10 oops=panic`.
- One-shot arm: a guarded /etc/grub.d/01_crucible_oneshot reads a flag
  from an env file on efi-A by explicit fs-uuid, clears it with
  `save_env -f` BEFORE selecting the test entry; missing/corrupt file
  silently boots stock. (Stock grubenv/next_entry is dead on SteamOS:
  grubx64.efi is standalone, $prefix is a memdisk.)
- Safety net: a canary systemd TIMER (not WantedBy=multi-user, which
  never runs if boot stalls) reboots to stock if the test kernel lacks
  amdgpu/network after a few minutes; RuntimeWatchdogSec with
  sp5100_tco; grub.cfg regen always goes through `grub-script-check`
  before install (a bad grub.cfg is the one unrecoverable-without-hands
  failure); never put fallible statements like bare save_env in
  grub.cfg -- a failure throws an interactive "press any key" hang.
- After any grub-mkconfig, verify the FIRST menuentry still boots the
  stock kernel (10_linux auto-adopts every /boot/vmlinuz-*).
- Do not swap modules in place on a running system: BTF validation
  fails against a differently-built vmlinux and amdgpu never loads.

Residual zombie states need hands: Boot Manager = Vol- + Power;
boot picker = "..." + Power.

## Measurement discipline (Deck-specific)

- Interleave test-vs-control blocks within ONE session; never compare
  against a baseline from a different session or thermal state --
  thermal drift alone has manufactured +14% "wins" that interleaved
  replication nulled.
- Pin TDP and GPU clocks via sysfs before trusting numbers; keep the
  Deck on the charger (battery TDP throttling; USB-A trickle charge is
  insufficient under load).
- Civ 6 on the Van Gogh iGPU is GPU-bound: CPU-scheduler patches cannot
  move its fps. Use CPU-bound benches (schbench) for scheduler signal;
  the primary Deck game metric is frametime p999 (tail), not avg fps.
- Verify kernel identity per boot in the log (/proc/version).

## Operational caveats

- SteamOS updates rewrite root/boot and remove custom kernels: disable
  auto-updates during measurement campaigns; recheck `uname -a` after
  any update.
- The guest agent is stdlib-only Python and runs on SteamOS as-is with
  TRANSPORT=tcp (vsock swapped for TCP port 5000); start it via
  systemd-run (plain nohup dies with the ssh session), open the port in
  firewalld. The Steam client is native and persistently logged in --
  no per-boot client settle.
