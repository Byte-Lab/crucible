from agents.game_selector.tools import make_game_selector_tools
from agents.common.tool_registry import ToolRegistry


def test_list_steam_games_tool_registered():
    registry = ToolRegistry()
    make_game_selector_tools(registry)
    names = [t["name"] for t in registry.tools]
    assert "list_steam_games" in names
    assert "check_benchmark_support" in names
    assert "get_game_metadata" in names


def test_check_benchmark_support_known_games():
    registry = ToolRegistry()
    make_game_selector_tools(registry)
    result = registry.call("check_benchmark_support", {"app_id": 750920})
    assert result["has_benchmark"] is True
    assert result["benchmark_args"] is not None
    result = registry.call("check_benchmark_support", {"app_id": 99999})
    assert result["has_benchmark"] is False


def test_list_native_benchmarks_tool():
    registry = ToolRegistry()
    make_game_selector_tools(registry)
    names = [t["name"] for t in registry.tools]
    assert "list_native_benchmarks" in names
    result = registry.call("list_native_benchmarks", {})
    benchmarks = {b["name"]: b for b in result["benchmarks"]}
    assert "vkmark" in benchmarks
    assert "glmark2" in benchmarks
    assert benchmarks["vkmark"]["workload_profile"] == "gpu_heavy"


def test_game_mode_user_message_mentions_native_benchmarks():
    from uuid import uuid4

    from agents.common.protocol import AgentConfig, TaskEnvelope
    from agents.game_selector.agent import GameSelectorAgent

    task = TaskEnvelope(
        task_id=uuid4(),
        agent="game_selector",
        context={"action": "select_game", "workload_kind": "game"},
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )
    msg = GameSelectorAgent().build_user_message(task)
    assert "list_native_benchmarks" in msg
