# guest/crucible_guest_agent.py
"""Crucible guest agent -- vsock RPC daemon running inside the VM."""

from __future__ import annotations

import base64
import glob
import json
import logging
import os
import pwd
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

# Native GPU benchmarks the guest may launch, mapped to their base argv.
# Kept to an allow-list so the host can't be tricked into executing
# arbitrary binaries via the RPC. vkmark must use the headless winsys: its
# default kms backend presents via raw DRM atomic commits without ever
# creating a VkSwapchainKHR, so MangoHud's QueuePresentKHR hook never fires
# and the frame-time CSV stays empty.
NATIVE_BENCHMARKS = {
    "vkmark": ["vkmark", "--winsys", "headless"],
    "glmark2": ["glmark2"],
}

# Fallback benchmark runtime when the host omits duration_secs; MangoHud's
# log window is derived from it and must elapse before the app exits.
DEFAULT_LAUNCH_BENCHMARK_DURATION_SECS = 10

# fetch_file reads and launch_benchmark writes are confined to these
# prefixes. The Claude tool loop controls the paths; without a guard it
# could exfiltrate or overwrite arbitrary guest files over vsock.
GUEST_FILE_ALLOWED_PREFIXES = ("/tmp/", "/var/log/crucible/")

# Wall-clock ceiling for a native benchmark run. vkmark's default scene set
# finishes in a couple of minutes; anything past this is hung.
LAUNCH_BENCHMARK_TIMEOUT_SECS = 600

# Perfetto trace config (text proto) for kernel-scheduling analysis: the
# analyzer reasons over scheduler switches/wakeups, run-queue behaviour,
# CPU frequency/idle, and IRQ handling to find kernel bottlenecks worth
# patching. Duration is set by the `--time` flag, not here. compact_sched
# keeps the sched stream small enough to fetch over vsock. Requires the
# guest kernel's FTRACE/TRACEPOINTS (present in the test kernel).
PERFETTO_KERNEL_CONFIG = """\
buffers {
  size_kb: 131072
  fill_policy: RING_BUFFER
}
data_sources {
  config {
    name: "linux.ftrace"
    ftrace_config {
      ftrace_events: "sched/sched_switch"
      ftrace_events: "sched/sched_wakeup"
      ftrace_events: "sched/sched_wakeup_new"
      ftrace_events: "sched/sched_waking"
      ftrace_events: "sched/sched_process_exit"
      ftrace_events: "sched/sched_process_free"
      ftrace_events: "task/task_newtask"
      ftrace_events: "task/task_rename"
      ftrace_events: "power/cpu_frequency"
      ftrace_events: "power/cpu_idle"
      ftrace_events: "power/suspend_resume"
      ftrace_events: "irq/irq_handler_entry"
      ftrace_events: "irq/irq_handler_exit"
      ftrace_events: "irq/softirq_entry"
      ftrace_events: "irq/softirq_exit"
      compact_sched {
        enabled: true
      }
    }
  }
}
data_sources {
  config {
    name: "linux.process_stats"
    process_stats_config {
      scan_all_processes_on_start: true
      proc_stats_poll_ms: 1000
    }
  }
}
"""

