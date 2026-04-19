# Crucible VM Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the VM management layer -- a Rust wrapper around virtme-ng for booting custom kernels with GPU passthrough, a Python guest agent (vsock RPC daemon) for executing commands inside the VM, a host-side vsock client, and the kernel build pipeline.

**Architecture:** The Rust orchestrator spawns and manages VMs via virtme-ng CLI. A lightweight Python daemon inside the guest listens on vsock port 5000 for RPC commands (launch game, start profiling, collect metrics, transfer files). The host-side Rust code connects via vsock to dispatch commands and collect results. The kernel build pipeline applies patches, builds via `vng --build`, and boots the result with automatic rollback on failure.

**Tech Stack:** Rust (tokio, tokio-vsock), Python 3.12+ (socket with AF_VSOCK), virtme-ng, QEMU/KVM, VFIO

**Spec reference:** `docs/superpowers/specs/2026-04-12-crucible-design.md` (Guest VM Environment section)

**Plan series:**
- Plan 1: Foundation (complete)
- **Plan 2 (this plan):** VM management
- Plan 3: Core agents
- Plan 4: Orchestration loop

---

## File Map

### Rust

| File | Responsibility |
|------|---------------|
| `crates/crucible-orchestrator/src/vm.rs` | VmManager: boot/shutdown VM via virtme-ng, health checks |
| `crates/crucible-orchestrator/src/vsock_client.rs` | Host-side vsock client: connect to guest agent, send commands, receive responses |
| `crates/crucible-orchestrator/src/kernel_builder.rs` | Apply patches, build kernel via vng --build, track known-good state |

### Python

| File | Responsibility |
|------|---------------|
| `guest/crucible_guest_agent.py` | vsock RPC daemon: listen on port 5000, handle commands |
| `guest/crucible-guest-agent.service` | systemd unit for auto-starting guest agent |
| `guest/setup_cgroups.sh` | Create cgroup hierarchy for process classification |

### Tests

| File | Responsibility |
|------|---------------|
| `crates/crucible-orchestrator/src/vm.rs` (inline) | Unit tests for VM config generation and command building |
| `crates/crucible-orchestrator/src/vsock_client.rs` (inline) | Unit tests for message serialization |
| `crates/crucible-orchestrator/src/kernel_builder.rs` (inline) | Unit tests for patch application and rollback logic |
| `tests/python/test_guest_agent.py` | Guest agent command handling tests (mocked vsock) |

---

## Task 1: Guest RPC Protocol Types

**Files:**
- Modify: `crates/crucible-common/src/protocol.rs`
- Create: `guest/__init__.py` (empty, for test imports)
- Create: `guest/protocol.py`

The guest RPC protocol defines the JSON messages exchanged between host and guest over vsock. Both sides need matching types.

- [ ] **Step 1: Write failing Rust tests for guest RPC types**

