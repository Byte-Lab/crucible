# Virt lane: Steam-in-VM mode (`[measurement] mode = "steam"`)

Real Steam titles launched headless inside the passthrough VM
(milestone G3). This file is the source of truth for the mode's
launch mechanics, presentation stack, rootfs, and debug lessons;
CLAUDE.md only summarizes. Load platform/virt/gpu-passthrough.md too
-- all its VFIO constraints apply here.

## Launch mechanics (encoded in the guest handler)

The profiler calls `launch_steam_benchmark(app_id, args,
mangohud_output, duration_secs)` against the steam rootfs. The guest
handler:

1. DHCPs the slirp NIC (`_ensure_network` -- Steam's CM logon needs a
   route out; `VmManager` appends the netdev unconditionally).
2. Raises `vm.max_map_count`/`overcommit_memory` (RADV +
   pressure-vessel VMA pressure).
3. Starts weston **as the steam user** -- a root-owned Wayland socket
   or X-auth cookie segfaults the crucible-user client with "Unable to
   open display".
4. **Two-phase launch**: the first `steam.sh -silent` start MUST carry
   `-applaunch <id>` and the MangoHud env (the game inherits the
   CLIENT's env; a bare client that gets the launch only via a later
   IPC `-applaunch` never spawns the game), followed by a second IPC
   `-applaunch` as an update-restart retry.
5. Client liveness is probed with `pgrep -x steam` polled until
   continuously up: `steam.sh` exits 0 once the client daemonizes (its
   exit code says nothing), and the client restarts itself once to
   self-update.

Never invoke the Debian `/usr/games/steam` wrapper: it targets
`~/.steam/debian-installation` and hangs on a zenity dialog headless.

Sizing: `[agents] timeout_secs = 1500` covers the whole launch RPC
(client settle 240s + Fossilize + asset load + log window); the steam
e2e case boots 24G (pressure-vessel container build + game OOM at 16G).

## Presentation stack (root-caused 2026-07-02 with Civ 6, app 289070)

- **weston MUST run with `--idle-time=0`** (in `WESTON_ARGV`).
  Weston's default 300s idle timeout blanks the input-less headless
  output and nothing ever wakes it: frame callbacks stop, Xwayland
  Present degrades to its 1 Hz fallback timer (Civ 6's menu "rendered"
  at exactly ~1000ms/frame on an idle GPU), and Vulkan WSI presents
  block forever. This -- not a Dota-specific quirk -- was the earlier
  "Dota stops presenting frames / gpu_load=0" symptom; client settle
  alone eats 240s so every launch landed past the 300s deadline.
- With the compositor awake, Civ 6's menu on stock radeonsi runs
  ~40fps vsync'd / ~71fps uncapped / ~60fps with MangoHud
  `fps_limit=60` at 3-5% GPU. The 40-vs-60 gap is GLX-under-Xwayland
  present pacing (~1.5 repaint ticks per vsync'd swap against weston's
  60Hz headless output, `DEFAULT_OUTPUT_REPAINT_REFRESH`), so the
  launch env sets `vblank_mode=0`: benchmark numbers must not be
  capped or quantized by compositor pacing.
- The zink GL->RADV routing experiment was reverted: its "radeonsi
  presents no frames" premise was the idle-out bug, and Mesa's default
  radeonsi is what real desktops run.
- Civ 6 ships three self-terminating benchmarks:
  `-benchmark graphicsbenchmark` (GPU flythrough; verified headless --
  renders, writes a first-party per-frame CSV to
  `Logs/Benchmark-<ts>.csv` matching MangoHud, exits clean),
  `-benchmark xp2benchmark` (heavier GS scene), and
  `-benchmark aibenchmark` (CPU-bound late-game AI turns -- the right
  workload class for scheduler patches).

## Compositor decision

weston headless + Xwayland, NOT gamescope: gamescope is not packaged
in trixie (sid only), weston 14 + xwayland are in-suite, and weston's
headless GL backend renders on the passthrough GPU with no output
hardware. Revisit gamescope (sid pull or vendored build) only if a
title misbehaves without its fullscreen/resolution spoofing.

## Steam rootfs

Built by `testbed/virt/setup-steam-rootfs.sh` into
`~/.crucible/steam-rootfs` (trixie + i386 multiarch +
steam-installer/steamcmd/weston/xwayland/dbus-x11/isc-dhcp-client).
The script:

- extracts the Steam client bootstrap tarball directly (never runs the
  Debian wrapper);
- creates the `~/.steam/{steam,root}` symlinks;
- adds the `crucible` user to `video`+`render` (weston EGL dies
  without them);
- seeds the steamcmd session + game library + **full-client login
  creds** from `CRUCIBLE_STEAM_CLIENT_CREDS` (default the snap Steam
  dir) -- the client JWT lives in `local.vdf` +
  `config/loginusers.vdf`, which steamcmd never writes; steamcmd's
  cached session alone cannot log the full client in;
- copies the host's perfetto binaries. Stamp `.crucible-steam-built`.

The seeded library must be kept current with host steamcmd (recipe in
the script header): the client refuses `-applaunch` on an
update-required app, and the in-guest download dies in the ephemeral
overlay.

## steamcmd session facts

- The host's cached steamcmd session copied into the rootfs logs in
  with "cached credentials", no Steam Guard prompt -- but only when run
  as uid 1000 (root gets error 13) with /proc mounted in the chroot.
- Token is machine-bound-ish: if the guest ever demands re-auth, run
  one interactive `steamcmd +login` inside a chroot of the rootfs.

## Debugging

- The guest's rw overlay is ephemeral, so in-guest logs vanish at
  poweroff -- write captures to `/run/virtme/cache/...` (the 9p mount
  of host `~/.cache/virtme-ng`, mounted automatically by vng) and
  harvest from the host after `poweroff -f`. The
  `~/.cache/virtme-ng/civ6*.sh` scripts are working examples (boot
  with `--exec 'sh /run/virtme/cache/<script>.sh'`).
- Known gap: `game_selector`'s `list_steam_games` runs on the HOST
  before the VM boots, so it scans the host's Steam libraries, not the
  guest rootfs library the profiler launches from -- in steam mode it
  can claim the configured `steam_app_id` "is not installed" while the
  launch succeeds anyway (observed 2026-07-02 with Civ 6).
  `BENCHMARK_GAMES` in `agents/game_selector/tools.py` also predates
  the verified Civ 6 modes. Point `list_steam_games` at the seeded
  rootfs library
  (`~/.crucible/steam-rootfs/home/crucible/.local/share/Steam/steamapps`)
  or add 289070 to `BENCHMARK_GAMES` before trusting selector output
  in steam mode.

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