# --- Steam benchmark (milestone G3) ----------------------------------------
# Verified on RDNA3 passthrough (G3.0 spike): weston headless with the GL
# renderer + Xwayland gives Steam titles the X11 path they expect, and
# MangoHud hooks the present chain through Xwayland.
STEAM_USER = "crucible"
STEAM_HOME = f"/home/{STEAM_USER}"
# Invoke the extracted client's steam.sh directly. The Debian
# /usr/games/steam wrapper must never run headless: it targets
# ~/.steam/debian-installation (not our extracted client) and blocks
# forever on a zenity first-run dialog when its bootstrap is missing.
STEAM_SH = f"{STEAM_HOME}/.local/share/Steam/steam.sh"
WESTON_ARGV = ["weston", "--backend=headless", "--renderer=gl", "--xwayland"]
# Route OpenGL titles (Aspyr's Civ 6 port) through zink → RADV Vulkan.
# RADV Vulkan is proven on this card (vkmark renders thousands of fps),
# whereas native radeonsi GL under Xwayland's headless glamor is the weak
# link that leaves Aspyr's GL layer presenting no frames. zink is a no-op
# for Vulkan-native titles (they bind RADV directly, never the GL driver).
# GALLIUM_DRIVER is not forced (that would also hijack the compositor's own
# GL); the loader override + GLX vendor are enough to steer the app's GL.
STEAM_GL_ZINK_ENV = {
    "MESA_LOADER_DRIVER_OVERRIDE": "zink",
    "__GLX_VENDOR_LIBRARY_NAME": "mesa",
}
XDG_RUNTIME_DIR = "/run/crucible-xdg"
# The Steam client must be fully up (boot + CM logon + library reconcile)
# before -applaunch is issued: a launch bundled with the client's own
# startup gets dropped when the client restarts through its update flow
# (observed 2026-07-01 — client came up, game never started). The client
# may also restart itself once to self-update on first boot, so liveness
# is polled until it stays up continuously for STEAM_CLIENT_STABLE_SECS.
STEAM_CLIENT_SETTLE_SECS = 300
STEAM_CLIENT_STABLE_SECS = 45
STEAM_CLIENT_POLL_SECS = 5
# MangoHud's autostart_log value is a delay in seconds from process start;
# this skips the load screen so the log window measures menu rendering,
# not asset streaming.
STEAM_LOG_START_DELAY_SECS = 60
# Between -applaunch and first frames come Steam's Fossilize shader
# pre-processing (~3 min per boot — the shadercache lives in the ephemeral
# overlay) and the 68G asset load over 9p. The CSV wait budget is
# log-start delay + duration + this grace.
STEAM_LAUNCH_GRACE_SECS = 600
WESTON_WARMUP_SECS = 6
# Steam's CM logon needs a route out. vng's slirp netdev provides one,
# but the minimal rootfs runs no network manager — the agent DHCPs the
# interface itself before launching the client.
NETWORK_DHCP_TIMEOUT_SECS = 30
# traced needs a beat to create its socket before the perfetto client
# connects; traced_probes registers the ftrace data source in that window.
PERFETTO_DAEMON_WARMUP_SECS = 2
# Dotted sysctl names only — the apply_sysctls guard against traversal
# out of /proc/sys (keys arrive from the optimizer's model output).
_SYSCTL_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
# High-impact tunables that live under /sys, not /proc/sys: transparent
# hugepages, memory-management knobs, and CPU frequency governor. The
# apply_sysctls guard confines writes to these prefixes.
_SYS_TUNABLE_PREFIXES = (
    "/sys/kernel/mm/",
    "/sys/devices/system/cpu/",
)


