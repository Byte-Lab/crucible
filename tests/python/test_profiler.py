# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

from agents.profiler.tools import make_profiler_tools, parse_mangohud_csv
from agents.common.tool_registry import ToolRegistry


def test_profiler_tools_registered():
    registry = ToolRegistry()
    make_profiler_tools(registry, guest_rpc=None)
    names = [t["name"] for t in registry.tools]
    assert "start_profiling" in names
    assert "stop_profiling" in names
    assert "collect_psi_snapshot" in names
    assert "read_mangohud_log" in names


def test_collect_psi_snapshot_reads_proc():
    registry = ToolRegistry()
    make_profiler_tools(registry, guest_rpc=None)
    result = registry.call("collect_psi_snapshot", {})
    assert "system_psi" in result


def test_parse_mangohud_csv():
    csv_data = "fps,frametime,cpu_load,gpu_load\n60.1,16.6,45,80\n59.8,16.7,46,82\n"
    result = parse_mangohud_csv(csv_data)
    assert result["frame_count"] == 2
    assert abs(result["fps_avg"] - 59.95) < 0.1