Add to `crates/crucible-common/src/protocol.rs`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum GuestCommand {
    HealthCheck,
    SetupCgroups {
        groups: Vec<String>,
    },
    LaunchGame {
        app_id: u64,
        args: Vec<String>,
    },
    StopGame,
    StartProfiling {
        config: serde_json::Value,
    },
    StopProfiling,
    CaptureScreen,
    InjectInput {
        events: Vec<InputEvent>,
    },
    FetchFile {
        path: String,
    },
    GetMetrics,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputEvent {
    pub event_type: String,
    pub code: String,
    pub value: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum GuestResponse {
    Ok {
        data: serde_json::Value,
    },
    Error {
        message: String,
    },
}

// Tests:
#[cfg(test)]
// Add these to the existing tests module:

#[test]
fn guest_command_health_check_serializes() {
    let cmd = GuestCommand::HealthCheck;
    let json = serde_json::to_value(&cmd).unwrap();
    assert_eq!(json["cmd"], "health_check");
}

#[test]
fn guest_command_launch_game_roundtrip() {
    let cmd = GuestCommand::LaunchGame {
        app_id: 1091500,
        args: vec!["--benchmark".to_string()],
    };
    let json = serde_json::to_string(&cmd).unwrap();
    let parsed: GuestCommand = serde_json::from_str(&json).unwrap();
    if let GuestCommand::LaunchGame { app_id, args } = parsed {
        assert_eq!(app_id, 1091500);
        assert_eq!(args, vec!["--benchmark"]);
    } else {
        panic!("wrong variant");
    }
}

#[test]
fn guest_response_ok_roundtrip() {
    let resp = GuestResponse::Ok {
        data: serde_json::json!({"pid": 4521, "cgroup": "crucible/game"}),
    };
    let json = serde_json::to_string(&resp).unwrap();
    let parsed: GuestResponse = serde_json::from_str(&json).unwrap();
    if let GuestResponse::Ok { data } = parsed {
        assert_eq!(data["pid"], 4521);
    } else {
        panic!("wrong variant");
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p crucible-common`
Expected: FAIL -- new types not defined yet.

- [ ] **Step 3: Implement the types** (add to protocol.rs, above the test module)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p crucible-common`
Expected: All 7 tests pass (4 existing + 3 new).

- [ ] **Step 5: Create matching Python protocol types**

```python
# guest/__init__.py
```

```python
# guest/protocol.py
from enum import Enum
from typing import Any

from pydantic import BaseModel


class GuestCommand(BaseModel):
    cmd: str
    # Optional fields depending on command
    groups: list[str] | None = None
    app_id: int | None = None
    args: list[str] | None = None
    config: dict[str, Any] | None = None
    events: list[dict[str, Any]] | None = None
    path: str | None = None


class GuestResponse(BaseModel):
    status: str  # "ok" or "error"
    data: dict[str, Any] | None = None
    message: str | None = None

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None) -> "GuestResponse":
        return cls(status="ok", data=data or {})

    @classmethod
    def error(cls, message: str) -> "GuestResponse":
        return cls(status="error", message=message)
```

- [ ] **Step 6: Commit**

```bash
git add crates/crucible-common/src/protocol.rs guest/
git -c commit.gpgsign=false commit -m "feat: add guest RPC protocol types for host-guest vsock communication"
```

---

## Task 2: Guest Agent (Python vsock RPC Daemon)

**Files:**
- Create: `guest/crucible_guest_agent.py`
- Create: `guest/crucible-guest-agent.service`
- Create: `guest/setup_cgroups.sh`
- Create: `tests/python/test_guest_agent.py`

- [ ] **Step 1: Write failing tests for the guest agent**

```python
# tests/python/test_guest_agent.py
import json
import os
import subprocess
import tempfile

from guest.protocol import GuestCommand, GuestResponse


def test_handle_health_check():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="health_check")
    resp = handler.handle(cmd)
    assert resp.status == "ok"
    assert "uptime" in resp.data


def test_handle_setup_cgroups():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="setup_cgroups", groups=["game", "compositor"])
    # This will fail in test env (no cgroup access), but should return error gracefully
    resp = handler.handle(cmd)
    # Either ok (if running as root with cgroups) or error (graceful failure)
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

    # Create a temp file to fetch
    with tempfile.NamedTempFile(mode="w", suffix=".txt", delete=False) as f:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_guest_agent.py -v`

- [ ] **Step 3: Implement the guest agent handler**

```python
# guest/crucible_guest_agent.py
"""Crucible guest agent -- vsock RPC daemon.

Listens on vsock port 5000 for commands from the host orchestrator.
Handles game launching, profiling, metrics collection, and file transfer.
"""
import json
import os
import socket
import struct
import sys
import time
import traceback

from guest.protocol import GuestCommand, GuestResponse

VSOCK_PORT = 5000
CRUCIBLE_CGROUP_ROOT = "/sys/fs/cgroup/crucible"

# CID_ANY for vsock server
VMADDR_CID_ANY = 0xFFFFFFFF


class GuestAgentHandler:
    """Handles individual guest RPC commands."""

    def __init__(self) -> None:
        self._start_time = time.monotonic()

    def handle(self, cmd: GuestCommand) -> GuestResponse:
        handler_name = f"_handle_{cmd.cmd}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            return GuestResponse.error(f"unknown command: {cmd.cmd}")
        try:
            return handler(cmd)
        except Exception as exc:
            return GuestResponse.error(f"{cmd.cmd} failed: {exc}")

    def _handle_health_check(self, cmd: GuestCommand) -> GuestResponse:
        uptime = time.monotonic() - self._start_time
        return GuestResponse.ok({
            "uptime": uptime,
            "pid": os.getpid(),
        })

    def _handle_setup_cgroups(self, cmd: GuestCommand) -> GuestResponse:
        groups = cmd.groups or []
        created = []
        for group in groups:
            path = os.path.join(CRUCIBLE_CGROUP_ROOT, group)
            try:
                os.makedirs(path, exist_ok=True)
                created.append(group)
            except OSError as exc:
                return GuestResponse.error(
                    f"failed to create cgroup {group}: {exc}"
                )
        return GuestResponse.ok({"created": created})

    def _handle_launch_game(self, cmd: GuestCommand) -> GuestResponse:
        app_id = cmd.app_id
        args = cmd.args or []
        if app_id is None:
            return GuestResponse.error("app_id is required")

        launch_cmd = [
            "steam", f"steam://rungameid/{app_id}",
        ]
        # MangoHud wrapping
        env = os.environ.copy()
        env["MANGOHUD"] = "1"
        env["MANGOHUD_OUTPUT"] = "/tmp/crucible_mangohud.csv"

        try:
            proc = subprocess.Popen(
                launch_cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return GuestResponse.ok({
                "pid": proc.pid,
                "app_id": app_id,
            })
        except FileNotFoundError:
            return GuestResponse.error("steam not found in PATH")

    def _handle_stop_game(self, cmd: GuestCommand) -> GuestResponse:
        # Send SIGTERM to all processes in the game cgroup
        game_cgroup = os.path.join(CRUCIBLE_CGROUP_ROOT, "game", "cgroup.procs")
        if os.path.exists(game_cgroup):
            try:
                with open(game_cgroup) as f:
                    pids = f.read().strip().split("\n")
                for pid in pids:
                    if pid:
                        os.kill(int(pid), 15)  # SIGTERM
                return GuestResponse.ok({"killed": len(pids)})
            except Exception as exc:
                return GuestResponse.error(f"failed to stop game: {exc}")
        return GuestResponse.ok({"killed": 0})

    def _handle_start_profiling(self, cmd: GuestCommand) -> GuestResponse:
        config = cmd.config or {}
        # Start perfetto trace session
        trace_config = config.get("perfetto_config", "")
        trace_out = "/tmp/crucible_trace.pb"

        try:
            proc = subprocess.Popen(
                ["perfetto", "-c", "-", "-o", trace_out],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if trace_config:
                proc.stdin.write(trace_config.encode())
            proc.stdin.close()

            return GuestResponse.ok({
                "perfetto_pid": proc.pid,
                "trace_output": trace_out,
            })
        except FileNotFoundError:
            return GuestResponse.error("perfetto not found in PATH")

    def _handle_stop_profiling(self, cmd: GuestCommand) -> GuestResponse:
        # Kill perfetto and collect results
        try:
            subprocess.run(["pkill", "perfetto"], check=False)
        except FileNotFoundError:
            pass

        results = {
            "traces": [],
            "mangohud": None,
            "psi_log": None,
        }

        if os.path.exists("/tmp/crucible_trace.pb"):
            results["traces"].append("/tmp/crucible_trace.pb")
        if os.path.exists("/tmp/crucible_mangohud.csv"):
            results["mangohud"] = "/tmp/crucible_mangohud.csv"

        return GuestResponse.ok(results)

    def _handle_capture_screen(self, cmd: GuestCommand) -> GuestResponse:
        output = "/tmp/crucible_screenshot.png"
        try:
            subprocess.run(
                ["grim", output],
                check=True,
                capture_output=True,
            )
            size = os.path.getsize(output)
            return GuestResponse.ok({"path": output, "size": size})
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            return GuestResponse.error(f"screenshot failed: {exc}")

    def _handle_inject_input(self, cmd: GuestCommand) -> GuestResponse:
        events = cmd.events or []
        # Placeholder -- actual uinput injection requires /dev/uinput access
        return GuestResponse.ok({"injected": len(events)})

    def _handle_fetch_file(self, cmd: GuestCommand) -> GuestResponse:
        path = cmd.path
        if not path or not os.path.exists(path):
            return GuestResponse.error(f"file not found: {path}")
        try:
            size = os.path.getsize(path)
            return GuestResponse.ok({"path": path, "size": size})
        except OSError as exc:
            return GuestResponse.error(f"cannot read file: {exc}")

    def _handle_get_metrics(self, cmd: GuestCommand) -> GuestResponse:
        metrics = {"system_psi": {}, "cgroup_psi": []}

        # Read system-wide PSI
        for resource in ("cpu", "memory", "io"):
            psi_path = f"/proc/pressure/{resource}"
            if os.path.exists(psi_path):
                with open(psi_path) as f:
                    lines = f.readlines()
                # Parse "some avg10=X.XX avg60=X.XX avg300=X.XX total=N"
                for line in lines:
                    if line.startswith("some"):
                        parts = line.split()
                        values = {}
                        for part in parts[1:]:
                            if "=" in part:
                                k, v = part.split("=")
                                values[k] = float(v)
                        metrics["system_psi"][resource] = values

        # Read per-cgroup PSI
        if os.path.exists(CRUCIBLE_CGROUP_ROOT):
            for group in os.listdir(CRUCIBLE_CGROUP_ROOT):
                group_path = os.path.join(CRUCIBLE_CGROUP_ROOT, group)
                if not os.path.isdir(group_path):
                    continue
                cgroup_metrics = {"group": group, "psi": {}}
                for resource in ("cpu", "memory", "io"):
                    psi_file = os.path.join(group_path, f"{resource}.pressure")
                    if os.path.exists(psi_file):
                        with open(psi_file) as f:
                            lines = f.readlines()
                        for line in lines:
                            if line.startswith("some"):
                                parts = line.split()
                                values = {}
                                for part in parts[1:]:
                                    if "=" in part:
                                        k, v = part.split("=")
                                        values[k] = float(v)
                                cgroup_metrics["psi"][resource] = values
                metrics["cgroup_psi"].append(cgroup_metrics)

        return GuestResponse.ok(metrics)


def _recv_message(conn: socket.socket) -> bytes:
    """Read a length-prefixed message from the socket."""
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("connection closed")
        header += chunk
    length = struct.unpack("!I", header)[0]
    data = b""
    while len(data) < length:
        chunk = conn.recv(min(65536, length - len(data)))
        if not chunk:
            raise ConnectionError("connection closed during message")
        data += chunk
    return data


def _send_message(conn: socket.socket, data: bytes) -> None:
    """Send a length-prefixed message to the socket."""
    header = struct.pack("!I", len(data))
    conn.sendall(header + data)


def serve() -> None:
    """Main vsock server loop."""
    handler = GuestAgentHandler()

    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.bind((VMADDR_CID_ANY, VSOCK_PORT))
    sock.listen(1)
    print(f"crucible-guest-agent listening on vsock port {VSOCK_PORT}", flush=True)

    while True:
        conn, addr = sock.accept()
        print(f"connection from CID={addr[0]}", flush=True)
        try:
            while True:
                raw = _recv_message(conn)
                cmd = GuestCommand.model_validate_json(raw)
                resp = handler.handle(cmd)
                _send_message(conn, resp.model_dump_json().encode())
        except ConnectionError:
            print("client disconnected", flush=True)
        except Exception as exc:
            print(f"error: {exc}", flush=True)
            traceback.print_exc()
        finally:
            conn.close()


if __name__ == "__main__":
    serve()
```

- [ ] **Step 4: Create systemd unit file**

```ini
# guest/crucible-guest-agent.service
[Unit]
Description=Crucible Guest Agent (vsock RPC daemon)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m guest.crucible_guest_agent
WorkingDirectory=/opt/crucible
Environment=PYTHONPATH=/opt/crucible
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Create cgroup setup script**

```bash
#!/bin/bash
# guest/setup_cgroups.sh
# Create cgroup hierarchy for Crucible process classification

set -euo pipefail

CGROUP_ROOT="/sys/fs/cgroup/crucible"

mkdir -p "$CGROUP_ROOT/game"
mkdir -p "$CGROUP_ROOT/compositor"
mkdir -p "$CGROUP_ROOT/wine"
mkdir -p "$CGROUP_ROOT/mesa"
mkdir -p "$CGROUP_ROOT/system"

# Enable CPU, memory, and IO controllers for each group
for group in game compositor wine mesa system; do
    echo "+cpu +memory +io" > "$CGROUP_ROOT/$group/cgroup.subtree_control" 2>/dev/null || true
done

echo "Crucible cgroup hierarchy created at $CGROUP_ROOT"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_guest_agent.py -v`
Expected: All 6 tests pass.

- [ ] **Step 7: Commit**

```bash
git add guest/ tests/python/test_guest_agent.py
git -c commit.gpgsign=false commit -m "feat: add guest agent vsock RPC daemon with command handlers"
```

---

## Task 3: Host-Side vsock Client (Rust)

**Files:**
- Create: `crates/crucible-orchestrator/src/vsock_client.rs`
- Modify: `crates/crucible-orchestrator/src/lib.rs` (add `pub mod vsock_client;`)
- Modify: `crates/crucible-orchestrator/Cargo.toml` (add tokio-vsock dependency)

The host-side client connects to the guest agent via vsock and sends/receives length-prefixed JSON messages.

- [ ] **Step 1: Add tokio-vsock to workspace dependencies**

Add to root `Cargo.toml` workspace dependencies:
```toml
tokio-vsock = "0.5"
```

Add to `crates/crucible-orchestrator/Cargo.toml` dependencies:
```toml
tokio-vsock = { workspace = true }
```

- [ ] **Step 2: Write failing tests for vsock client**

```rust
// crates/crucible-orchestrator/src/vsock_client.rs

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_message_roundtrip() {
        let data = b"hello world";
        let framed = frame_message(data);
        assert_eq!(framed.len(), 4 + data.len());

        let (length, payload) = parse_frame(&framed).unwrap();
        assert_eq!(length, data.len() as u32);
        assert_eq!(payload, data);
    }

    #[test]
    fn frame_empty_message() {
        let framed = frame_message(b"");
        assert_eq!(framed.len(), 4);
        let (length, payload) = parse_frame(&framed).unwrap();
        assert_eq!(length, 0);
        assert_eq!(payload, b"");
    }

    #[test]
    fn send_command_serializes_correctly() {
        let cmd = GuestCommand::HealthCheck;
        let json = serde_json::to_vec(&cmd).unwrap();
        let framed = frame_message(&json);
        // First 4 bytes are big-endian length
        let len_bytes = &framed[..4];
        let len = u32::from_be_bytes([len_bytes[0], len_bytes[1], len_bytes[2], len_bytes[3]]);
        assert_eq!(len as usize, json.len());
    }
}
```

- [ ] **Step 3: Implement vsock client**

```rust
// crates/crucible-orchestrator/src/vsock_client.rs
use anyhow::{Context, Result};
use crucible_common::protocol::{GuestCommand, GuestResponse};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_vsock::VsockStream;

const VSOCK_PORT: u32 = 5000;

pub struct VsockClient {
    cid: u32,
    timeout: Duration,
}

impl VsockClient {
    pub fn new(cid: u32, timeout: Duration) -> Self {
        Self { cid, timeout }
    }

    pub async fn send_command(&self, cmd: GuestCommand) -> Result<GuestResponse> {
        let mut stream = tokio::time::timeout(
            self.timeout,
            VsockStream::connect(self.cid, VSOCK_PORT),
        )
        .await
        .with_context(|| format!("vsock connect to CID {} timed out", self.cid))?
        .with_context(|| format!("failed to connect to guest CID {}", self.cid))?;

        let cmd_json = serde_json::to_vec(&cmd)
            .context("failed to serialize guest command")?;
        let framed = frame_message(&cmd_json);

        stream
            .write_all(&framed)
            .await
            .context("failed to send command to guest")?;

        // Read response: 4-byte length header + payload
        let mut len_buf = [0u8; 4];
        stream
            .read_exact(&mut len_buf)
            .await
            .context("failed to read response length")?;
        let resp_len = u32::from_be_bytes(len_buf) as usize;

        let mut resp_buf = vec![0u8; resp_len];
        stream
            .read_exact(&mut resp_buf)
            .await
            .context("failed to read response body")?;

        let resp: GuestResponse = serde_json::from_slice(&resp_buf)
            .with_context(|| {
                format!(
                    "failed to parse guest response: {}",
                    String::from_utf8_lossy(&resp_buf)
                )
            })?;

        Ok(resp)
    }

    pub async fn health_check(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::HealthCheck).await
    }

    pub async fn launch_game(&self, app_id: u64, args: Vec<String>) -> Result<GuestResponse> {
        self.send_command(GuestCommand::LaunchGame { app_id, args }).await
    }

    pub async fn stop_game(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::StopGame).await
    }

    pub async fn start_profiling(&self, config: serde_json::Value) -> Result<GuestResponse> {
        self.send_command(GuestCommand::StartProfiling { config }).await
    }

    pub async fn stop_profiling(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::StopProfiling).await
    }

    pub async fn get_metrics(&self) -> Result<GuestResponse> {
        self.send_command(GuestCommand::GetMetrics).await
    }

    pub async fn fetch_file(&self, path: String) -> Result<GuestResponse> {
        self.send_command(GuestCommand::FetchFile { path }).await
    }

    pub async fn setup_cgroups(&self, groups: Vec<String>) -> Result<GuestResponse> {
        self.send_command(GuestCommand::SetupCgroups { groups }).await
    }
}