def _has_default_route(route_path: str = "/proc/net/route") -> bool:
    """True when the kernel routing table has a default (0.0.0.0) route."""
    try:
        with open(route_path, encoding="ascii") as f:
            lines = f.readlines()[1:]
    except OSError:
        return False
    return any(
        len(fields) > 1 and fields[1] == "00000000"
        for fields in (line.split() for line in lines)
    )


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

    def _handle_apply_sysctls(self, cmd: GuestCommand) -> GuestResponse:
        """Apply optimizer-proposed sysctl tunings before the comparison run.

        config = {"sysctls": {"kernel.sched_base_slice_ns": "1500000", ...}}.
        Keys are dotted sysctl names; values written as strings. Reports
        applied and failed maps — a failed key (e.g. the knob's patch didn't
        build) must not crash the cycle.
        """
        sysctls = (cmd.config or {}).get("sysctls") or {}
        if not isinstance(sysctls, dict) or not sysctls:
            return GuestResponse.error("config.sysctls must be a non-empty dict")
        applied: dict[str, str] = {}
        failed: dict[str, str] = {}
        for key, value in sysctls.items():
            # Keys come from the optimizer's model output over vsock — same
            # trust level as fetch_file paths. Two forms are accepted:
            #   - dotted /proc/sys knob (kernel.x, vm.y)
            #   - an explicit tunable path under the /sys tuning allow-list
            #     (THP, CPU governor, etc. — the high-impact knobs that are
            #     NOT in /proc/sys).
            # Both are guarded against path traversal.
            if not isinstance(key, str):
                failed[str(key)] = "invalid sysctl key"
                continue
            if key.startswith("/sys/"):
                path = key
                if not any(
                    os.path.realpath(path).startswith(p)
                    for p in _SYS_TUNABLE_PREFIXES
                ):
                    failed[key] = "path not under an allowed /sys tuning prefix"
                    continue
            elif _SYSCTL_KEY_RE.match(key):
                path = "/proc/sys/" + key.replace(".", "/")
                if not os.path.realpath(path).startswith("/proc/sys/"):
                    failed[key] = "path escapes /proc/sys"
                    continue
            else:
                failed[key] = "invalid sysctl key"
                continue
            try:
                with open(path, "w", encoding="ascii") as f:
                    f.write(str(value))
                with open(path, "r", encoding="ascii") as f:
                    applied[key] = f.read().strip()
            except OSError as exc:
                failed[key] = str(exc)
        return GuestResponse.ok({"applied": applied, "failed": failed})

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

    def _ensure_perfetto_daemons(self) -> GuestResponse | None:
        """Start traced + traced_probes (idempotent) for the perfetto client.

        The `perfetto` CLI is only a consumer: it connects to the traced
        service socket, and traced_probes is what actually programs ftrace.
        The minimal guest has no init service for them.
        """
        daemons = getattr(self, "_perfetto_daemons", None)
        if daemons and all(p.poll() is None for p in daemons):
            return None
        started = []
        for binary in ("traced", "traced_probes"):
            log_path = f"/tmp/crucible_{binary}.log"
            try:
                log_file = open(log_path, "wb")
                started.append(
                    subprocess.Popen(
                        [binary],
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                    )
                )
            except FileNotFoundError:
                return GuestResponse.error(f"{binary} not found in guest")
            except OSError as exc:
                return GuestResponse.error(f"cannot start {binary}: {exc}")
        self._perfetto_daemons = started
        time.sleep(PERFETTO_DAEMON_WARMUP_SECS)
        dead = [p for p in started if p.poll() is not None]
        if dead:
            # Surface each dead daemon's captured output — a DEVNULL here
            # already cost one blind debugging cycle.
            details = []
            for p in dead:
                name = p.args[0] if isinstance(p.args, list) else str(p.args)
                tail = ""
                try:
                    with open(f"/tmp/crucible_{name}.log", "rb") as f:
                        tail = f.read()[-500:].decode("utf-8", errors="replace")
                except OSError:
                    pass
                details.append(f"{name} (exit {p.returncode}): {tail.strip()}")
            return GuestResponse.error(
                "perfetto daemon(s) exited at start: " + " | ".join(details)
            )
        return None

    def _handle_start_profiling(self, cmd: GuestCommand) -> GuestResponse:
        # duration_secs is a first-class wire field; config carries the
        # optional output path / custom trace_config.
        config = cmd.config or {}
        duration = cmd.duration_secs or config.get("duration_s", 30)
        output = config.get("output", "/tmp/crucible_trace.perfetto-trace")
        daemon_error = self._ensure_perfetto_daemons()
        if daemon_error is not None:
            return daemon_error
        # `perfetto -c -` reads a text-proto config from stdin; without one
        # it records nothing. Feed the kernel-scheduling config (overridable
        # via config["trace_config"] for a custom capture). The CLI rejects
        # -c together with --time, so the capture bound (protecting against
        # a lost stop_profiling) is a duration_ms line inside the config.
        trace_config = config.get("trace_config", PERFETTO_KERNEL_CONFIG)
        trace_config = f"duration_ms: {int(duration) * 1000}\n" + trace_config
        try:
            client_log = open("/tmp/crucible_perfetto.log", "wb")
            proc = subprocess.Popen(
                [
                    "perfetto",
                    "--txt",
                    "-c", "-",
                    "-o", output,
                ],
                stdin=subprocess.PIPE,
                stdout=client_log,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError:
            return GuestResponse.error("perfetto binary not found")
        except OSError as exc:
            return GuestResponse.error(f"failed to start profiling: {exc}")
        try:
            assert proc.stdin is not None
            proc.stdin.write(trace_config.encode("utf-8"))
            proc.stdin.close()
        except OSError as exc:
            proc.kill()
            return GuestResponse.error(f"failed to send perfetto config: {exc}")
        return GuestResponse.ok({"pid": proc.pid, "output": output})

    def _handle_stop_profiling(self, cmd: GuestCommand) -> GuestResponse:
        # SIGTERM makes the perfetto client end the capture and WRITE the
        # trace file (it only materializes at capture end). Wait for the
        # client to actually exit so the flush is complete before the host
        # fetches. -x avoids TERMing traced/traced_probes or ourselves.
        try:
            subprocess.run(["pkill", "-x", "perfetto"], check=False)
        except FileNotFoundError:
            pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            probe = subprocess.run(["pgrep", "-x", "perfetto"], capture_output=True)
            if probe.returncode != 0:
                break
            time.sleep(0.5)

        trace_dir = Path("/tmp")
        traces = sorted(trace_dir.glob("crucible_trace*.perfetto-trace"))
        paths = [str(t) for t in traces]
        sizes = {str(t): t.stat().st_size for t in traces}
        return GuestResponse.ok({"traces": paths, "sizes": sizes})

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
        if not any(resolved.startswith(p) for p in GUEST_FILE_ALLOWED_PREFIXES):
            return GuestResponse.error(
                f"path not allowed: {file_path} (allowed prefixes: "
                f"{', '.join(GUEST_FILE_ALLOWED_PREFIXES)})"
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

        # stress-ng writes --metrics-brief to stderr in some versions
        # (0.20+) and stdout in others (0.19 in the trixie rootfs). Parse
        # both so the ops/sec metric survives a version bump.
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        metrics = _parse_stress_ng_metrics(combined)
        stderr_tail = "\n".join(combined.splitlines()[-20:])

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

    def _ensure_gpu_module(self) -> None:
        """Load amdgpu and wait briefly for the render node.

        vng --exec boots the agent directly: no systemd, no udev coldplug,
        so nothing auto-loads the GPU driver for a passed-through device.
        Idempotent; failure is tolerated (no GPU → llvmpipe still renders
        via the headless winsys). amdgpu's probe of a real GPU takes
        seconds; without the wait Vulkan silently enumerates llvmpipe.
        """
        try:
            subprocess.run(
                ["modprobe", "amdgpu"],
                capture_output=True,
                timeout=60,
                check=False,
            )
            for _ in range(20):
                if glob.glob("/dev/dri/renderD*"):
                    break
                time.sleep(0.5)
        except Exception:
            pass

    def _validated_mangohud_output(
        self, cmd: GuestCommand
    ) -> GuestResponse | tuple[Path, Path]:
        """Validate cmd.mangohud_output; (output_path, output_dir) or error.

        Write-side counterpart of the fetch_file read guard: this path is
        mkdir'd and rename-targeted, so an unrestricted value is an
        arbitrary file write. Resolve symlinks/.. before checking.
        """
        if not cmd.mangohud_output:
            return GuestResponse.error("mangohud_output is required")
        resolved_output = os.path.realpath(cmd.mangohud_output)
        if not any(
            resolved_output.startswith(p) for p in GUEST_FILE_ALLOWED_PREFIXES
        ):
            return GuestResponse.error(
                f"mangohud_output not allowed: {cmd.mangohud_output} "
                f"(allowed prefixes: {', '.join(GUEST_FILE_ALLOWED_PREFIXES)})"
            )
        output_path = Path(resolved_output)
        output_dir = output_path.parent
        # MANGOHUD_CONFIG is a comma-separated key=value string; either
        # character in the folder path silently corrupts the whole config.
        if "," in str(output_dir) or "=" in str(output_dir):
            return GuestResponse.error(
                f"mangohud_output dir must not contain ',' or '=': {output_dir}"
            )
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return GuestResponse.error(f"cannot create output dir: {exc}")
        return output_path, output_dir

    def _handle_launch_benchmark(self, cmd: GuestCommand) -> GuestResponse:
        name = cmd.name or ""
        if name not in NATIVE_BENCHMARKS:
            return GuestResponse.error(
                f"unsupported benchmark: {name!r} (allowed: {', '.join(NATIVE_BENCHMARKS)})"
            )
        validated = self._validated_mangohud_output(cmd)
        if isinstance(validated, GuestResponse):
            return validated
        output_path, output_dir = validated

        # MangoHud has no fixed-output-file option: it writes
        # <app>_<timestamp>.csv into output_folder. Snapshot the folder
        # before the run so we can identify the new log and rename it to
        # the deterministic path the host asked for.
        pre_existing = set(output_dir.glob("*.csv"))

        # MangoHud only writes the CSV when logging *stops* while the app is
        # still alive: log_duration=0 never stops, and no_display suppresses
        # the HUD update loop that feeds the logger — both leave the file
        # unwritten. Finite window, ends 2s before the benchmark does.
        duration = cmd.duration_secs or DEFAULT_LAUNCH_BENCHMARK_DURATION_SECS
        log_duration = max(1, duration - 2)
        env = os.environ.copy()
        env.update({
            "MANGOHUD": "1",
            "MANGOHUD_CONFIG": (
                f"output_folder={output_dir},autostart_log=1,"
                f"log_duration={log_duration},log_interval=100"
            ),
        })

        self._ensure_gpu_module()

        # Optional CPU co-load: on a few-vCPU guest an idle system gives
        # scheduler/CPU sysctls nothing to arbitrate, so tunings show no
        # measurable effect. A stress-ng background load makes the scheduler
        # contended, so a tuning that shifts CPU toward the benchmark moves
        # its frame rate. Runs for the benchmark window + margin, then is
        # killed. Present in both baseline and comparison, so it's a constant
        # backdrop — only the sysctl tuning differs between phases.
        coload = getattr(cmd, "coload_cpu", None) or 0
        coload_proc = None
        if coload > 0:
            try:
                coload_proc = subprocess.Popen(
                    ["stress-ng", "--cpu", str(coload),
                     "--timeout", f"{duration + 10}s"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                logging.warning("coload requested but stress-ng not found")

        psi_pre = _read_system_psi_avg10()
        try:
            proc = subprocess.run(
                [*NATIVE_BENCHMARKS[name], *(cmd.args or [])],
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
        finally:
            if coload_proc is not None:
                coload_proc.terminate()
                try:
                    coload_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    coload_proc.kill()
                subprocess.run(["pkill", "-x", "stress-ng"], check=False)
        psi_post = _read_system_psi_avg10()

        # MangoHud writes two CSVs: the per-frame log and a *_summary.csv
        # with aggregates, written last (= newest). The profiler's parser
        # needs the frame log; shipping the summary yields zero frames.
        new_logs = sorted(
            (
                p
                for p in set(output_dir.glob("*.csv")) - pre_existing
                if not p.name.endswith("_summary.csv")
            ),
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

    def _ensure_weston(self) -> GuestResponse | dict[str, str]:
        """Start weston headless (idempotent) and return the client env.

        Recipe verified on RDNA3 passthrough: headless backend + GL
        renderer + Xwayland; clients reach it via WAYLAND_DISPLAY/DISPLAY
        inside a private XDG_RUNTIME_DIR.

        weston MUST run as the same unprivileged user as the Steam client
        (the agent itself is root). If weston runs as root, its Wayland
        socket and the Xwayland X-auth cookie live under root's ownership
        and the crucible-user Steam client gets "Unable to open display"
        and segfaults on its post-update re-exec (observed 2026-07-01).
        """
        client_env = {
            "XDG_RUNTIME_DIR": XDG_RUNTIME_DIR,
            "WAYLAND_DISPLAY": "wayland-1",
            "DISPLAY": ":0",
        }
        weston = getattr(self, "_weston_proc", None)
        if weston is not None and weston.poll() is None:
            return client_env
        try:
            steam_pw = pwd.getpwnam(STEAM_USER)
        except KeyError:
            return GuestResponse.error(f"steam user {STEAM_USER} not found")
        try:
            os.makedirs(XDG_RUNTIME_DIR, mode=0o700, exist_ok=True)
            os.chown(XDG_RUNTIME_DIR, steam_pw.pw_uid, steam_pw.pw_gid)
            os.chmod(XDG_RUNTIME_DIR, 0o700)
        except OSError as exc:
            return GuestResponse.error(f"cannot prepare XDG_RUNTIME_DIR: {exc}")
        # Launch weston as the steam user so the display it creates is
        # owned by (and reachable by) that same user.
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = XDG_RUNTIME_DIR
        env["HOME"] = STEAM_HOME
        env["USER"] = STEAM_USER
        weston_argv = ["runuser", "-u", STEAM_USER, "--", *WESTON_ARGV]
        try:
            self._weston_proc = subprocess.Popen(
                weston_argv,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return GuestResponse.error("runuser or weston binary not found")
        time.sleep(WESTON_WARMUP_SECS)
        if self._weston_proc.poll() is not None:
            return GuestResponse.error(
                f"weston exited immediately (code {self._weston_proc.returncode})"
            )
        return client_env

    def _ensure_network(self) -> GuestResponse | None:
        """Bring up DHCP on the first non-lo interface (idempotent).

        Returns an error response when no route can be established —
        without one the Steam client sits at the login screen forever,
        which would otherwise surface as an opaque CSV timeout.
        """
        if _has_default_route():
            return None
        try:
            ifaces = sorted(
                name for name in os.listdir("/sys/class/net") if name != "lo"
            )
        except OSError as exc:
            return GuestResponse.error(f"cannot list network interfaces: {exc}")
        if not ifaces:
            return GuestResponse.error("no network interface for Steam logon")
        try:
            proc = subprocess.run(
                ["dhclient", ifaces[0]],
                capture_output=True,
                text=True,
                timeout=NETWORK_DHCP_TIMEOUT_SECS,
            )
        except FileNotFoundError:
            return GuestResponse.error("dhclient not found in guest")
        except subprocess.TimeoutExpired:
            return GuestResponse.error(
                f"dhclient {ifaces[0]} timed out after {NETWORK_DHCP_TIMEOUT_SECS}s"
            )
        if proc.returncode != 0:
            return GuestResponse.error(
                f"dhclient {ifaces[0]} failed: {(proc.stderr or '').strip()[:200]}"
            )
        return None

    @staticmethod
    def _steam_client_running() -> bool:
        """True when the Steam client binary is alive for the steam user.

        The steam.sh wrapper exits 0 once the client daemonizes, so the
        wrapper's Popen handle says nothing about client liveness — probe
        the `steam` process itself.
        """
        try:
            probe = subprocess.run(
                ["pgrep", "-u", STEAM_USER, "-x", "steam"],
                capture_output=True,
            )
        except FileNotFoundError:
            return False
        return probe.returncode == 0

    @staticmethod
    def _raise_game_sysctls() -> None:
        """Raise kernel limits modern Steam titles need (best-effort).

        Dota 2 under the Steam Linux Runtime maps far more than the
        default 65530 VMAs (RADV + pressure-vessel + Fossilize); the
        minimal guest ships the stock default, so mmap fails with
        "Cannot allocate memory" and the game never spawns. Steam Deck
        and gaming distros ship 1048576+. overcommit_memory=1 avoids
        heuristic rejection of the game's large sparse reservations.
        """
        for key, value in (
            ("/proc/sys/vm/max_map_count", "1048576"),
            ("/proc/sys/vm/overcommit_memory", "1"),
        ):
            try:
                with open(key, "w", encoding="ascii") as f:
                    f.write(value)
            except OSError as exc:
                logging.warning("could not set %s: %s", key, exc)

    def _ensure_steam_client(
        self, env: dict[str, str], launch_argv: list[str] | None = None
    ) -> GuestResponse | None:
        """Start the Steam client headless and let it settle (idempotent).

        steamcmd/steam refuse to run usefully as root (G3.0 spike: error
        13 creating caches); the agent runs as root, so drop to the steam
        user. The client stays resident across calls — the VM is rebooted
        between kernel builds anyway. The extracted client's steam.sh is
        invoked directly, never the Debian wrapper (zenity hang, wrong
        STEAMDIR).

        `launch_argv` (e.g. ["-applaunch", "570", "-novid"]) is appended to
        the FIRST client start. Verified 2026-07-01: the game only actually
        launches when -applaunch rides the client's own startup — a bare
        `-silent` client that receives the launch only via a later IPC
        applaunch never spawns the game. The MangoHud env the game inherits
        also has to be on this first start, not just the IPC call.
        """
        if self._steam_client_running():
            return None
        argv = ["runuser", "-u", STEAM_USER, "--", STEAM_SH, "-silent"]
        if launch_argv:
            argv.extend(launch_argv)
        try:
            subprocess.Popen(
                argv,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return GuestResponse.error("runuser or steam.sh not found")
        # Boot + CM logon + library reconcile, plus a possible one-time
        # self-update restart. An -applaunch issued before the client is
        # stably up is silently dropped by the update flow, so poll until
        # the client process stays alive continuously for the stable
        # window (not just present at a single instant).
        deadline = time.monotonic() + STEAM_CLIENT_SETTLE_SECS
        stable_since: float | None = None
        while time.monotonic() < deadline:
            if self._steam_client_running():
                now = time.monotonic()
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= STEAM_CLIENT_STABLE_SECS:
                    return None
            else:
                # A drop resets the stability clock — the client is mid
                # self-update restart.
                stable_since = None
            time.sleep(STEAM_CLIENT_POLL_SECS)
        return GuestResponse.error(
            f"steam client never stayed up for {STEAM_CLIENT_STABLE_SECS}s "
            f"within {STEAM_CLIENT_SETTLE_SECS}s (crash-looping or update failure)"
        )

    def _handle_launch_steam_benchmark(self, cmd: GuestCommand) -> GuestResponse:
        if cmd.app_id is None:
            return GuestResponse.error("app_id is required")
        validated = self._validated_mangohud_output(cmd)
        if isinstance(validated, GuestResponse):
            return validated
        output_path, output_dir = validated

        net_error = self._ensure_network()
        if net_error is not None:
            return net_error

        self._raise_game_sysctls()
        self._ensure_gpu_module()
        client_env = self._ensure_weston()
        if isinstance(client_env, GuestResponse):
            return client_env

        duration = cmd.duration_secs or DEFAULT_LAUNCH_BENCHMARK_DURATION_SECS
        env = os.environ.copy()
        env.update(client_env)
        # Steer OpenGL titles onto zink→RADV. The game inherits the client's
        # env; the client itself is headless (its GL only draws an unseen UI),
        # so routing it through zink too is harmless. Vulkan-native titles
        # ignore this (they bind RADV directly).
        env.update(STEAM_GL_ZINK_ENV)
        env.update({
            # runuser overrides HOME/USER for the target user, but be
            # explicit: Steam derives all its paths from HOME.
            "HOME": STEAM_HOME,
            "USER": STEAM_USER,
            # The game process inherits the CLIENT's environment, not the
            # -applaunch invocation's — MangoHud config must ride on the
            # client start inside _ensure_steam_client.
            "MANGOHUD": "1",
            "MANGOHUD_CONFIG": (
                f"output_folder={output_dir},"
                f"autostart_log={STEAM_LOG_START_DELAY_SECS},"
                f"log_duration={duration},log_interval=100"
            ),
        })

        launch_argv = ["-applaunch", str(cmd.app_id), *(cmd.args or [])]
        client_error = self._ensure_steam_client(env, launch_argv)
        if client_error is not None:
            return client_error

        pre_existing = set(output_dir.glob("*.csv"))
        psi_pre = _read_system_psi_avg10()

        # Second steam.sh invocation re-sends -applaunch to the running
        # client over its IPC pipe — a retry in case the first (which rode
        # the client startup) was dropped by a self-update restart.
        argv = [
            "runuser", "-u", STEAM_USER, "--",
            STEAM_SH, *launch_argv,
        ]
        try:
            steam_proc = subprocess.Popen(
                argv,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return GuestResponse.error("runuser or steam.sh not found")

        # The CSV appears when MangoHud's log window opens (rows stream in
        # every log_interval; size goes stable once the window closes).
        # Before that come Fossilize shader pre-processing and the asset
        # load, both covered by the grace budget.
        deadline = (
            time.monotonic()
            + STEAM_LOG_START_DELAY_SECS
            + duration
            + STEAM_LAUNCH_GRACE_SECS
        )
        last_size = -1
        frame_log: Path | None = None
        while time.monotonic() < deadline:
            candidates = sorted(
                (
                    p
                    for p in set(output_dir.glob("*.csv")) - pre_existing
                    if not p.name.endswith("_summary.csv")
                ),
                key=lambda p: p.stat().st_mtime,
            )
            if candidates:
                size = candidates[-1].stat().st_size
                if size > 0 and size == last_size:
                    frame_log = candidates[-1]
                    break
                last_size = size
            time.sleep(2)

        psi_post = _read_system_psi_avg10()
        if frame_log is None:
            budget = STEAM_LOG_START_DELAY_SECS + duration + STEAM_LAUNCH_GRACE_SECS
            return GuestResponse.error(
                f"no MangoHud log appeared within {budget}s "
                f"(applaunch pid {steam_proc.pid}, app {cmd.app_id})"
            )
        try:
            frame_log.rename(output_path)
        except OSError as exc:
            return GuestResponse.error(f"cannot move MangoHud log: {exc}")

        def delta(resource: str) -> float:
            before = psi_pre.get(resource, 0.0)
            after = psi_post.get(resource, 0.0)
            return max(0.0, after - before)

        return GuestResponse.ok({
            "app_id": cmd.app_id,
            "steam_pid": steam_proc.pid,
            "mangohud_output": str(output_path),
            "log_found": True,
            "psi_cpu_delta": delta("cpu"),
            "psi_memory_delta": delta("memory"),
            "psi_io_delta": delta("io"),
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
