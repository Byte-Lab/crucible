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

- [ ] **G3.0 spike (cheap, do first):** steamcmd in a trixie chroot with
  the copied host session: does `+login jqueryheadshot` succeed without a
  Guard prompt? Does `+app_update 570` start downloading? Abort/adjust
  auth approach before building anything else on it.
- [ ] **G3.1 rootfs:** extend `setup-game-rootfs.sh` (or a new
  `setup-steam-rootfs.sh` sharing rootfs-common) with: i386 multiarch,
  `steam-installer` (non-free), `steamcmd`, `weston`, `xwayland`,
  Vulkan/MangoHud i386 variants, the copied Steam session, and a
  steamcmd-driven Dota 2 install. New stamp `.crucible-steam-built`.
- [ ] **G3.2 guest:** weston-headless session management + Steam launch
  handler; MangoHud env on the game process; demo-driven benchmark with
  the existing finite-log-window rules; reuse fetch_file transport.
- [ ] **G3.3 host:** profiler tools + prompt for the steam path;
  game_selector lists installed Steam apps (steamapps/*.acf scan RPC);
  `[measurement] mode = "steam"` (or `game_benchmark = "dota2"` variant)
  threading.
- [ ] **G3.4 e2e:** `CRUCIBLE_E2E_GAME=1` gate — full cycle on a real
  game, non-zero fps, tool-leak scan.

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