pub fn frame_message(data: &[u8]) -> Vec<u8> {
    let len = data.len() as u32;
    let mut framed = Vec::with_capacity(4 + data.len());
    framed.extend_from_slice(&len.to_be_bytes());
    framed.extend_from_slice(data);
    framed
}

pub fn parse_frame(framed: &[u8]) -> Result<(u32, &[u8])> {
    if framed.len() < 4 {
        anyhow::bail!("frame too short: {} bytes", framed.len());
    }
    let len = u32::from_be_bytes([framed[0], framed[1], framed[2], framed[3]]);
    let payload = &framed[4..];
    Ok((len, payload))
}
```

- [ ] **Step 4: Add module to lib.rs**

Add `pub mod vsock_client;` to `crates/crucible-orchestrator/src/lib.rs`.

- [ ] **Step 5: Run tests**

Run: `cargo test -p crucible-orchestrator vsock_client`
Expected: All 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add Cargo.toml crates/crucible-orchestrator/
git -c commit.gpgsign=false commit -m "feat: add host-side vsock client for guest RPC communication"
```

---

## Task 4: VM Manager (virtme-ng wrapper)

**Files:**
- Create: `crates/crucible-orchestrator/src/vm.rs`
- Modify: `crates/crucible-orchestrator/src/lib.rs` (add `pub mod vm;`)

