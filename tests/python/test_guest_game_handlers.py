# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

"""Tests for game-mode guest agent handlers: byte fetch_file + launch_benchmark."""

import base64
import os
import tempfile

import pytest

from guest.protocol import GuestCommand


@pytest.fixture()
def handler():
    from guest.crucible_guest_agent import GuestAgentHandler

    return GuestAgentHandler()


# ---------------------------------------------------------------------------
# fetch_file: must return the file bytes, not just its size
# ---------------------------------------------------------------------------


def test_fetch_file_returns_base64_contents(handler):
    payload = b"frametime,fps\n16.6,60.2\n"
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(payload)
        tmp_path = f.name
    try:
        resp = handler.handle(GuestCommand(cmd="fetch_file", path=tmp_path))
        assert resp.status == "ok"
        assert resp.data["size"] == len(payload)
        assert resp.data["truncated"] is False
        assert base64.b64decode(resp.data["contents_b64"]) == payload
    finally:
        os.unlink(tmp_path)


def test_fetch_file_binary_roundtrip(handler):
    payload = bytes(range(256)) * 4
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(payload)
        tmp_path = f.name
    try:
        resp = handler.handle(GuestCommand(cmd="fetch_file", path=tmp_path))
        assert resp.status == "ok"
        assert base64.b64decode(resp.data["contents_b64"]) == payload
    finally:
        os.unlink(tmp_path)


