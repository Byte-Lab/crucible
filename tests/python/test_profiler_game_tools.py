"""Profiler game-mode tools: launch_benchmark + fetch_mangohud_log."""
import base64
from typing import Any

from agents.common.tool_registry import ToolRegistry
from agents.profiler.tools import make_profiler_tools, parse_mangohud_csv

MANGOHUD_CSV = (
    "os,cpu,gpu,ram,kernel,driver,cpuscheduler\n"
    "Linux,AMD,RX 7900 XT,32GB,6.9,radv,\n"
    "fps,frametime,cpu_load,gpu_load,cpu_temp,gpu_temp\n"
    "60.0,16.6,40,95,55,70\n"
    "58.5,17.1,42,96,55,71\n"
    "30.0,33.3,45,97,56,72\n"
    "61.2,16.3,41,95,55,70\n"
)


class FakeGuestRpc:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, cmd: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((cmd, args))
        return self.responses[cmd]


def _registry(rpc) -> ToolRegistry:
    registry = ToolRegistry()
    make_profiler_tools(registry, guest_rpc=rpc)
    return registry


def test_launch_benchmark_dry_run_without_guest():
    registry = _registry(None)
    result = registry.call("launch_benchmark", {
        "name": "vkmark",
        "args": [],
        "mangohud_output": "/tmp/mh.csv",
    })
    assert result["status"] == "dry_run"
    assert "vkmark" in result["message"]


def test_launch_benchmark_forwards_to_guest():
    rpc = FakeGuestRpc({
        "launch_benchmark": {
            "status": "ok",
            "data": {"exit_code": 0, "log_found": True, "mangohud_output": "/tmp/mh.csv"},
        }
    })
    registry = _registry(rpc)
    result = registry.call("launch_benchmark", {
        "name": "vkmark",
        "args": ["--size", "1920x1080"],
        "mangohud_output": "/tmp/mh.csv",
        "duration_secs": 15,
    })
    assert result["status"] == "ok"
    assert rpc.calls == [(
        "launch_benchmark",
        {
            "name": "vkmark",
            "args": ["--size", "1920x1080"],
            "mangohud_output": "/tmp/mh.csv",
            "duration_secs": 15,
        },
    )]


def test_fetch_mangohud_log_decodes_and_parses():
    encoded = base64.b64encode(MANGOHUD_CSV.encode()).decode("ascii")
    rpc = FakeGuestRpc({
        "fetch_file": {
            "status": "ok",
            "data": {
                "path": "/tmp/mh.csv",
                "size": len(MANGOHUD_CSV),
                "truncated": False,
                "contents_b64": encoded,
            },
        }
    })
    registry = _registry(rpc)
    result = registry.call("fetch_mangohud_log", {"log_path": "/tmp/mh.csv"})
    assert result["frame_count"] == 4
    assert result["fps_avg"] > 0
    assert result["fps_p1"] == 30.0
    assert result["frametime_p99_ms"] == 33.3
    assert rpc.calls == [("fetch_file", {"path": "/tmp/mh.csv"})]


def test_fetch_mangohud_log_surfaces_guest_error():
    rpc = FakeGuestRpc({
        "fetch_file": {"status": "error", "message": "file not found: /tmp/mh.csv"},
    })
    registry = _registry(rpc)
    result = registry.call("fetch_mangohud_log", {"log_path": "/tmp/mh.csv"})
    assert result["status"] == "error"
    assert "not found" in result["error"]


def test_fetch_mangohud_log_dry_run_without_guest():
    registry = _registry(None)
    result = registry.call("fetch_mangohud_log", {"log_path": "/tmp/mh.csv"})
    assert result["status"] == "dry_run"


def test_profiler_game_user_message_instructs_launch_benchmark():
    from uuid import uuid4

    from agents.common.protocol import AgentConfig, TaskEnvelope
    from agents.profiler.agent import ProfilerAgent

    task = TaskEnvelope(
        task_id=uuid4(),
        agent="profiler",
        context={
            "phase": "baseline",
            "game": "vkmark",
            "workload_kind": "game",
            "game_benchmark": "vkmark",
            # No benchmark_args: those are stress-ng knobs and the
            # orchestrator deliberately omits them in game mode.
            "mangohud_output": "/tmp/crucible_mangohud.csv",
        },
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )
    msg = ProfilerAgent().build_user_message(task)
    assert "launch_benchmark" in msg
    assert "fetch_mangohud_log" in msg
    assert "vkmark" in msg
    assert "/tmp/crucible_mangohud.csv" in msg
    assert "frame_time_p99_ms" in msg


def test_parse_mangohud_csv_skips_metadata_rows():
    # MangoHud logs start with two system-info rows before the fps header;
    # the parser must not choke on them.
    stats = parse_mangohud_csv(MANGOHUD_CSV)
    assert stats["frame_count"] == 4
    assert stats["fps_min"] == 30.0
    assert stats["fps_max"] == 61.2


def test_launch_steam_benchmark_forwards_to_guest():
    rpc = FakeGuestRpc({
        "launch_steam_benchmark": {
            "status": "ok",
            "data": {"log_found": True, "mangohud_output": "/tmp/mh.csv"},
        }
    })
    registry = _registry(rpc)
    result = registry.call("launch_steam_benchmark", {
        "app_id": 570,
        "args": ["+timedemo", "bench"],
        "mangohud_output": "/tmp/mh.csv",
        "duration_secs": 90,
    })
    assert result["status"] == "ok"
    assert rpc.calls == [(
        "launch_steam_benchmark",
        {
            "app_id": 570,
            "args": ["+timedemo", "bench"],
            "mangohud_output": "/tmp/mh.csv",
            "duration_secs": 90,
        },
    )]


def test_launch_steam_benchmark_dry_run_without_guest():
    registry = _registry(None)
    result = registry.call("launch_steam_benchmark", {
        "app_id": 570,
        "args": [],
        "mangohud_output": "/tmp/mh.csv",
    })
    assert result["status"] == "dry_run"


def test_profiler_steam_user_message_instructs_steam_benchmark():
    from uuid import uuid4

    from agents.common.protocol import AgentConfig, TaskEnvelope
    from agents.profiler.agent import ProfilerAgent

    task = TaskEnvelope(
        task_id=uuid4(),
        agent="profiler",
        context={
            "phase": "baseline",
            "game": "dota2",
            "workload_kind": "steam",
            "steam_app_id": 570,
            "duration_secs": 90,
            "mangohud_output": "/tmp/crucible_mangohud.csv",
        },
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )
    msg = ProfilerAgent().build_user_message(task)
    assert "launch_steam_benchmark" in msg
    assert "app_id=570" in msg
    assert "duration_secs=90" in msg
    assert "fetch_mangohud_log" in msg
    assert "do NOT invent metrics" in msg or "NOT invent metrics" in msg