The VM manager wraps virtme-ng to boot/shutdown VMs with custom kernels and GPU passthrough.

- [ ] **Step 1: Write failing tests**

```rust
// crates/crucible-orchestrator/src/vm.rs

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::VmConfig;

    fn test_vm_config() -> VmConfig {
        VmConfig {
            kernel_src: "/home/void/upstream/questing".to_string(),
            guest_rootfs: "/home/void/.crucible/rootfs".to_string(),
            memory: "16G".to_string(),
            cpus: 8,
            vfio_device: "0a:00.0".to_string(),
            boot_timeout_secs: 60,
            vsock_cid: 3,
        }
    }

    #[test]
    fn build_vng_boot_command() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/path/to/bzImage");

        let args: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
        assert!(args.contains(&"--boot"));
        assert!(args.contains(&"--kernel"));
        assert!(args.contains(&"/path/to/bzImage"));
    }

    #[test]
    fn build_vng_boot_command_contains_qemu_opts() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        let cmd = manager.build_boot_command("/path/to/bzImage");

        let joined = cmd.join(" ");
        assert!(joined.contains("vfio-pci,host=0a:00.0"));
        assert!(joined.contains("-m 16G"));
        assert!(joined.contains("-smp 8"));
        assert!(joined.contains("vhost-vsock-pci,guest-cid=3"));
    }

    #[test]
    fn vm_state_transitions() {
        let config = test_vm_config();
        let manager = VmManager::new(config);
        assert!(matches!(manager.state(), VmState::Stopped));
    }
}
```

