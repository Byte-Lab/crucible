# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

"""Vsock client used by host-side agents to call the guest agent.

The orchestrator passes a vsock CID into the agent via
``TaskEnvelope.context["vsock_cid"]``. ``ClaudeAgentBase.execute``
instantiates a ``GuestRpc`` and exposes it to subclasses as
``self._guest_rpc`` before ``setup_tools`` runs. Tools that wrap guest
RPC commands (see ``agents/profiler/tools.py``) catch any exception and
return a ``{"status": "error", ...}`` dict so the Claude tool loop can
proceed.
"""
from __future__ import annotations

import json
import socket
import struct
from typing import Any

VSOCK_PORT = 5000
AF_VSOCK = 40


class GuestRpc:
    """Connect-per-call wrapper for the length-prefixed JSON guest protocol.

    Two transports share one framing:
      - vsock (VM lane): construct with ``cid`` — connects AF_VSOCK to
        ``(cid, port)``.
      - TCP (Steam Deck lane): construct with ``host`` — connects AF_INET
        to ``(host, port)``.
    Exactly one of ``cid``/``host`` must be set.
    """

    def __init__(
        self,
        cid: int | None = None,
        host: str | None = None,
        port: int = VSOCK_PORT,
        timeout_secs: float = 30.0,
    ) -> None:
        if (cid is None) == (host is None):
            raise ValueError("GuestRpc requires exactly one of cid (vsock) or host (tcp)")
        self.cid = cid
        self.host = host
        self.port = port
        self.timeout_secs = timeout_secs

    def call(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a single command and return the decoded response payload."""
        payload: dict[str, Any] = {"cmd": cmd}
        if args:
            payload.update(args)

        if self.host is not None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target: Any = (self.host, self.port)
        else:
            sock = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
            target = (self.cid, self.port)
        sock.settimeout(self.timeout_secs)
        try:
            sock.connect(target)
            _send(sock, payload)
            return _recv(sock)
        finally:
            sock.close()


def _send(sock: socket.socket, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    sock.sendall(struct.pack("!I", len(body)) + body)


def _recv(sock: socket.socket) -> dict[str, Any]:
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack("!I", header)
    body = _recv_exact(sock, length)
    return json.loads(body)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("guest closed connection mid-message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
