from __future__ import annotations

import csv
import io
import math
import os
from typing import Any

from agents.common.tool_registry import ToolRegistry


def parse_mangohud_csv(csv_text: str) -> dict[str, Any]:
    """Parse MangoHud CSV output and compute frame statistics.

    Real MangoHud logs open with two system-info rows (os/cpu/gpu/... header
    plus its values) before the per-frame ``fps,frametime,...`` header.
    Skip ahead to the frame header so DictReader keys off the right row.
    """
    lines = csv_text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        first_field = line.split(",", 1)[0].strip().lower()
        if first_field in ("fps", "frametime"):
            start = i
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    fps_values: list[float] = []
    frametime_values: list[float] = []

    for row in reader:
        fps_val = row.get("fps")
        ft_val = row.get("frametime")
        if fps_val is not None:
            fps_values.append(float(fps_val))
        if ft_val is not None:
            frametime_values.append(float(ft_val))

    if not fps_values:
        return {
            "frame_count": 0,
            "fps_avg": 0.0,
            "fps_p1": 0.0,
            "fps_min": 0.0,
            "fps_max": 0.0,
            "frametime_p50_ms": 0.0,
            "frametime_p95_ms": 0.0,
            "frametime_p99_ms": 0.0,
        }

    fps_sorted = sorted(fps_values)
    ft_sorted = sorted(frametime_values) if frametime_values else [0.0]
    count = len(fps_sorted)

    def percentile(data: list[float], p: float) -> float:
        # Nearest-rank: ceil(p/100 * n) - 1. The previous floor-based index
        # underestimated high percentiles on small samples (p99 of 4 frames
        # returned the 2nd-worst frame, hiding the stutter spike).
        idx = max(0, math.ceil(len(data) * p / 100.0) - 1)
        return data[idx]

    return {
        "frame_count": count,
        "fps_avg": sum(fps_values) / count,
        "fps_p1": percentile(fps_sorted, 1),
        "fps_min": fps_sorted[0],
        "fps_max": fps_sorted[-1],
        "frametime_p50_ms": percentile(ft_sorted, 50),
        "frametime_p95_ms": percentile(ft_sorted, 95),
        "frametime_p99_ms": percentile(ft_sorted, 99),
    }


def _parse_psi_file(path: str) -> dict[str, float] | None:
    """Read a PSI file and parse avg10/avg60/avg300 values from the 'some' line."""
    try:
        with open(path, "r") as fh:
            for line in fh:
                if line.startswith("some"):
                    values: dict[str, float] = {}
                    for part in line.split()[1:]:
                        if "=" in part:
                            k, v = part.split("=", 1)
                            values[k] = float(v)
                    return values
    except OSError:
        pass
    return None


def make_profiler_tools(registry: ToolRegistry, guest_rpc: Any) -> None:
    """Register profiler tools into the given registry."""
    from agents.profiler.game_tools import make_profiler_game_tools

    make_profiler_game_tools(registry, guest_rpc)

    @registry.tool(description="Start profiling with Perfetto trace collection.")
    def start_profiling(perfetto_config: str = "", duration_secs: int = 30) -> dict:
        if guest_rpc is not None:
            try:
                result = guest_rpc.call("start_profiling", {
                    "perfetto_config": perfetto_config,
                    "duration_secs": duration_secs,
                })
                return {"status": "started", "detail": result}
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
        return {
            "status": "dry_run",
            "message": f"Would start profiling for {duration_secs}s (no guest RPC)",
        }

    @registry.tool(description="Stop an active profiling session.")
    def stop_profiling() -> dict:
        if guest_rpc is not None:
            try:
                result = guest_rpc.call("stop_profiling", {})
                return {"status": "stopped", "detail": result}
            except Exception as exc:
                return {"status": "error", "error": str(exc)}
        return {"status": "dry_run", "message": "Would stop profiling (no guest RPC)"}

    @registry.tool(description="Collect a PSI (Pressure Stall Information) snapshot from /proc/pressure and crucible cgroup.")
    def collect_psi_snapshot() -> dict:
        system_psi: dict[str, dict[str, float] | None] = {}
        for resource in ("cpu", "memory", "io"):
            system_psi[resource] = _parse_psi_file(f"/proc/pressure/{resource}")

        cgroup_psi: list[dict[str, Any]] = []
        cgroup_base = "/sys/fs/cgroup/crucible"
        if os.path.isdir(cgroup_base):
            for group in os.listdir(cgroup_base):
                group_path = os.path.join(cgroup_base, group)
                if not os.path.isdir(group_path):
                    continue
                group_data: dict[str, Any] = {"group": group, "psi": {}}
                for resource in ("cpu", "memory", "io"):
                    psi_file = os.path.join(group_path, f"{resource}.pressure")
                    parsed = _parse_psi_file(psi_file)
                    if parsed is not None:
                        group_data["psi"][resource] = parsed
                cgroup_psi.append(group_data)

        return {"system_psi": system_psi, "cgroup_psi": cgroup_psi}

    @registry.tool(description="Read and parse a MangoHud CSV log file.")
    def read_mangohud_log(log_path: str) -> dict:
        try:
            with open(log_path, "r") as fh:
                csv_text = fh.read()
            return parse_mangohud_csv(csv_text)
        except OSError as exc:
            return {"error": str(exc)}

    @registry.tool(description="Get performance metrics from the guest VM, falling back to local PSI.")
    def get_guest_metrics() -> dict:
        if guest_rpc is not None:
            try:
                return guest_rpc.call("get_metrics", {})
            except Exception:
                pass
        # Fall back to local PSI snapshot
        return collect_psi_snapshot()

    @registry.tool(description=(
        "Run a synthetic benchmark in the guest VM. `name` selects the engine "
        "(only 'stress-ng' is supported today). `args` are passed through to "
        "stress-ng, e.g. ['--cpu', '4']. `duration_secs` caps the run. "
        "Returns ops_per_sec, bogo_ops, real_time_secs, and psi_*_delta."
    ))
    def run_benchmark(name: str, args: list[str], duration_secs: int) -> dict:
        if guest_rpc is None:
            return {
                "status": "dry_run",
                "message": (
                    f"Would run {name} for {duration_secs}s with args {args} "
                    "(no guest RPC)"
                ),
            }
        try:
            return guest_rpc.call("run_benchmark", {
                "name": name,
                "args": args,
                "duration_secs": duration_secs,
            })
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