def test_fetch_file_oversize_is_truncated(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(agent_mod, "FETCH_FILE_MAX_BYTES", 16)
    payload = b"x" * 64
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(payload)
        tmp_path = f.name
    try:
        resp = handler.handle(GuestCommand(cmd="fetch_file", path=tmp_path))
        assert resp.status == "ok"
        assert resp.data["truncated"] is True
        assert resp.data["size"] == 64
        assert base64.b64decode(resp.data["contents_b64"]) == payload[:16]
    finally:
        os.unlink(tmp_path)


def test_fetch_file_missing_path_still_errors(handler):
    resp = handler.handle(GuestCommand(cmd="fetch_file", path="/tmp/nonexistent-file.csv"))
    assert resp.status == "error"


def test_fetch_file_rejects_paths_outside_allowed_prefixes(handler):
    # The Claude tool loop controls log_path; don't let it exfiltrate
    # arbitrary guest files.
    for path in ("/etc/shadow", "/root/.ssh/id_rsa", "/tmp/../etc/passwd"):
        resp = handler.handle(GuestCommand(cmd="fetch_file", path=path))
        assert resp.status == "error", f"{path} should be rejected"
        assert "not allowed" in resp.message.lower(), f"{path}: {resp.message}"


# ---------------------------------------------------------------------------
# launch_benchmark: native GPU benchmark under MangoHud
# ---------------------------------------------------------------------------


def _launch_cmd(**overrides):
    base = {
        "cmd": "launch_benchmark",
        "name": "vkmark",
        "args": ["--size", "1920x1080"],
        "mangohud_output": "/tmp/crucible_mangohud.csv",
    }
    base.update(overrides)
    return GuestCommand.from_dict(base)


def test_launch_benchmark_rejects_unknown_name(handler):
    resp = handler.handle(_launch_cmd(name="rm"))
    assert resp.status == "error"
    assert "unsupported" in resp.message.lower()


def test_launch_benchmark_requires_mangohud_output(handler):
    resp = handler.handle(_launch_cmd(mangohud_output=None))
    assert resp.status == "error"


def test_launch_benchmark_runs_under_mangohud(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")

        class Result:
            returncode = 0
            stdout = "vkmark Score: 4321"
            stderr = ""

        return Result()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd())
    assert resp.status == "ok"
    assert resp.data["exit_code"] == 0
    assert resp.data["mangohud_output"] == "/tmp/crucible_mangohud.csv"
    assert captured["argv"][0] == "vkmark"
    assert "--size" in captured["argv"]
    assert captured["env"]["MANGOHUD"] == "1"
    # MangoHud has no output_file key: logs land in output_folder with a
    # generated name; the handler renames the newest CSV to mangohud_output.
    assert "output_folder=/tmp" in captured["env"]["MANGOHUD_CONFIG"]
    assert "autostart_log=1" in captured["env"]["MANGOHUD_CONFIG"]


def test_launch_benchmark_spawns_cpu_coload(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    popens = []

    class FakeCoload:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        popens.append(argv)
        return FakeCoload()

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stdout = "vkmark Score: 4321"
            stderr = ""

        return Result()

    monkeypatch.setattr(agent_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd(coload_cpu=4, duration_secs=15))
    assert resp.status == "ok"
    # A stress-ng --cpu 4 co-load must be spawned alongside the benchmark
    # (and terminated in the finally block).
    coload = [a for a in popens if a[0] == "stress-ng"]
    assert coload, f"no stress-ng co-load spawned; popens={popens}"
    assert "--cpu" in coload[0] and "4" in coload[0]


def test_launch_benchmark_no_coload_when_zero(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    popens = []
    monkeypatch.setattr(
        agent_mod.subprocess, "Popen",
        lambda argv, **k: popens.append(argv) or _DummyProc(),
    )
    monkeypatch.setattr(
        agent_mod.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    handler.handle(_launch_cmd(coload_cpu=0))
    assert not [a for a in popens if a and a[0] == "stress-ng"]


class _DummyProc:
    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_launch_benchmark_missing_binary(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    def fake_run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd(name="glmark2"))
    assert resp.status == "error"
    assert "not found" in resp.message.lower()


def test_launch_benchmark_rejects_output_outside_allowed_prefixes(handler):
    # Write-side counterpart of the fetch_file guard: mangohud_output is
    # mkdir'd and rename-targeted, so an unrestricted path means arbitrary
    # file write in the guest.
    for path in ("/etc/cron.d/mh.csv", "/tmp/../etc/mh.csv", "/root/mh.csv"):
        resp = handler.handle(_launch_cmd(mangohud_output=path))
        assert resp.status == "error", f"{path} should be rejected"
        assert "not allowed" in resp.message.lower(), f"{path}: {resp.message}"


def test_launch_benchmark_rejects_comma_in_output_path(handler):
    # MANGOHUD_CONFIG is comma-separated; a comma in the folder would
    # silently corrupt the config string.
    resp = handler.handle(_launch_cmd(mangohud_output="/tmp/foo,bar/mh.csv"))
    assert resp.status == "error"


def test_launch_benchmark_reports_psi_deltas(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd())
    assert resp.status == "ok"
    assert "psi_cpu_delta" in resp.data
    assert "psi_memory_delta" in resp.data
    assert "psi_io_delta" in resp.data


def test_launch_benchmark_vkmark_forces_headless_winsys(handler, monkeypatch):
    # vkmark's default kms winsys presents via raw DRM atomic commits and
    # never creates a VkSwapchainKHR, so MangoHud's QueuePresentKHR hook
    # sees zero frames. Only the headless winsys (VK_EXT_headless_surface)
    # produces a hookable swapchain. Verified on RDNA3 passthrough.
    import guest.crucible_guest_agent as agent_mod

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd())
    assert resp.status == "ok"
    assert captured["argv"][:3] == ["vkmark", "--winsys", "headless"]
    # caller args still appended
    assert "--size" in captured["argv"]


def test_launch_benchmark_mangohud_log_duration_finite(handler, monkeypatch):
    # log_duration=0 means "log until stop_logging", but nothing ever calls
    # stop_logging before the app exits, so the CSV is never written.
    # no_display suppresses the HUD update loop that feeds the logger —
    # also fatal. The CSV only appears with a finite log_duration that
    # elapses before the benchmark exits, plus the HUD left enabled.
    import guest.crucible_guest_agent as agent_mod

    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd(duration_secs=15))
    assert resp.status == "ok"
    config = captured["env"]["MANGOHUD_CONFIG"]
    assert "log_duration=13" in config
    assert "log_interval=100" in config
    assert "autostart_log=1" in config
    assert "no_display" not in config


def test_launch_benchmark_log_duration_clamped_and_defaulted(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)

    # Tiny duration clamps to 1s, never 0 (0 = the never-flushes bug).
    resp = handler.handle(_launch_cmd(duration_secs=2))
    assert resp.status == "ok"
    assert "log_duration=1" in captured["env"]["MANGOHUD_CONFIG"]

    # Missing duration falls back to the module default.
    resp = handler.handle(_launch_cmd())
    assert resp.status == "ok"
    expected = max(1, agent_mod.DEFAULT_LAUNCH_BENCHMARK_DURATION_SECS - 2)
    assert f"log_duration={expected}" in captured["env"]["MANGOHUD_CONFIG"]


def test_launch_benchmark_loads_gpu_module_first(handler, monkeypatch):
    # The orchestrator boots the guest with vng --exec: no systemd, no udev
    # coldplug, so nothing auto-loads amdgpu for the passed-through GPU.
    # The handler must modprobe before launching or the benchmark falls
    # back to llvmpipe (or finds no Vulkan device at all).
    import guest.crucible_guest_agent as agent_mod

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd())
    assert resp.status == "ok"
    assert calls[0][:2] == ["modprobe", "amdgpu"]
    assert calls[1][0] == "vkmark"


def test_launch_benchmark_renames_frame_log_not_summary(handler, monkeypatch, tmp_path):
    # MangoHud writes TWO csvs per run: <app>_<ts>.csv (per-frame rows) and
    # <app>_<ts>_summary.csv (aggregates, written LAST so it is the newest).
    # Picking "newest csv" shipped the summary to the profiler, whose parser
    # found zero frame rows and emitted fps_avg=0 for a real GPU run.
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "GUEST_FILE_ALLOWED_PREFIXES", (str(tmp_path) + "/",)
    )
    out = tmp_path / "crucible_mangohud.csv"

    def fake_run(argv, **kwargs):
        if argv[0] == "modprobe":
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        # benchmark "runs": MangoHud drops frame log first, summary second
        frame_log = tmp_path / "vkmark_2026-06-12_22-00-00.csv"
        frame_log.write_text("os,cpu,gpu\nx,y,z\nfps,frametime\n100,10\n")
        summary = tmp_path / "vkmark_2026-06-12_22-00-00_summary.csv"
        summary.write_text("0.1% Min FPS,Average FPS\n578.2,965.1\n")
        import os as _os
        _os.utime(summary, None)  # summary is newest

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    resp = handler.handle(_launch_cmd(mangohud_output=str(out)))
    assert resp.status == "ok"
    assert resp.data["log_found"] is True
    content = out.read_text()
    assert "Average FPS" not in content, "summary csv was picked instead of frame log"
    assert "fps,frametime" in content