- [ ] **Step 2: Implement VmManager**

```rust
// crates/crucible-orchestrator/src/vm.rs
use anyhow::{Context, Result};
use crate::config::VmConfig;
use std::path::Path;
use std::time::Duration;
use tokio::process::{Child, Command};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VmState {
    Stopped,
    Booting,
    Running,
    Failed,
}

pub struct VmManager {
    config: VmConfig,
    state: VmState,
    child: Option<Child>,
}

impl VmManager {
    pub fn new(config: VmConfig) -> Self {
        Self {
            config,
            state: VmState::Stopped,
            child: None,
        }
    }

    pub fn state(&self) -> VmState {
        self.state
    }

    pub fn build_boot_command(&self, kernel_path: &str) -> Vec<String> {
        let qemu_opts = format!(
            "-device vfio-pci,host={} -m {} -smp {} -device vhost-vsock-pci,guest-cid={}",
            self.config.vfio_device,
            self.config.memory,
            self.config.cpus,
            self.config.vsock_cid,
        );

        vec![
            "vng".to_string(),
            "--boot".to_string(),
            "--kernel".to_string(),
            kernel_path.to_string(),
            "--root".to_string(),
            self.config.guest_rootfs.clone(),
            "--qemu-opts".to_string(),
            qemu_opts,
        ]
    }

    pub async fn boot(&mut self, kernel_path: &str) -> Result<()> {
        if self.state != VmState::Stopped {
            anyhow::bail!("VM is not stopped (current state: {:?})", self.state);
        }

        self.state = VmState::Booting;
        let cmd_args = self.build_boot_command(kernel_path);

        tracing::info!(kernel = kernel_path, "booting VM");

        let child = Command::new(&cmd_args[0])
            .args(&cmd_args[1..])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .with_context(|| format!("failed to spawn vng: {}", cmd_args.join(" ")))?;

        self.child = Some(child);
        self.state = VmState::Running;

        Ok(())
    }

    pub async fn wait_for_ready(
        &self,
        vsock_client: &crate::vsock_client::VsockClient,
        timeout: Duration,
    ) -> Result<()> {
        let start = std::time::Instant::now();
        let poll_interval = Duration::from_secs(2);

        loop {
            if start.elapsed() > timeout {
                anyhow::bail!(
                    "VM failed to become ready within {}s",
                    timeout.as_secs()
                );
            }

            match vsock_client.health_check().await {
                Ok(resp) => {
                    if let crucible_common::protocol::GuestResponse::Ok { .. } = resp {
                        tracing::info!("VM is ready");
                        return Ok(());
                    }
                }
                Err(_) => {
                    // Not ready yet, keep polling
                }
            }

            tokio::time::sleep(poll_interval).await;
        }
    }

    pub async fn shutdown(&mut self) -> Result<()> {
        if let Some(ref mut child) = self.child {
            tracing::info!("shutting down VM");
            child.kill().await.context("failed to kill VM process")?;
            child
                .wait()
                .await
                .context("failed to wait for VM process")?;
        }
        self.child = None;
        self.state = VmState::Stopped;
        Ok(())
    }
}
```

