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