# ---------------------------------------------------------------------------
# launch_steam_benchmark: Steam title under weston-headless + MangoHud (G3)
# ---------------------------------------------------------------------------


def _steam_cmd(**overrides):
    base = {
        "cmd": "launch_steam_benchmark",
        "app_id": 570,
        "args": ["+timedemo", "bench"],
        "mangohud_output": "/tmp/crucible_mangohud.csv",
        "duration_secs": 60,
    }
    base.update(overrides)
    return GuestCommand.from_dict(base)


def test_launch_steam_benchmark_requires_app_id(handler):
    resp = handler.handle(_steam_cmd(app_id=None))
    assert resp.status == "error"


def test_launch_steam_benchmark_validates_output_path(handler):
    resp = handler.handle(_steam_cmd(mangohud_output="/etc/evil.csv"))
    assert resp.status == "error"
    assert "not allowed" in resp.message.lower()


def test_has_default_route_parses_proc_net_route(tmp_path):
    import guest.crucible_guest_agent as agent_mod

    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway\nenp0s2\t00000000\t0202000A\n")
    assert agent_mod._has_default_route(str(route)) is True
    route.write_text("Iface\tDestination\tGateway\nenp0s2\t0000FEA9\t00000000\n")
    assert agent_mod._has_default_route(str(route)) is False
    assert agent_mod._has_default_route(str(tmp_path / "missing")) is False


