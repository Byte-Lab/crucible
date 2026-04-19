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
