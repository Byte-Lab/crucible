# Virt lane: Steam-in-VM mode (`[measurement] mode = "steam"`)

Real Steam titles launched headless inside the passthrough VM. CLAUDE.md
holds the launch mechanics already encoded in the guest agent (two-phase
`-applaunch`, weston as the steam user with `--idle-time=0`, DHCP before
logon, cred seeding, library refresh, the /usr/games/steam wrapper ban,
`vblank_mode=0`, Civ 6 benchmark modes, 1500s agent timeout, 24G VM,
harvest logs via /run/virtme/cache). This file holds the decisions and
lessons that are NOT in CLAUDE.md.

## Compositor decision

weston headless + Xwayland, NOT gamescope: gamescope is not packaged in
trixie (sid only), weston 14 + xwayland are in-suite, and weston's
headless GL backend renders on the passthrough GPU with no output
hardware. Revisit gamescope (sid pull or vendored build) only if a
title misbehaves without its fullscreen/resolution spoofing.

## steamcmd session facts

- The host's cached steamcmd session copied into the rootfs logs in
  with "cached credentials", no Steam Guard prompt -- but only when run
  as uid 1000 (root gets error 13) with /proc mounted in the chroot.
- steamcmd's session alone does NOT log the full Steam client in; the
  client JWT lives in local.vdf + config/loginusers.vdf (seeded from
  CRUCIBLE_STEAM_CLIENT_CREDS at rootfs build).
- Token is machine-bound-ish: if the guest ever demands re-auth, run
  one interactive `steamcmd +login` inside a chroot of the rootfs.

## Debug lessons (paid for in full cycles)

- The "mmap wall" was a red herring: bursts of `mmap() failed: Cannot
  allocate memory` clustered with pressure-vessel `unsafe call to
  setenv` are benign CEF/steamwebhelper logging -- they appear whether
  or not the game launches. Nine cycles chased it; the real bug was the
  launch recipe (the game process never appearing in ps meant the
  launch was never ISSUED, not that it crashed). Ruled out along the
  way: RAM, vm.max_map_count, client version, and the filesystem
  (a full ext4 virtio-blk library reproduced the burst identically).
- Cold shader cache: a title's first launch per boot runs a Fossilize
  pre-compile (minutes) before rendering, so the MangoHud window can
  elapse with an empty/zero CSV. Bake a warmed
  steamapps/shadercache/<appid> into the rootfs library so every
  boot's overlay sees it pre-built.
