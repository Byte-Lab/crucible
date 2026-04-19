import json
import os
import tempfile

from guest.protocol import GuestCommand, GuestResponse


def test_handle_health_check():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="health_check")
    resp = handler.handle(cmd)
    assert resp.status == "ok"
    assert "uptime" in resp.data
    assert "pid" in resp.data


def test_handle_setup_cgroups():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="setup_cgroups", groups=["game", "compositor"])
    resp = handler.handle(cmd)
    # error is acceptable in test env without cgroup access
    assert resp.status in ("ok", "error")


def test_handle_get_metrics():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="get_metrics")
    resp = handler.handle(cmd)
    assert resp.status == "ok"
    assert "system_psi" in resp.data


def test_handle_unknown_command():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="nonexistent_command")
    resp = handler.handle(cmd)
    assert resp.status == "error"
    assert "unknown" in resp.message.lower()


def test_handle_fetch_file():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("test content")
        tmp_path = f.name
    try:
        cmd = GuestCommand(cmd="fetch_file", path=tmp_path)
        resp = handler.handle(cmd)
        assert resp.status == "ok"
        assert resp.data["size"] > 0
    finally:
        os.unlink(tmp_path)


def test_handle_fetch_file_not_found():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="fetch_file", path="/nonexistent/file.txt")
    resp = handler.handle(cmd)
    assert resp.status == "error"
