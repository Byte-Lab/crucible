from uuid import uuid4

from agents.analyzer.agent import AnalyzerAgent
from agents.analyzer.tools import make_analyzer_tools
from agents.common.protocol import AgentConfig, TaskEnvelope
from agents.common.tool_registry import ToolRegistry


def _make_task(context: dict) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=uuid4(),
        agent="analyzer",
        context=context,
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )


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


def test_analyzer_user_message_includes_previous_attempts():
    task = _make_task({
        "game_name": "synthetic",
        "metrics": {"fps_avg": 60.0},
        "attempt_number": 2,
        "previous_attempts": [
            {"attempt_number": 1, "patch_path": "/tmp/a.diff", "verdict": "marginal"},
        ],
    })
    msg = AnalyzerAgent().build_user_message(task)
    assert "attempt 2" in msg
    assert "Previous optimization attempts failed at the margin" in msg
    assert "alternate bottlenecks" in msg
    assert "/tmp/a.diff" in msg
    assert '"verdict": "marginal"' in msg


def test_analyzer_user_message_omits_previous_attempts_on_first_pass():
    task = _make_task({
        "game_name": "synthetic",
        "metrics": {"fps_avg": 60.0},
    })
    msg = AnalyzerAgent().build_user_message(task)
    assert "Previous optimization attempts" not in msg
    assert "alternate bottlenecks" not in msg
    assert "attempt 1" in msg


def test_search_source_output_is_capped(tmp_path):
    # The Agent SDK's CLI transport rejects any single JSON message over
    # 1 MiB; an uncapped grep over a kernel tree blew through it and
    # killed the Analyzer mid-cycle. Tool results must stay bounded.
    registry = ToolRegistry()
    make_analyzer_tools(registry)
    big = tmp_path / "big.c"
    big.write_text("needle\n" * 200_000)
    result = registry.call("search_source", {
        "directory": str(tmp_path),
        "pattern": "needle",
    })
    assert result["truncated"] is True
    assert result["count"] <= 500
    import json
    assert len(json.dumps(result)) < 256_000


def test_read_file_caps_caller_supplied_max_lines(tmp_path):
    registry = ToolRegistry()
    make_analyzer_tools(registry)
    big = tmp_path / "big.txt"
    big.write_text("x" * 4096 + "\n", encoding="utf-8")
    big.write_text(("x" * 4096 + "\n") * 10_000, encoding="utf-8")
    result = registry.call("read_file", {
        "path": str(big),
        "max_lines": 10_000,
    })
    import json
    assert len(json.dumps(result)) < 1_000_000
    assert result["truncated"] is True
