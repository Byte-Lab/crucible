from agents.analyzer.tools import make_analyzer_tools
from agents.common.tool_registry import ToolRegistry


def test_analyzer_tools_registered():
    registry = ToolRegistry()
    make_analyzer_tools(registry)
    names = [t["name"] for t in registry.tools]
    assert "read_file" in names
    assert "run_trace_processor_query" in names
    assert "compare_measurements" in names


def test_compare_measurements_improvement():
    registry = ToolRegistry()
    make_analyzer_tools(registry)
    result = registry.call("compare_measurements", {
        "baseline": {"fps_avg": 60.0, "frame_time_p99_ms": 25.0, "psi_cpu_avg": 1.0},
        "comparison": {"fps_avg": 65.0, "frame_time_p99_ms": 22.0, "psi_cpu_avg": 0.8},
    })
    assert result["fps_avg"]["delta_pct"] > 0
    assert result["frame_time_p99_ms"]["delta_pct"] < 0


def test_compare_measurements_regression():
    registry = ToolRegistry()
    make_analyzer_tools(registry)
    result = registry.call("compare_measurements", {
        "baseline": {"fps_avg": 60.0, "frame_time_p99_ms": 25.0},
        "comparison": {"fps_avg": 55.0, "frame_time_p99_ms": 30.0},
    })
    assert result["fps_avg"]["delta_pct"] < 0