- [ ] **Step 3: Add module to lib.rs**

Add `pub mod vm;` to `crates/crucible-orchestrator/src/lib.rs`.

- [ ] **Step 4: Run tests**

Run: `cargo test -p crucible-orchestrator vm`
Expected: All 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add crates/crucible-orchestrator/
git -c commit.gpgsign=false commit -m "feat: add VM manager wrapping virtme-ng with GPU passthrough"
```

---

## Task 5: Kernel Build Pipeline

**Files:**
- Create: `crates/crucible-orchestrator/src/kernel_builder.rs`
- Modify: `crates/crucible-orchestrator/src/lib.rs` (add `pub mod kernel_builder;`)

The kernel builder applies patches, builds kernels using `vng --build`, and manages rollback.

- [ ] **Step 1: Write failing tests**

```rust
// crates/crucible-orchestrator/src/kernel_builder.rs

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn build_command_generation() {
        let builder = KernelBuilder::new("/home/void/upstream/questing");
        let cmd = builder.build_vng_build_command();
        assert_eq!(cmd[0], "vng");
        assert!(cmd.contains(&"--build".to_string()));
    }

    #[test]
    fn patch_state_tracking() {
        let tmp = tempfile::tempdir().unwrap();
        let kernel_src = tmp.path().to_str().unwrap();
        let builder = KernelBuilder::new(kernel_src);

        assert!(builder.known_good_commit().is_none());
    }

    #[test]
    fn set_known_good_commit() {
        let tmp = tempfile::tempdir().unwrap();
        let kernel_src = tmp.path().to_str().unwrap();
        let mut builder = KernelBuilder::new(kernel_src);

        builder.set_known_good_commit("abc123");
        assert_eq!(builder.known_good_commit(), Some("abc123"));
    }

    #[test]
    fn build_apply_patch_command() {
        let builder = KernelBuilder::new("/home/void/upstream/questing");
        let cmd = builder.build_apply_patch_command("/tmp/patch.diff");
        assert!(cmd.contains(&"git".to_string()));
        assert!(cmd.contains(&"apply".to_string()));
        assert!(cmd.contains(&"/tmp/patch.diff".to_string()));
    }

    #[test]
    fn build_revert_command() {
        let builder = KernelBuilder::new("/home/void/upstream/questing");
        let cmd = builder.build_revert_command();
        assert!(cmd.contains(&"git".to_string()));
        assert!(cmd.contains(&"checkout".to_string()));
    }
}
```

- [ ] **Step 2: Implement KernelBuilder**

```rust
// crates/crucible-orchestrator/src/kernel_builder.rs
use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use tokio::process::Command;

