from __future__ import annotations

import os
import subprocess
from typing import Any

from agents.common.tool_registry import ToolRegistry

LOWER_IS_BETTER: set[str] = {
    "frame_time_p99_ms",
    "frame_time_p95_ms",
    "frame_time_p50_ms",
    "psi_cpu_avg",
    "psi_memory_avg",
    "psi_io_avg",
}


def make_analyzer_tools(registry: ToolRegistry) -> None:
    """Register analyzer tools into the given registry."""

    @registry.tool(description="Read a file from disk, returning up to max_lines lines.")
    def read_file(path: str, max_lines: int = 500) -> dict:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = []
                for i, line in enumerate(fh):
                    if i >= max_lines:
                        break
                    lines.append(line)
            return {"content": "".join(lines), "lines_read": len(lines)}
        except OSError as exc:
            return {"error": str(exc)}

    @registry.tool(
        description="Run a SQL query against a Perfetto trace using trace_processor_shell."
    )
    def run_trace_processor_query(trace_path: str, query: str) -> dict:
        try:
            result = subprocess.run(
                ["trace_processor_shell", "--query", query, trace_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except FileNotFoundError:
            return {"error": "trace_processor_shell not found in PATH"}
        except subprocess.TimeoutExpired:
            return {"error": "query timed out after 30 seconds"}

    @registry.tool(
        description="Compare two sets of performance measurements, computing delta percentages."
    )
    def compare_measurements(baseline: dict, comparison: dict) -> dict:
        results: dict[str, Any] = {}
        all_keys = set(baseline.keys()) | set(comparison.keys())
        for key in sorted(all_keys):
            base_val = baseline.get(key)
            comp_val = comparison.get(key)
            if base_val is None or comp_val is None:
                continue
            delta = comp_val - base_val
            delta_pct = (delta / base_val * 100.0) if base_val != 0 else 0.0
            if key in LOWER_IS_BETTER:
                improved = comp_val < base_val
            else:
                improved = comp_val > base_val
            results[key] = {
                "baseline": base_val,
                "comparison": comp_val,
                "delta": delta,
                "delta_pct": delta_pct,
                "improved": improved,
            }
        return results

    @registry.tool(description="Search source files matching a pattern using grep.")
    def search_source(directory: str, pattern: str, file_glob: str = "*.c") -> dict:
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include", file_glob, pattern, directory],
                capture_output=True,
                text=True,
                timeout=30,
            )
            matches = result.stdout.strip().split("\n") if result.stdout.strip() else []
            return {"matches": matches, "count": len(matches)}
        except FileNotFoundError:
            return {"error": "grep not found"}
        except subprocess.TimeoutExpired:
            return {"error": "search timed out"}
