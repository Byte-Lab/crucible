# guest/crucible_guest_agent.py
"""Crucible guest agent -- vsock RPC daemon running inside the VM."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

from guest.protocol import GuestCommand, GuestResponse

logger = logging.getLogger(__name__)

VSOCK_PORT = 5000
CGROUP_ROOT = Path("/sys/fs/cgroup/crucible")
_BOOT_TIME: float = time.monotonic()

# Cap fetch_file payloads so a runaway log can't blow up the vsock frame
# (base64 inflates ~33%; 8 MiB raw stays well under any framing concern).
FETCH_FILE_MAX_BYTES = 8 * 1024 * 1024

# Native GPU benchmarks the guest may launch. Kept to an allow-list so the
# host can't be tricked into executing arbitrary binaries via the RPC.
NATIVE_BENCHMARKS = ("vkmark", "glmark2")

# fetch_file may only read under these prefixes. The Claude tool loop
# controls the requested path; without a guard it could exfiltrate
# arbitrary guest files (e.g. /etc/shadow) over vsock.
FETCH_FILE_ALLOWED_PREFIXES = ("/tmp/", "/var/log/crucible/")

# Wall-clock ceiling for a native benchmark run. vkmark's default scene set
# finishes in a couple of minutes; anything past this is hung.
LAUNCH_BENCHMARK_TIMEOUT_SECS = 600


# ---------------------------------------------------------------------------
# Wire protocol helpers
# ---------------------------------------------------------------------------

def _recv_message(conn: socket.socket) -> dict[str, Any]:
    """Read a length-prefixed JSON message from *conn*."""
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("connection closed while reading header")
        header += chunk

    (length,) = struct.unpack("!I", header)
    data = b""
    while len(data) < length:
        chunk = conn.recv(length - len(data))
        if not chunk:
            raise ConnectionError("connection closed while reading payload")
        data += chunk

    return json.loads(data)


def _send_message(conn: socket.socket, data: dict[str, Any]) -> None:
    """Send a length-prefixed JSON message over *conn*."""
    payload = json.dumps(data).encode()
    header = struct.pack("!I", len(payload))
    conn.sendall(header + payload)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

class GuestAgentHandler:
    """Dispatch-based handler for guest RPC commands.

    Each command ``foo`` is routed to ``_handle_foo(cmd)``.
    """

    def handle(self, cmd: GuestCommand) -> GuestResponse:
        method_name = f"_handle_{cmd.cmd}"
        method = getattr(self, method_name, None)
        if method is None:
            return GuestResponse.error(f"unknown command: {cmd.cmd}")
        try:
            return method(cmd)
        except Exception as exc:  # noqa: BLE001
            logger.exception("error handling command %s", cmd.cmd)
            return GuestResponse.error(str(exc))

    # -- individual handlers ------------------------------------------------

    def _handle_health_check(self, cmd: GuestCommand) -> GuestResponse:
        uptime = time.monotonic() - _BOOT_TIME
        return GuestResponse.ok({"uptime": round(uptime, 2), "pid": os.getpid()})

    def _handle_setup_cgroups(self, cmd: GuestCommand) -> GuestResponse:
        groups = cmd.groups or ["game", "compositor", "wine", "mesa", "system"]
        created: list[str] = []
        try:
            for group in groups:
                path = CGROUP_ROOT / group
                path.mkdir(parents=True, exist_ok=True)
                created.append(str(path))
                # Enable controllers best-effort
                ctrl_file = path / "cgroup.subtree_control"
                try:
                    ctrl_file.write_text("+cpu +memory +io\n")
                except OSError:
                    pass
        except OSError as exc:
            return GuestResponse.error(f"cgroup setup failed: {exc}")
        return GuestResponse.ok({"created": created})

    def _handle_launch_game(self, cmd: GuestCommand) -> GuestResponse:
        app_id = cmd.app_id
        if app_id is None:
            return GuestResponse.error("app_id is required")

        env = os.environ.copy()
        env.update({
            "MANGOHUD": "1",
            "MANGOHUD_LOG_LEVEL": "info",
        })

        try:
            proc = subprocess.Popen(
                ["steam", f"steam://rungameid/{app_id}"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return GuestResponse.ok({"pid": proc.pid, "app_id": app_id})
        except FileNotFoundError:
            return GuestResponse.error("steam binary not found")
        except OSError as exc:
            return GuestResponse.error(f"failed to launch game: {exc}")

    def _handle_stop_game(self, cmd: GuestCommand) -> GuestResponse:
        cgroup_procs = CGROUP_ROOT / "game" / "cgroup.procs"
        pids_killed: list[int] = []
        try:
            if not cgroup_procs.exists():
                return GuestResponse.error("game cgroup does not exist")
            raw = cgroup_procs.read_text().strip()
            if not raw:
                return GuestResponse.ok({"killed": []})
            for line in raw.splitlines():
                pid = int(line.strip())
                try:
                    os.kill(pid, signal.SIGTERM)
                    pids_killed.append(pid)
                except ProcessLookupError:
                    pass
        except OSError as exc:
            return GuestResponse.error(f"stop_game failed: {exc}")
        return GuestResponse.ok({"killed": pids_killed})

    def _handle_start_profiling(self, cmd: GuestCommand) -> GuestResponse:
        config = cmd.config or {}
        duration = config.get("duration_s", 30)
        output = config.get("output", "/tmp/crucible_trace.perfetto-trace")
        try:
            proc = subprocess.Popen(
                [
                    "perfetto",
                    "--txt",
                    "-c", "-",
                    "-o", output,
                    "--time", str(duration),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return GuestResponse.ok({"pid": proc.pid, "output": output})
        except FileNotFoundError:
            return GuestResponse.error("perfetto binary not found")
        except OSError as exc:
            return GuestResponse.error(f"failed to start profiling: {exc}")

    def _handle_stop_profiling(self, cmd: GuestCommand) -> GuestResponse:
        try:
            subprocess.run(["pkill", "-f", "perfetto"], check=False)
        except FileNotFoundError:
            pass

        trace_dir = Path("/tmp")
        traces = sorted(trace_dir.glob("crucible_trace*.perfetto-trace"))
        paths = [str(t) for t in traces]
        return GuestResponse.ok({"traces": paths})

    def _handle_capture_screen(self, cmd: GuestCommand) -> GuestResponse:
        config = cmd.config or {}
        output = config.get("output", "/tmp/crucible_screenshot.png")
        try:
            subprocess.run(["grim", output], check=True, capture_output=True)
            return GuestResponse.ok({"path": output})
        except FileNotFoundError:
            return GuestResponse.error("grim binary not found")
        except subprocess.CalledProcessError as exc:
            return GuestResponse.error(f"screenshot failed: {exc.stderr.decode()}")

    def _handle_inject_input(self, cmd: GuestCommand) -> GuestResponse:
        events = cmd.events or []
        # Placeholder for uinput injection
        return GuestResponse.ok({"injected": len(events)})

    def _handle_fetch_file(self, cmd: GuestCommand) -> GuestResponse:
        file_path = cmd.path
        if file_path is None:
            return GuestResponse.error("path is required")
        # Resolve symlinks/.. first so traversal can't escape the allow-list.
        resolved = os.path.realpath(file_path)
        if not any(resolved.startswith(p) for p in FETCH_FILE_ALLOWED_PREFIXES):
            return GuestResponse.error(
                f"path not allowed: {file_path} (allowed prefixes: "
                f"{', '.join(FETCH_FILE_ALLOWED_PREFIXES)})"
            )
        p = Path(resolved)
        if not p.exists():
            return GuestResponse.error(f"file not found: {file_path}")
        try:
            size = p.stat().st_size
            with p.open("rb") as fh:
                contents = fh.read(FETCH_FILE_MAX_BYTES)
            return GuestResponse.ok({
                "path": str(p),
                "size": size,
                "truncated": size > FETCH_FILE_MAX_BYTES,
                "contents_b64": base64.b64encode(contents).decode("ascii"),
            })
        except OSError as exc:
            return GuestResponse.error(f"cannot read file: {exc}")

    def _handle_get_metrics(self, cmd: GuestCommand) -> GuestResponse:
        system_psi = _read_system_psi()
        cgroup_psi = _read_cgroup_psi()
        return GuestResponse.ok({
            "system_psi": system_psi,
            "cgroup_psi": cgroup_psi,
        })

    def _handle_run_benchmark(self, cmd: GuestCommand) -> GuestResponse:
        name = cmd.name or "stress-ng"
        if name != "stress-ng":
            return GuestResponse.error(f"unsupported benchmark: {name}")
        if cmd.duration_secs is None or cmd.duration_secs <= 0:
            return GuestResponse.error("duration_secs must be a positive integer")
        duration = cmd.duration_secs
        extra_args = cmd.args or []

        psi_pre = _read_system_psi_avg10()
        try:
            proc = subprocess.run(
                [
                    "stress-ng",
                    "--metrics-brief",
                    "--timeout",
                    f"{duration}s",
                    *extra_args,
                ],
                capture_output=True,
                text=True,
                timeout=duration + 30,
            )
        except FileNotFoundError:
            return GuestResponse.error("stress-ng binary not found")
        except subprocess.TimeoutExpired as exc:
            return GuestResponse.error(f"stress-ng exceeded wall clock timeout: {exc}")
        psi_post = _read_system_psi_avg10()

        metrics = _parse_stress_ng_metrics(proc.stderr or "")
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])

        def delta(resource: str) -> float:
            before = psi_pre.get(resource, 0.0)
            after = psi_post.get(resource, 0.0)
            return max(0.0, after - before)

        return GuestResponse.ok({
            "name": "stress-ng",
            "exit_code": proc.returncode,
            "ops_per_sec": metrics["ops_per_sec"],
            "bogo_ops": metrics["bogo_ops"],
            "real_time_secs": metrics["real_time_secs"],
            "stressors": metrics["stressors"],
            "psi_cpu_delta": delta("cpu"),
            "psi_memory_delta": delta("memory"),
            "psi_io_delta": delta("io"),
            "psi_pre": psi_pre,
            "psi_post": psi_post,
            "raw_stderr_tail": stderr_tail,
        })

    def _handle_launch_benchmark(self, cmd: GuestCommand) -> GuestResponse:
        name = cmd.name or ""
        if name not in NATIVE_BENCHMARKS:
            return GuestResponse.error(
                f"unsupported benchmark: {name!r} (allowed: {', '.join(NATIVE_BENCHMARKS)})"
            )
        if not cmd.mangohud_output:
            return GuestResponse.error("mangohud_output is required")

        # Write-side counterpart of the fetch_file read guard: this path is
        # mkdir'd and rename-targeted, so an unrestricted value is an
        # arbitrary file write. Resolve symlinks/.. before checking.
        resolved_output = os.path.realpath(cmd.mangohud_output)
        if not any(
            resolved_output.startswith(p) for p in FETCH_FILE_ALLOWED_PREFIXES
        ):
            return GuestResponse.error(
                f"mangohud_output not allowed: {cmd.mangohud_output} "
                f"(allowed prefixes: {', '.join(FETCH_FILE_ALLOWED_PREFIXES)})"
            )
        output_path = Path(resolved_output)
        output_dir = output_path.parent
        # MANGOHUD_CONFIG is comma-separated; a comma in the folder path
        # would silently corrupt the whole config string.
        if "," in str(output_dir):
            return GuestResponse.error(
                f"mangohud_output dir must not contain a comma: {output_dir}"
            )
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return GuestResponse.error(f"cannot create output dir: {exc}")

        # MangoHud has no fixed-output-file option: it writes
        # <app>_<timestamp>.csv into output_folder. Snapshot the folder
        # before the run so we can identify the new log and rename it to
        # the deterministic path the host asked for.
        pre_existing = set(output_dir.glob("*.csv"))

        env = os.environ.copy()
        env.update({
            "MANGOHUD": "1",
            "MANGOHUD_CONFIG": (
                f"output_folder={output_dir},autostart_log=1,log_duration=0,no_display"
            ),
        })

        psi_pre = _read_system_psi_avg10()
        try:
            proc = subprocess.run(
                [name, *(cmd.args or [])],
                env=env,
                capture_output=True,
                text=True,
                timeout=LAUNCH_BENCHMARK_TIMEOUT_SECS,
            )
        except FileNotFoundError:
            return GuestResponse.error(f"{name} binary not found")
        except subprocess.TimeoutExpired:
            return GuestResponse.error(
                f"{name} exceeded wall clock timeout of {LAUNCH_BENCHMARK_TIMEOUT_SECS}s"
            )
        psi_post = _read_system_psi_avg10()

        new_logs = sorted(
            set(output_dir.glob("*.csv")) - pre_existing,
            key=lambda p: p.stat().st_mtime,
        )
        log_found = bool(new_logs)
        if log_found:
            try:
                new_logs[-1].rename(output_path)
            except OSError as exc:
                return GuestResponse.error(f"cannot move MangoHud log: {exc}")

        def delta(resource: str) -> float:
            before = psi_pre.get(resource, 0.0)
            after = psi_post.get(resource, 0.0)
            return max(0.0, after - before)

        return GuestResponse.ok({
            "name": name,
            "exit_code": proc.returncode,
            "mangohud_output": str(output_path),
            "log_found": log_found,
            "psi_cpu_delta": delta("cpu"),
            "psi_memory_delta": delta("memory"),
            "psi_io_delta": delta("io"),
            "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-20:]),
            "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-20:]),
        })


# ---------------------------------------------------------------------------
# PSI helpers
# ---------------------------------------------------------------------------

def _read_system_psi() -> dict[str, str]:
    """Read /proc/pressure/* and return raw contents keyed by resource."""
    result: dict[str, str] = {}
    pressure_dir = Path("/proc/pressure")
    if not pressure_dir.exists():
        return result
    for resource in ("cpu", "memory", "io"):
        path = pressure_dir / resource
        if path.exists():
            try:
                result[resource] = path.read_text().strip()
            except OSError:
                pass
    return result


# PSI "some" line shape: `some avg10=0.12 avg60=0.34 avg300=0.56 total=12345`
_PSI_SOME_RE = re.compile(r"some\s+avg10=([\d.]+)")

# stress-ng --metrics-brief writes lines like:
#   stress-ng: metrc: [12345] cpu  4516400  30.00  119.59  0.05  150546.67  37766.36
# Columns: stressor, bogo_ops, real_time_s, usr_time_s, sys_time_s,
#          ops_per_sec_real, ops_per_sec_usr_sys
_STRESS_NG_METRIC_RE = re.compile(
    r"stress-ng:\s+metrc:\s+\[\d+\]\s+(\S+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
)


def _read_system_psi_avg10() -> dict[str, float]:
    """Return PSI `some avg10` per resource as a dict keyed by cpu/memory/io."""
    result: dict[str, float] = {}
    pressure_dir = Path("/proc/pressure")
    if not pressure_dir.exists():
        return result
    for resource in ("cpu", "memory", "io"):
        path = pressure_dir / resource
        if not path.exists():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            m = _PSI_SOME_RE.match(line.strip())
            if m:
                result[resource] = float(m.group(1))
                break
    return result


def _parse_stress_ng_metrics(stderr: str) -> dict[str, Any]:
    """Parse `stress-ng --metrics-brief` stderr into ops/sec aggregates.

    Returns a dict with totals across all stressors and a per-stressor list.
    Stressors run in parallel so real_time is the max, not the sum.
    """
    total_ops = 0
    total_real_time = 0.0
    total_ops_per_sec = 0.0
    stressors: list[dict[str, Any]] = []
    header_seen = False
    for line in stderr.splitlines():
        # Skip the two header rows: they contain "bogo ops" without numeric
        # columns and would otherwise be misread by a loose regex. The
        # metric data lines always come after them.
        if "bogo ops" in line and "stressor" in line:
            header_seen = True
            continue
        if not header_seen:
            continue
        m = _STRESS_NG_METRIC_RE.search(line)
        if not m:
            continue
        name, bogo_ops, real_time, _usr, _sys, ops_real, _ops_usr_sys = m.groups()
        ops = int(bogo_ops)
        rt = float(real_time)
        ops_per_sec = float(ops_real)
        stressors.append({
            "stressor": name,
            "bogo_ops": ops,
            "real_time_secs": rt,
            "ops_per_sec": ops_per_sec,
        })
        total_ops += ops
        total_real_time = max(total_real_time, rt)
        total_ops_per_sec += ops_per_sec
    return {
        "bogo_ops": total_ops,
        "real_time_secs": total_real_time,
        "ops_per_sec": total_ops_per_sec,
        "stressors": stressors,
    }


def _read_cgroup_psi() -> dict[str, dict[str, str]]:
    """Read per-cgroup PSI from the crucible cgroup hierarchy."""
    result: dict[str, dict[str, str]] = {}
    if not CGROUP_ROOT.exists():
        return result
    for group_dir in CGROUP_ROOT.iterdir():
        if not group_dir.is_dir():
            continue
        group_name = group_dir.name
        psi: dict[str, str] = {}
        for resource in ("cpu.pressure", "memory.pressure", "io.pressure"):
            path = group_dir / resource
            if path.exists():
                try:
                    psi[resource.split(".")[0]] = path.read_text().strip()
                except OSError:
                    pass
        if psi:
            result[group_name] = psi
    return result


# ---------------------------------------------------------------------------
# vsock server loop
# ---------------------------------------------------------------------------

def serve() -> None:
    """Listen on vsock port 5000 and dispatch RPC commands."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    handler = GuestAgentHandler()

    # AF_VSOCK = 40, VMADDR_CID_ANY = 0xFFFFFFFF
    AF_VSOCK = 40
    VMADDR_CID_ANY = 0xFFFFFFFF

    sock = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((VMADDR_CID_ANY, VSOCK_PORT))
    sock.listen(4)
    logger.info("crucible guest agent listening on vsock port %d", VSOCK_PORT)

    while True:
        conn, addr = sock.accept()
        logger.info("accepted connection from %s", addr)
        try:
            while True:
                raw = _recv_message(conn)
                cmd = GuestCommand.from_dict(raw)
                logger.info("received command: %s", cmd.cmd)
                resp = handler.handle(cmd)
                _send_message(conn, resp.to_dict())
        except ConnectionError:
            logger.info("client disconnected")
        except Exception:
            logger.exception("error in connection handler")
        finally:
            conn.close()


if __name__ == "__main__":
    serve()
