# G3: Steam Game Measurement (2026-06-12)

Real Steam games in the guest, measured through the existing MangoHud →
fetch_file → evaluator pipeline that the vkmark milestone (G0–G2, verified
on hardware 2026-06-12) proved end to end.

## Decisions

- **Compositor: weston headless + Xwayland, not gamescope.** gamescope is
  not in trixie (sid only, 3.16.24); weston 14 + xwayland are in-suite and
  weston-headless was already the documented escalation path. weston's
  headless backend with the GL renderer renders via the passthrough GPU
  with no output hardware. Revisit gamescope (sid pull or vendored build)
  only if real games misbehave without its fullscreen/resolution spoofing.
- **First title: Dota 2.** Free license (no purchase on the account
  needed), native Linux, Vulkan, and Source 2 supports scripted demo
  playback (`+timedemo`) for repeatable benchmark passes. CS2 is the
  fallback (also free/native/Vulkan, benchmark via workshop map).
  Native-first defers the Proton/Wine variable to G3b.
- **Game files live in the rootfs**, installed at build time by steamcmd
  (`+app_update 570`). The guest mounts the rootfs RO via 9p with vng's rw
  overlays on top; Steam needs a writable steamapps dir → bind the app dir
  into the guest user's Steam library at boot (or vng `--overlay-rwdir`).
- **Steam login: cached steamcmd session from the host** (`~/.local/share/
  Steam` config + token, created interactively 2026-06-12) is copied into
  the rootfs at build time. Tokens are machine-bound-ish but steamcmd
  re-auth without Guard prompt is the norm; verify early (G3.0 spike).
