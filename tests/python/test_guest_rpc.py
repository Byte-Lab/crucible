"""Tests for the host-side vsock client used by Claude agents.

We mock the socket because AF_VSOCK is only available inside a VM-aware
kernel context; CI may not have it.
"""
import json
import socket
import struct
from typing import Any

import pytest

from agents.common.guest_rpc import GuestRpc


class FakeSocket:
    """Records sent bytes and yields canned responses on recv."""

    def __init__(self, recv_payloads: list[dict[str, Any]]) -> None:
        self.send_buf: bytearray = bytearray()
        chunks: list[bytes] = []
        for p in recv_payloads:
            body = json.dumps(p).encode()
            chunks.append(struct.pack("!I", len(body)) + body)
        self._recv_buf = b"".join(chunks)
        self._recv_pos = 0
        self.connected_to: tuple[int, int] | None = None
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, t: float) -> None:
        self.timeout = t

    def connect(self, addr: tuple[int, int]) -> None:
        self.connected_to = addr

    def sendall(self, b: bytes) -> None:
        self.send_buf.extend(b)

    def recv(self, n: int) -> bytes:
        chunk = self._recv_buf[self._recv_pos : self._recv_pos + n]
        self._recv_pos += len(chunk)
        return bytes(chunk)

    def close(self) -> None:
        self.closed = True


def test_guest_rpc_call_frames_request_and_parses_response(monkeypatch):
    fake = FakeSocket([{"status": "ok", "data": {"uptime": 1.2}}])
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: fake)

    rpc = GuestRpc(cid=3, timeout_secs=5.0)
    resp = rpc.call("health_check")

    assert fake.connected_to == (3, 5000)
    assert fake.timeout == 5.0
    assert fake.closed is True
    assert resp == {"status": "ok", "data": {"uptime": 1.2}}

    # Verify wire framing: 4-byte big-endian length + JSON payload.
    sent = bytes(fake.send_buf)
    (length,) = struct.unpack("!I", sent[:4])
    body = sent[4 : 4 + length]
    assert len(body) == length
    assert json.loads(body) == {"cmd": "health_check"}


def test_guest_rpc_call_merges_args_into_payload(monkeypatch):
    fake = FakeSocket([{"status": "ok", "data": {}}])
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: fake)

    rpc = GuestRpc(cid=3)
    rpc.call("run_benchmark", {"name": "stress-ng", "args": ["--cpu", "4"], "duration_secs": 30})

    sent = bytes(fake.send_buf)
    (length,) = struct.unpack("!I", sent[:4])
    payload = json.loads(sent[4 : 4 + length])
    assert payload == {
        "cmd": "run_benchmark",
        "name": "stress-ng",
        "args": ["--cpu", "4"],
        "duration_secs": 30,
    }


def test_guest_rpc_raises_when_guest_closes_mid_message(monkeypatch):
    class TruncatedSocket(FakeSocket):
        def recv(self, n: int) -> bytes:
            return b""

    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: TruncatedSocket([]))
    rpc = GuestRpc(cid=3)
    with pytest.raises(ConnectionError):
        rpc.call("health_check")
