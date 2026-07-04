# Steam Deck as a bare-metal validation lane

Goal: validate winner patches on the canonical Linux gaming device.
Division of labor: VM (virtme-ng) = discovery iteration; Deck =
real-silicon validation; (future BMC tower = unattended bare-metal).

## Kernel install flow (community-proven, e.g. linux-charcoal)

    sudo steamos-readonly disable
    sudo pacman -U <kernel>.pkg.tar.zst   # hooks run mkinitcpio -P + grub update
    sudo steamos-readonly enable
    reboot; uname -a

Build against Valve's linux-neptune config (sources:
github.com/valvesoftware/steamos_kernel) so Deck-specific quirks
(audio DSP, panel, controller) keep working. Cross-compile on the
workstation; the Deck's 4-core Zen2 is not a build machine.

## Boot chain / autonomous recovery (key finding)

ESP steamcl.efi chainloader -> slot A/B selection via A.conf/B.conf
(fields: boot-requested-at, boot-other, boot-attempts, boot-count,
image-invalid) -> per-slot GRUB -> /boot/vmlinuz-linux-neptune.
Failed boots exhaust boot-attempts, slot is marked image-invalid, and
steamcl AUTOMATICALLY falls back to the other slot.

Crucible design: slot A stays stock forever; slot B is the test slot.
MachineManager loop: cross-compile -> scp -> install into B ->
steamos-bootconf request-boot B -> reboot -> agent handshake over TCP.
Panic/no-boot self-recovers to stock via boot counting. Add panic=10 +
sp5100_tco watchdog for hangs. Residual zombie states need hands:
Boot Manager = Vol- + Power; BIOS = Vol+ + Power.

## Operational caveats

- SteamOS updates rewrite root/boot and remove custom kernels —
  disable auto-updates during measurement campaigns; recheck uname
  after any update.
- Thermal/power variance: pin TDP and GPU clocks via sysfs (gamescope
  does the same) before trusting numbers.
- Guest agent is stdlib-only Python: runs on SteamOS as-is, vsock
  swapped for TCP. Steam client is native and persistently logged in —
  no per-boot client settle.

Sources: linux-charcoal README, randombk/steamos-teardown docs/boot.md,
ArchWiki Steam Deck page, Valve steamos_kernel, Steam recovery FAQ.