def test_ensure_network_skips_dhcp_when_route_exists(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(agent_mod, "_has_default_route", lambda: True)

    def forbid_run(*args, **kwargs):
        raise AssertionError("dhclient must not run when a route exists")

    monkeypatch.setattr(agent_mod.subprocess, "run", forbid_run)
    assert handler._ensure_network() is None


def test_ensure_network_runs_dhclient_on_first_iface(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(agent_mod, "_has_default_route", lambda: False)
    monkeypatch.setattr(agent_mod.os, "listdir", lambda _: ["lo", "enp0s2"])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    assert handler._ensure_network() is None
    assert calls == [["dhclient", "enp0s2"]]


def test_ensure_network_errors_when_dhclient_fails(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(agent_mod, "_has_default_route", lambda: False)
    monkeypatch.setattr(agent_mod.os, "listdir", lambda _: ["lo", "enp0s2"])
    monkeypatch.setattr(
        agent_mod.subprocess, "run",
        lambda *a, **k: type(
            "R", (), {"returncode": 2, "stdout": "", "stderr": "no lease"}
        )(),
    )
    resp = handler._ensure_network()
    assert resp is not None
    assert resp.status == "error"
    assert "dhclient" in resp.message


def test_launch_steam_benchmark_fails_fast_without_network(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(
        type(handler),
        "_ensure_network",
        lambda self: agent_mod.GuestResponse.error("no route to Steam"),
    )
    resp = handler.handle(_steam_cmd())
    assert resp.status == "error"
    assert "no route" in resp.message


def test_launch_steam_benchmark_starts_weston_and_steam(handler, monkeypatch, tmp_path):
    # Verified recipe from the G3.0 hardware spike: weston headless with
    # the GL renderer + Xwayland; clients connect via WAYLAND_DISPLAY /
    # DISPLAY in a private XDG_RUNTIME_DIR; steam runs as the unprivileged
    # guest user (steamcmd hard-fails as root).
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "GUEST_FILE_ALLOWED_PREFIXES", (str(tmp_path) + "/",)
    )
    monkeypatch.setattr(agent_mod, "STEAM_LAUNCH_GRACE_SECS", 0)
    monkeypatch.setattr(agent_mod, "STEAM_CLIENT_STABLE_SECS", 0)
    monkeypatch.setattr(agent_mod, "XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    # Deterministic regardless of the test host's routing table.
    monkeypatch.setattr(agent_mod, "_has_default_route", lambda: True)
    # weston setup chowns the runtime dir to the steam user and looks it
    # up; neither is possible as an unprivileged test process.
    monkeypatch.setattr(
        agent_mod.pwd, "getpwnam",
        lambda name: type("PW", (), {"pw_uid": 1000, "pw_gid": 1000})(),
    )
    monkeypatch.setattr(agent_mod.os, "chown", lambda *a, **k: None)
    out = tmp_path / "mh.csv"

    popens = []

    class FakeProc:
        def __init__(self, argv):
            self.argv = argv
            self.pid = 4242

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        popens.append((argv, kwargs.get("env") or {}))
        # Emit the frame log only on the SECOND applaunch (no -silent) — the
        # first launch rides the client start and its CSV would predate the
        # pre_existing snapshot. Real MangoHud writes it after render anyway.
        if "-applaunch" in argv and "-silent" not in argv:
            (tmp_path / "dota2_2026-06-13_00-00-00.csv").write_text(
                "os,cpu,gpu\nx,y,z\nfps,frametime\n144,6.9\n"
            )
        return FakeProc(argv)

    monkeypatch.setattr(agent_mod.subprocess, "Popen", fake_popen)

    # The client-liveness probe (pgrep) reports absent before the client
    # wrapper is spawned and present afterwards; everything else run()s
    # cleanly (modprobe).
    pgrep_calls = []

    def fake_run(argv, **kwargs):
        rc = 0
        if argv[0] == "pgrep":
            pgrep_calls.append(argv)
            rc = 1 if len(pgrep_calls) == 1 else 0
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)

    resp = handler.handle(_steam_cmd(mangohud_output=str(out)))
    assert resp.status == "ok", resp.message
    assert resp.data["log_found"] is True
    assert "fps,frametime" in out.read_text()

    weston_argv, weston_env = popens[0]
    # weston runs as the steam user (its display must be owned by the same
    # user the client runs as, or the client can't open it).
    assert weston_argv[:4] == ["runuser", "-u", "crucible", "--"]
    assert weston_argv[4] == "weston"
    assert "--backend=headless" in weston_argv
    assert "--renderer=gl" in weston_argv
    assert "--xwayland" in weston_argv
    # Weston's default 300s idle timeout blanks the (input-less) headless
    # output and nothing ever wakes it: frame callbacks stop, Xwayland
    # Present degrades to its 1 Hz fallback timer (Civ 6 at ~1000ms/frame)
    # and Vulkan WSI presents block forever (Dota frozen). Idle-out must
    # be disabled or every game launched after the 300s mark measures the
    # sleeping compositor, not the GPU.
    assert "--idle-time=0" in weston_argv

    # Two-phase launch. The FIRST client start boots -silent AND carries
    # -applaunch (plus the MangoHud env the game inherits): verified
    # 2026-07-01 that the game only actually spawns when -applaunch rides
    # the client's own startup, not when sent to a bare client over IPC.
    client_argv, client_env_used = popens[1]
    assert client_argv[:4] == ["runuser", "-u", "crucible", "--"]
    # The extracted client's steam.sh, never the Debian /usr/games/steam
    # wrapper (it targets ~/.steam/debian-installation and hangs on a
    # zenity first-run dialog when run headless).
    assert client_argv[4] == "/home/crucible/.local/share/Steam/steam.sh"
    assert "-silent" in client_argv
    assert "-applaunch" in client_argv
    assert "570" in client_argv
    assert client_env_used["MANGOHUD"] == "1"
    assert "autostart_log=60" in client_env_used["MANGOHUD_CONFIG"]
    assert "log_duration=60" in client_env_used["MANGOHUD_CONFIG"]
    assert "no_display" not in client_env_used["MANGOHUD_CONFIG"]
    # Mesa GL vsync-off: under Xwayland+weston-headless a vsync'd GLX swap
    # costs ~1.5 repaint ticks (Civ 6 menu measured 40fps against a 60Hz
    # output with the GPU 4% busy) — benchmark numbers must not be capped
    # or quantized by the compositor's present pacing.
    assert client_env_used["vblank_mode"] == "0"
    # The zink routing experiment is reverted: "radeonsi presents no
    # frames" was actually the weston idle-out bug, and stock radeonsi GL
    # is what a real desktop runs (measured fine once the compositor stays
    # awake). The launch env must not steer Mesa away from the default.
    assert "MESA_LOADER_DRIVER_OVERRIDE" not in client_env_used
    assert "__GLX_VENDOR_LIBRARY_NAME" not in client_env_used

    # The SECOND invocation re-sends -applaunch over IPC as a retry; it has
    # no -silent (the client is already up).
    steam_argv, steam_env = popens[2]
    assert steam_argv[:4] == ["runuser", "-u", "crucible", "--"]
    assert steam_argv[4] == "/home/crucible/.local/share/Steam/steam.sh"
    assert "-silent" not in steam_argv
    joined = " ".join(steam_argv)
    assert "-applaunch 570" in joined
    assert "+timedemo bench" in joined
    assert steam_env["DISPLAY"] == ":0"
    assert steam_env["WAYLAND_DISPLAY"] == "wayland-1"
    assert steam_env["MANGOHUD"] == "1"


def test_ensure_steam_client_errors_when_client_never_stabilizes(
    handler, monkeypatch
):
    import guest.crucible_guest_agent as agent_mod

    # Drive monotonic time forward deterministically so the settle
    # deadline is reached without real sleeping.
    clock = {"t": 0.0}
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(agent_mod, "STEAM_CLIENT_SETTLE_SECS", 60)
    # pgrep never finds a client; the steam.sh wrapper exiting 0 (it does
    # so once the client daemonizes) must not itself be read as success.
    monkeypatch.setattr(
        agent_mod.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(
        agent_mod.subprocess, "Popen",
        lambda *a, **k: type("P", (), {"pid": 4242, "poll": lambda self: 0})(),
    )
    resp = handler._ensure_steam_client({})
    assert resp is not None
    assert resp.status == "error"
    assert "never stayed up" in resp.message


def test_ensure_steam_client_tolerates_update_restart(handler, monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    clock = {"t": 0.0}
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(agent_mod, "STEAM_CLIENT_SETTLE_SECS", 600)
    monkeypatch.setattr(agent_mod, "STEAM_CLIENT_STABLE_SECS", 45)
    monkeypatch.setattr(agent_mod, "STEAM_CLIENT_POLL_SECS", 5)
    monkeypatch.setattr(
        agent_mod.subprocess, "Popen",
        lambda *a, **k: type("P", (), {"pid": 4242, "poll": lambda self: 0})(),
    )

    # pgrep returncode 0 = present, 1 = absent. First call is the
    # early "already running?" check (absent → proceed to launch); then
    # the poll loop sees present, a drop (self-update restart), and a
    # long stable run.
    liveness = iter([1, 0, 0, 1] + [0] * 40)

    def fake_run(argv, **kwargs):
        rc = next(liveness) if argv[0] == "pgrep" else 0
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)
    assert handler._ensure_steam_client({}) is None


def test_launch_steam_benchmark_times_out_without_log(handler, monkeypatch, tmp_path):
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "GUEST_FILE_ALLOWED_PREFIXES", (str(tmp_path) + "/",)
    )
    monkeypatch.setattr(agent_mod, "STEAM_LAUNCH_GRACE_SECS", 0)
    monkeypatch.setattr(agent_mod, "XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    # Deterministic regardless of the test host's routing table.
    monkeypatch.setattr(agent_mod, "_has_default_route", lambda: True)
    monkeypatch.setattr(agent_mod, "STEAM_LOG_START_DELAY_SECS", 0)
    monkeypatch.setattr(
        agent_mod.pwd, "getpwnam",
        lambda name: type("PW", (), {"pw_uid": 1000, "pw_gid": 1000})(),
    )
    monkeypatch.setattr(agent_mod.os, "chown", lambda *a, **k: None)

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(agent_mod.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(
        agent_mod.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    resp = handler.handle(
        _steam_cmd(mangohud_output=str(tmp_path / "mh.csv"), duration_secs=1)
    )
    assert resp.status == "error"
    assert "no mangohud log" in resp.message.lower()


# ---------------------------------------------------------------------------
# First-party benchmark log harvest (Civ 6 Logs/Benchmark-*.csv)
# ---------------------------------------------------------------------------


def test_harvest_firstparty_log_copies_new_stable_file(handler, monkeypatch, tmp_path):
    import guest.crucible_guest_agent as mod

    steam_home = tmp_path / "steam-home"
    logs = steam_home / "Logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(mod, "STEAM_HOME", str(steam_home))
    monkeypatch.setattr(mod, "FIRSTPARTY_LOG_WAIT_SECS", 10)

    stale = logs / "Benchmark-old.csv"
    stale.write_text("99.9\n")
    pre = {stale}

    fresh = logs / "Benchmark-new.csv"
    fresh.write_text("16.6\n33.3\n")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mangohud_output = out_dir / "crucible_mangohud.csv"

    dest = handler._harvest_firstparty_log("Logs/Benchmark-*.csv", pre, mangohud_output)
    assert dest == str(out_dir / "crucible_mangohud_firstparty.csv")
    assert (out_dir / "crucible_mangohud_firstparty.csv").read_text() == "16.6\n33.3\n"


def test_harvest_firstparty_log_returns_empty_when_no_new_file(
    handler, monkeypatch, tmp_path
):
    import guest.crucible_guest_agent as mod

    steam_home = tmp_path / "steam-home"
    (steam_home / "Logs").mkdir(parents=True)
    monkeypatch.setattr(mod, "STEAM_HOME", str(steam_home))
    monkeypatch.setattr(mod, "FIRSTPARTY_LOG_WAIT_SECS", 3)

    dest = handler._harvest_firstparty_log(
        "Logs/Benchmark-*.csv", set(), tmp_path / "crucible_mangohud.csv"
    )
    assert dest == ""


def test_start_profiling_buffer_size_override(handler, monkeypatch):
    import io

    import guest.crucible_guest_agent as mod

    captured = {"config": b""}

    class FakeStdin(io.BytesIO):
        def close(self):
            captured["config"] = self.getvalue()
            super().close()

    class FakeProc:
        pid = 4242
        stdin = FakeStdin()

        def poll(self):
            return None

    monkeypatch.setattr(
        mod.GuestAgentHandler, "_ensure_perfetto_daemons", lambda self: None
    )
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **kw: FakeProc())

    cmd = GuestCommand(
        cmd="start_profiling",
        duration_secs=60,
        config={"output": "/tmp/t.trace", "buffer_size_kb": 6144},
    )
    resp = handler._handle_start_profiling(cmd)
    assert resp.status == "ok", resp.message
    text = captured["config"].decode()
    assert "duration_ms: 60000" in text
    assert "size_kb: 6144" in text
    assert "size_kb: 131072" not in text


def test_fetch_file_offset_pages_through_large_file(handler, monkeypatch, tmp_path):
    import guest.crucible_guest_agent as mod

    monkeypatch.setattr(mod, "FETCH_FILE_MAX_BYTES", 8)
    payload = b"0123456789abcdef"  # 16 bytes = 2 chunks of 8
    target = tmp_path / "trace.bin"
    target.write_bytes(payload)
    monkeypatch.setattr(
        mod, "GUEST_FILE_ALLOWED_PREFIXES", (str(tmp_path) + "/",)
    )

    first = handler._handle_fetch_file(
        GuestCommand(cmd="fetch_file", path=str(target))
    )
    assert first.status == "ok"
    assert base64.b64decode(first.data["contents_b64"]) == payload[:8]
    assert first.data["truncated"] is True
    assert first.data["offset"] == 0

    second = handler._handle_fetch_file(
        GuestCommand(cmd="fetch_file", path=str(target), config={"offset": 8})
    )
    assert second.status == "ok"
    assert base64.b64decode(second.data["contents_b64"]) == payload[8:]
    assert second.data["truncated"] is False

    bad = handler._handle_fetch_file(
        GuestCommand(cmd="fetch_file", path=str(target), config={"offset": -1})
    )
    assert bad.status == "error"
