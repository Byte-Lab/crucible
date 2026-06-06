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
    resp = handler.handle(GuestCommand(cmd="fetch_file", path="/nonexistent/file.csv"))
    assert resp.status == "error"


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
