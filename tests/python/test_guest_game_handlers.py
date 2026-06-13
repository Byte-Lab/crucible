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
    monkeypatch.setattr(agent_mod, "XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
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
        if argv[0] != "weston":
            # the "game" produced a frame log
            (tmp_path / "dota2_2026-06-13_00-00-00.csv").write_text(
                "os,cpu,gpu\nx,y,z\nfps,frametime\n144,6.9\n"
            )
        return FakeProc(argv)

    monkeypatch.setattr(agent_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        agent_mod.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    resp = handler.handle(_steam_cmd(mangohud_output=str(out)))
    assert resp.status == "ok", resp.message
    assert resp.data["log_found"] is True
    assert "fps,frametime" in out.read_text()

    weston_argv, weston_env = popens[0]
    assert weston_argv[0] == "weston"
    assert "--backend=headless" in weston_argv
    assert "--renderer=gl" in weston_argv
    assert "--xwayland" in weston_argv

    steam_argv, steam_env = popens[1]
    assert steam_argv[:4] == ["runuser", "-u", "crucible", "--"]
    joined = " ".join(steam_argv)
    assert "steam" in joined and "-applaunch 570" in joined
    assert "+timedemo bench" in joined
    assert steam_env["DISPLAY"] == ":0"
    assert steam_env["WAYLAND_DISPLAY"] == "wayland-1"
    assert steam_env["MANGOHUD"] == "1"
    assert "log_duration=58" in steam_env["MANGOHUD_CONFIG"]
    assert "no_display" not in steam_env["MANGOHUD_CONFIG"]


def test_launch_steam_benchmark_times_out_without_log(handler, monkeypatch, tmp_path):
    import guest.crucible_guest_agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "GUEST_FILE_ALLOWED_PREFIXES", (str(tmp_path) + "/",)
    )
    monkeypatch.setattr(agent_mod, "STEAM_LAUNCH_GRACE_SECS", 0)
    monkeypatch.setattr(agent_mod, "XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)

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