- The existing `launch_game` RPC (steam://rungameid/) and `LaunchBenchmark`
  plumbing stay; a new guest handler `launch_steam_benchmark` composes:
  weston headless → steam -applaunch <id> <args> under MANGOHUD=1 →
  wait for demo/timeout → CSV via the proven fetch path.

## Phases

- [x] **G3.0 spike — PASSED (2026-06-12/13):**
  - steamcmd in a trixie chroot with the copied host session logs in with
    "cached credentials", **no Steam Guard prompt** (must run as uid 1000,
    not root — error 13 otherwise; needs /proc mounted). Dota 2
    (`+app_update 570`) downloads with the free license.
  - Display chain validated on real RDNA3 in the VM: weston headless
    (`--backend=headless --renderer=gl --xwayland`, EGL 1.5 on Mesa) +
    Xwayland; vkmark via `--winsys xcb` (the X11 path a Steam game takes)
    hits 9,499 FPS on RADV NAVI31 with MangoHud writing the frame CSV
    through the Xwayland present path. Env recipe: `XDG_RUNTIME_DIR`
    (0700), `WAYLAND_DISPLAY=wayland-1`, `DISPLAY=:0`, ~6s weston warmup.
- [x] **G3.1 rootfs:** `setup-steam-rootfs.sh` — i386 multiarch,
  `steam-installer`, `steamcmd`, `weston`, `xwayland`, Vulkan/MangoHud,
  copied Steam session, steamcmd Dota 2 install, stamp
  `.crucible-steam-built`. Hardened 2026-07-01: extracts the client
  bootstrap tarball directly (never the zenity-hanging Debian wrapper);
  writes `~/.steam/{steam,root}` symlinks; adds `dbus-x11` +
  `isc-dhcp-client`; grants `video`+`render` groups; seeds full-client
  login creds from `CRUCIBLE_STEAM_CLIENT_CREDS` (snap Steam) — steamcmd's
  cached session alone does NOT log the full client in. Library-refresh
  recipe (host steamcmd) documented in the header: the client refuses
  `-applaunch` on an out-of-date app.
- [x] **G3.2 guest:** `launch_steam_benchmark` handler. Fixes landed
  2026-07-01: (1) DHCP the guest NIC before logon (`_ensure_network`);
  (2) run weston as the **steam user**, not root — a root-owned Wayland
  socket / X-auth cookie makes the crucible-user client segfault with
  "Unable to open display"; (3) two-phase launch — start the client
  `-silent`, poll until it stays up `STEAM_CLIENT_STABLE_SECS` (survives
  the one-time self-update restart), then a second `steam.sh -applaunch`
  over IPC; (4) raise `vm.max_map_count`/`overcommit_memory` for the
  RADV+pressure-vessel VMA pressure; MangoHud `autostart_log` delay skips
  the load screen.
- [x] **G3.3 host:** profiler `launch_steam_benchmark`/`fetch_mangohud_log`
  tools + no-fabrication prompt; game_selector returns the configured
  title; `[measurement] mode = "steam"` + `steam_app_id` threading; slirp
  netdev in `VmManager`; `[agents] timeout_secs` raised to 1500 for the
  long Steam launch RPC.
- [x] **G3.4 launch — SOLVED (2026-07-01):** `dota2` execs and renders on
  RADV NAVI31 through the agent handler. The real blocker was the **launch
  recipe**, not any resource limit: `-applaunch <id>` must ride the
  **first** Steam client start (with the MangoHud env the game inherits) —
  a bare `-silent` client that receives the launch only via a later IPC
  `-applaunch` never spawns the game. Encoded in
  `_ensure_steam_client(env, launch_argv)` + a second IPC applaunch retry.
- [~] **G3.4 e2e:** `CRUCIBLE_E2E_GAME=1` gate wired (steam-mode cycle,
  non-zero fps assert, 24G, tool-leak scan). Pending a clean end-to-end
  green now that the game launches (needs the warm shader cache below).

## Resolved: the "mmap wall" was a red herring (2026-07-01)

Nine VM cycles chased a deterministic burst of `mmap() failed: Cannot
allocate memory` (clustered with pressure-vessel `unsafe call to setenv`)
as the launch blocker. It was **benign CEF/steamwebhelper logging** — it
appears whether or not the game launches. Proof it was not the cause:
moving the entire 75 GB Steam library onto an **ext4 virtio-blk image**
(kernel rebuilt with `CONFIG_EXT4_FS=y`, qcow2 snapshot overlay) produced
the identical mmap burst *and* the game launched once the recipe was
fixed. Ruled out along the way: RAM (18.5 GB free at "failure"),
`vm.max_map_count` (confirmed 1048576 in-guest), client version split,
and the filesystem (ext4 == 9p behaviour). Lesson: `dota2` never appeared
in `ps` because the launch was never *issued* correctly, not because it
crashed — the console noise masked a control-flow bug. Kept the ext4
kernel (harmless); shelved the block image (unneeded).

## Open: warm shader cache for repeatable baselines

Dota's first launch per boot runs a cold Fossilize shader pre-compile
(minutes) before it renders, so MangoHud's log window can elapse before
any frame — the baseline CSV comes back empty/zero. Fix in progress: bake
a warmed `steamapps/shadercache/570` into the rootfs library (read-only
9p lower layer) so every boot's overlay sees it pre-built and renders
promptly. This is the concrete form of the plan's "baseline after warmup"
step.

## Risks

- Steam client auto-update loop on first start (slow first boot; cache the
  updated client in the rootfs by running it once at build time).
- steamcmd token may demand re-auth inside the guest (different machine
  id). Mitigation: G3.0 spike catches it; fallback is one interactive
  `steamcmd +login` run inside a chroot of the rootfs with the user.
- Dota 2 download ~40 GB into the rootfs; disk has 1.2 TB free. Build time
  dominated by download (one-off, stamped).
- weston-headless GL renderer must initialize on RADV in the guest —
  verify with `weston-info`/glmark2-wayland before the Steam layer.
- Source 2 timedemo availability/flags drift; verify the exact CLI once
  the game is installed (`-novid +timedemo <demo> +q` style flags).
