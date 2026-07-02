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
            "coload_cpu": 0,
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


# ---------------------------------------------------------------------------
# Perfetto: start_profiling wiring + fetch_perfetto_trace
# ---------------------------------------------------------------------------


def test_start_profiling_sends_duration_and_output():
    rpc = FakeGuestRpc({"start_profiling": {"status": "ok", "data": {"pid": 7}}})
    registry = _registry(rpc)
    result = registry.call("start_profiling", {
        "duration_secs": 45,
        "output": "/tmp/trace.perfetto-trace",
    })
    assert result["status"] == "started"
    cmd, args = rpc.calls[0]
    assert cmd == "start_profiling"
    # duration_secs is the first-class wire field the guest handler reads;
    # output rides in config.
    assert args["duration_secs"] == 45
    assert args["config"]["output"] == "/tmp/trace.perfetto-trace"


def test_fetch_perfetto_trace_writes_host_file(tmp_path):
    blob = b"\x0a\x0bperfetto-binary-trace"
    rpc = FakeGuestRpc({
        "fetch_file": {
            "status": "ok",
            "data": {
                "contents_b64": base64.b64encode(blob).decode(),
                "truncated": False,
            },
        }
    })
    registry = _registry(rpc)
    result = registry.call("fetch_perfetto_trace", {
        "trace_path": "/tmp/crucible_trace.perfetto-trace",
        "host_output_dir": str(tmp_path),
    })
    assert result["status"] == "ok"
    assert result["size_bytes"] == len(blob)
    with open(result["host_path"], "rb") as f:
        assert f.read() == blob


def test_fetch_perfetto_trace_error_when_guest_fails():
    rpc = FakeGuestRpc({
        "fetch_file": {"status": "error", "message": "not allowed"}
    })
    registry = _registry(rpc)
    result = registry.call("fetch_perfetto_trace", {})
    assert result["status"] == "error"
    assert "not allowed" in result["error"]


# ---------------------------------------------------------------------------
# First-party benchmark frame logs (Civ 6 Logs/Benchmark-*.csv)
# ---------------------------------------------------------------------------


def test_parse_frametime_csv_computes_per_frame_stats():
    from agents.profiler.tools import parse_frametime_csv

    # 98 fast frames + 2 slow: nearest-rank p99 (idx 98 of 100) must
    # surface the stall.
    text = "\n".join(["10.0"] * 98 + ["100.0"] * 2)
    stats = parse_frametime_csv(text)
    assert stats["frame_count"] == 100
    assert abs(stats["fps_avg"] - 1000.0 * 100 / (98 * 10.0 + 200.0)) < 1e-9
    assert stats["frametime_p99_ms"] == 100.0
    assert abs(stats["fps_p1"] - 10.0) < 1e-9


def test_parse_frametime_csv_tolerates_junk_and_empty():
    from agents.profiler.tools import parse_frametime_csv

    stats = parse_frametime_csv("header\n\n16.6\nnot-a-number\n33.3\n")
    assert stats["frame_count"] == 2
    empty = parse_frametime_csv("")
    assert empty["frame_count"] == 0
    assert empty["fps_avg"] == 0.0


def test_fetch_firstparty_frametimes_parses_guest_file():
    calls = {}

    class FakeRpc:
        def call(self, cmd, args):
            calls[cmd] = args
            payload = base64.b64encode(b"20.0\n20.0\n40.0\n").decode()
            return {
                "status": "ok",
                "data": {"contents_b64": payload, "truncated": False},
            }

    from agents.profiler.game_tools import make_profiler_game_tools

    registry = ToolRegistry()
    make_profiler_game_tools(registry, FakeRpc())
    result = registry.call(
        "fetch_firstparty_frametimes",
        {"log_path": "/tmp/crucible_mangohud_firstparty.csv"},
    )
    assert calls["fetch_file"]["path"] == "/tmp/crucible_mangohud_firstparty.csv"
    assert result["frame_count"] == 3
    assert result["frametime_p99_ms"] == 40.0


def test_profiler_steam_user_message_prefers_firstparty_log():
    from uuid import uuid4

    from agents.common.protocol import AgentConfig, TaskEnvelope
    from agents.profiler.agent import ProfilerAgent

    task = TaskEnvelope(
        task_id=uuid4(),
        agent="profiler",
        context={
            "phase": "baseline",
            "game": "civ6",
            "workload_kind": "steam",
            "steam_app_id": 289070,
            "duration_secs": 120,
            "mangohud_output": "/tmp/crucible_mangohud.csv",
        },
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )
    msg = ProfilerAgent().build_user_message(task)
    assert "fetch_firstparty_frametimes" in msg
    assert "firstparty_log" in msg
    # Mixed methodologies corrupt a phase: harvest failure on a
    # firstparty-expected title must fail the run, never silently fall
    # back to MangoHud sampling.
    assert "firstparty_expected" in msg
    assert "Do NOT fall back" in msg
    assert "metrics_source" in msg
    # No perfetto steps without capture_perfetto.
    assert "start_profiling" not in msg


def test_profiler_steam_user_message_profiled_run_traces_single_launch():
    from uuid import uuid4

    from agents.common.protocol import AgentConfig, TaskEnvelope
    from agents.profiler.agent import ProfilerAgent

    task = TaskEnvelope(
        task_id=uuid4(),
        agent="profiler",
        context={
            "phase": "baseline",
            "game": "civ6",
            "workload_kind": "steam",
            "steam_app_id": 289070,
            "duration_secs": 120,
            "mangohud_output": "/tmp/crucible_mangohud.csv",
            "capture_perfetto": True,
            "perfetto_output": "/tmp/crucible_trace.perfetto-trace",
            "perfetto_host_dir": "/home/void/.crucible/civ6-grind-artifacts",
        },
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )
    msg = ProfilerAgent().build_user_message(task)
    assert "start_profiling" in msg
    assert "buffer_size_kb=6144" in msg
    assert "stop_profiling" in msg
    assert "fetch_perfetto_trace" in msg
    # Single launch: start_profiling comes before the (only) launch call.
    assert msg.index("start_profiling") < msg.index("launch_steam_benchmark")
    assert msg.count("launch_steam_benchmark(app_id=") == 1
