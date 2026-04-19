from __future__ import annotations

import csv
import io
import os
from typing import Any

from agents.common.tool_registry import ToolRegistry


def parse_mangohud_csv(csv_text: str) -> dict[str, Any]:
    """Parse MangoHud CSV output and compute frame statistics."""
    reader = csv.DictReader(io.StringIO(csv_text))
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
        idx = max(0, int(len(data) * p / 100.0) - 1)
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


def _read_psi_file(path: str) -> str | None:
    """Read a PSI file and return its contents, or None if unavailable."""
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return None


def make_profiler_tools(registry: ToolRegistry, guest_rpc: Any) -> None:
    """Register profiler tools into the given registry."""

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
        system_psi: dict[str, str | None] = {}
        for resource in ("cpu", "memory", "io"):
            proc_path = f"/proc/pressure/{resource}"
            system_psi[resource] = _read_psi_file(proc_path)

        cgroup_psi: dict[str, str | None] = {}
        cgroup_base = "/sys/fs/cgroup/crucible"
        if os.path.isdir(cgroup_base):
            for resource in ("cpu", "memory", "io"):
                cg_path = os.path.join(cgroup_base, f"{resource}.pressure")
                cgroup_psi[resource] = _read_psi_file(cg_path)

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