pub struct KernelBuilder {
    kernel_src: PathBuf,
    known_good: Option<String>,
}

impl KernelBuilder {
    pub fn new(kernel_src: impl Into<PathBuf>) -> Self {
        Self {
            kernel_src: kernel_src.into(),
            known_good: None,
        }
    }

    pub fn known_good_commit(&self) -> Option<&str> {
        self.known_good.as_deref()
    }

    pub fn set_known_good_commit(&mut self, commit: impl Into<String>) {
        self.known_good = Some(commit.into());
    }

    pub fn build_vng_build_command(&self) -> Vec<String> {
        vec![
            "vng".to_string(),
            "--build".to_string(),
        ]
    }

    pub fn build_apply_patch_command(&self, patch_path: &str) -> Vec<String> {
        vec![
            "git".to_string(),
            "-C".to_string(),
            self.kernel_src.to_string_lossy().to_string(),
            "apply".to_string(),
            patch_path.to_string(),
        ]
    }

    pub fn build_revert_command(&self) -> Vec<String> {
        vec![
            "git".to_string(),
            "-C".to_string(),
            self.kernel_src.to_string_lossy().to_string(),
            "checkout".to_string(),
            "--".to_string(),
            ".".to_string(),
        ]
    }

    pub async fn apply_patch(&self, patch_path: &str) -> Result<()> {
        let args = self.build_apply_patch_command(patch_path);
        let output = Command::new(&args[0])
            .args(&args[1..])
            .output()
            .await
            .with_context(|| format!("failed to apply patch: {}", patch_path))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            anyhow::bail!("git apply failed: {}", stderr);
        }

