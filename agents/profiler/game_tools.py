"""Game-mode profiler tools: drive native GPU benchmarks and pull MangoHud logs.

Registered alongside the synthetic tools by ``make_profiler_tools``. The
guest runs vkmark/glmark2 under MangoHud (``launch_benchmark`` RPC) and the
frame-time CSV is fetched back over vsock as base64 (``fetch_file`` RPC) —
the guest agent's fetch_file returns ``contents_b64``/``truncated``, not
just the file size.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any

from agents.common.tool_registry import ToolRegistry


def make_profiler_game_tools(registry: ToolRegistry, guest_rpc: Any) -> None:
    """Register game-mode profiler tools into the given registry."""
    # Imported here to avoid a circular import: tools.py imports this module.
    from agents.profiler.tools import parse_frametime_csv, parse_mangohud_csv

    @registry.tool(description=(
        "Run a native GPU benchmark in the guest VM under MangoHud. `name` is "
        "'vkmark' or 'glmark2'; `args` are passed through to the benchmark; "
        "`mangohud_output` is the guest path where the frame-time CSV will be "
        "written; `duration_secs` is the expected benchmark runtime (the guest "
        "sizes MangoHud's log window from it — keep it equal to the duration "
        "you pass in `args`). Returns exit_code, log_found, and psi_*_delta."
    ))
    def launch_benchmark(
        name: str,
        args: list[str],
        mangohud_output: str,
        duration_secs: int = 10,
        coload_cpu: int = 0,
    ) -> dict:
        if guest_rpc is None:
            return {
                "status": "dry_run",
                "message": (
                    f"Would run {name} with args {args} logging to "
                    f"{mangohud_output} (no guest RPC)"
                ),
            }
        try:
            return guest_rpc.call("launch_benchmark", {
                "name": name,
                "args": args,
                "mangohud_output": mangohud_output,
                "duration_secs": duration_secs,
                "coload_cpu": coload_cpu,
            })
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    @registry.tool(description=(
        "Run a Steam title in the guest VM under weston-headless + MangoHud. "
        "`app_id` is the Steam app id; `args` are extra launch options (e.g. "
        "timedemo flags); `mangohud_output` is the guest CSV path; "
        "`duration_secs` is the expected benchmark runtime. Returns "
        "log_found and psi_*_delta."
    ))
    def launch_steam_benchmark(
        app_id: int,
        args: list[str],
        mangohud_output: str,
        duration_secs: int = 60,
    ) -> dict:
        if guest_rpc is None:
            return {
                "status": "dry_run",
                "message": (
                    f"Would launch Steam app {app_id} with args {args} logging "
                    f"to {mangohud_output} (no guest RPC)"
                ),
            }
        try:
            return guest_rpc.call("launch_steam_benchmark", {
                "app_id": app_id,
                "args": args,
                "mangohud_output": mangohud_output,
                "duration_secs": duration_secs,
            })
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    @registry.tool(description=(
        "Fetch a MangoHud CSV log from the guest VM and parse it into frame "
        "statistics: frame_count, fps_avg, fps_p1, fps_min, fps_max, and "
        "frametime p50/p95/p99 in milliseconds."
    ))
    def fetch_mangohud_log(log_path: str) -> dict:
        if guest_rpc is None:
            return {
                "status": "dry_run",
                "message": f"Would fetch {log_path} from guest (no guest RPC)",
            }
        try:
            resp = guest_rpc.call("fetch_file", {"path": log_path})
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        if resp.get("status") != "ok":
            return {"status": "error", "error": resp.get("message", "fetch_file failed")}

        data = resp.get("data", {})
        contents_b64 = data.get("contents_b64")
        if not contents_b64:
            return {"status": "error", "error": "guest returned no file contents"}

        csv_text = base64.b64decode(contents_b64).decode("utf-8", errors="replace")
        stats = parse_mangohud_csv(csv_text)
        if data.get("truncated"):
            stats["truncated"] = True
        return stats

    @registry.tool(description=(
        "Fetch a first-party benchmark frame log from the guest VM (one "
        "frametime-in-ms per line, e.g. Civ 6's Logs/Benchmark-*.csv as "
        "surfaced in launch_steam_benchmark's firstparty_log field) and "
        "parse it into frame statistics: frame_count, fps_avg, fps_p1, and "
        "frametime p50/p95/p99 in milliseconds. Prefer these over MangoHud "
        "stats when available — they are per-frame and exactly scoped to "
        "the benchmark scene, with no load-screen contamination."
    ))
    def fetch_firstparty_frametimes(log_path: str) -> dict:
        if guest_rpc is None:
            return {
                "status": "dry_run",
                "message": f"Would fetch {log_path} from guest (no guest RPC)",
            }
        try:
            resp = guest_rpc.call("fetch_file", {"path": log_path})
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        if resp.get("status") != "ok":
            return {"status": "error", "error": resp.get("message", "fetch_file failed")}

        data = resp.get("data", {})
        contents_b64 = data.get("contents_b64")
        if not contents_b64:
            return {"status": "error", "error": "guest returned no file contents"}

        csv_text = base64.b64decode(contents_b64).decode("utf-8", errors="replace")
        stats = parse_frametime_csv(csv_text)
        if data.get("truncated"):
            stats["truncated"] = True
        return stats

    @registry.tool(description=(
        "Fetch a Perfetto trace file from the guest VM and save it under "
        "the orchestrator's artifact directory on the host. Returns the "
        "host path and size. Call after the profiled benchmark run "
        "completes (the capture auto-stops after its duration)."
    ))
    def fetch_perfetto_trace(
        trace_path: str = "/tmp/crucible_trace.perfetto-trace",
        host_output_dir: str = "/tmp",
    ) -> dict:
        if guest_rpc is None:
            return {
                "status": "dry_run",
                "message": f"Would fetch {trace_path} from guest (no guest RPC)",
            }
        # perfetto writes the trace only when the capture ends; if the fetch
        # races the flush the file is briefly absent/empty. Retry a few
        # times before declaring failure.
        contents_b64 = None
        data: dict[str, Any] = {}
        last_error = "guest returned no trace contents"
        for attempt in range(4):
            if attempt:
                time.sleep(3)
            try:
                resp = guest_rpc.call("fetch_file", {"path": trace_path})
            except Exception as exc:
                last_error = str(exc)
                continue
            if resp.get("status") != "ok":
                last_error = resp.get("message", "fetch_file failed")
                continue
            data = resp.get("data", {})
            contents_b64 = data.get("contents_b64")
            if contents_b64:
                break
        if not contents_b64:
            return {"status": "error", "error": last_error}
        blob = base64.b64decode(contents_b64)
        host_path = os.path.join(host_output_dir, os.path.basename(trace_path))
        try:
            with open(host_path, "wb") as f:
                f.write(blob)
        except OSError as exc:
            return {"status": "error", "error": f"cannot write {host_path}: {exc}"}
        return {
            "status": "ok",
            "host_path": host_path,
            "size_bytes": len(blob),
            "truncated": bool(data.get("truncated")),
        }