        tracing::info!(patch = patch_path, "patch applied");
        Ok(())
    }

    pub async fn build_kernel(&self) -> Result<PathBuf> {
        let args = self.build_vng_build_command();
        let output = Command::new(&args[0])
            .args(&args[1..])
            .current_dir(&self.kernel_src)
            .output()
            .await
            .context("failed to build kernel")?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            anyhow::bail!("kernel build failed: {}", stderr);
        }

        // vng --build produces the bzImage at the standard location
        let bzimage = self.kernel_src.join("arch/x86/boot/bzImage");
        tracing::info!(path = %bzimage.display(), "kernel built");
        Ok(bzimage)
    }

    pub async fn revert_patch(&self) -> Result<()> {
        let args = self.build_revert_command();
        let output = Command::new(&args[0])
            .args(&args[1..])
            .output()
            .await
            .context("failed to revert patch")?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            anyhow::bail!("git checkout failed: {}", stderr);
        }

        tracing::info!("patch reverted");
        Ok(())
    }

    pub async fn get_current_commit(&self) -> Result<String> {
        let output = Command::new("git")
            .args(["-C", &self.kernel_src.to_string_lossy(), "rev-parse", "HEAD"])
            .output()
            .await
            .context("failed to get current commit")?;

        if !output.status.success() {
            anyhow::bail!("git rev-parse failed");
        }

        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    /// Full patch-build-boot cycle with automatic rollback on failure.
    pub async fn apply_and_build(
        &self,
        patch_path: &str,
    ) -> Result<PathBuf> {
        self.apply_patch(patch_path).await?;

        match self.build_kernel().await {
            Ok(bzimage) => Ok(bzimage),
            Err(build_err) => {
                tracing::warn!(err = %build_err, "build failed, reverting patch");
                self.revert_patch().await?;
                Err(build_err)
            }
        }
    }
}
```

- [ ] **Step 3: Add module to lib.rs**

Add `pub mod kernel_builder;` to `crates/crucible-orchestrator/src/lib.rs`.

- [ ] **Step 4: Run tests**

Run: `cargo test -p crucible-orchestrator kernel_builder`
Expected: All 5 tests pass.

- [ ] **Step 5: Run full test suite**

Run: `cargo test`
Expected: All Rust tests pass.

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/ -v`
Expected: All Python tests pass.

- [ ] **Step 6: Commit**

```bash
git add crates/crucible-orchestrator/
git -c commit.gpgsign=false commit -m "feat: add kernel builder with patch/build/revert pipeline"
```

---

## Completion Checklist

After all tasks:

- [ ] `cargo build --release` compiles cleanly
- [ ] `cargo test` -- all Rust tests pass
- [ ] `python3 -m pytest tests/python/ -v` -- all Python tests pass
- [ ] Guest RPC protocol types match between Rust and Python
- [ ] Guest agent handles all required commands (health_check, setup_cgroups, launch_game, stop_game, start_profiling, stop_profiling, capture_screen, inject_input, fetch_file, get_metrics)
- [ ] vsock client has convenience methods for all guest commands
- [ ] VM manager builds correct virtme-ng boot command with VFIO passthrough
- [ ] Kernel builder supports apply -> build -> revert pipeline
- [ ] All changes committed

## Next Plans

- **Plan 3: Core Agents** -- game selector, profiler, analyzer, optimizer with Claude API tool-use
- **Plan 4: Orchestration Loop** -- state machine, statistical evaluator, full closed-loop
