# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

from agents.common.tool_registry import ToolRegistry


def test_register_tool():
    registry = ToolRegistry()

    @registry.tool(description="List installed Steam games")
    def list_games(library_path: str = "~/.steam") -> dict:
        return {"games": []}

    assert len(registry.tools) == 1
    assert registry.tools[0]["name"] == "list_games"
    assert registry.tools[0]["description"] == "List installed Steam games"


def test_tool_schema_has_parameters():
    registry = ToolRegistry()

    @registry.tool(description="Launch a game")
    def launch_game(app_id: int, args: list[str] | None = None) -> dict:
        return {}

    schema = registry.tools[0]
    props = schema["input_schema"]["properties"]
    assert "app_id" in props
    assert props["app_id"]["type"] == "integer"
    assert "args" in props
    assert "app_id" in schema["input_schema"]["required"]
    assert "args" not in schema["input_schema"]["required"]


def test_call_tool():
    registry = ToolRegistry()

    @registry.tool(description="Add two numbers")
    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    result = registry.call("add", {"a": 3, "b": 4})
    assert result == {"sum": 7}


def test_call_unknown_tool():
    registry = ToolRegistry()
    try:
        registry.call("nonexistent", {})
        assert False, "should have raised"
    except KeyError:
        pass


def test_multiple_tools():
    registry = ToolRegistry()

    @registry.tool(description="Tool A")
    def tool_a() -> dict:
        return {"a": True}

    @registry.tool(description="Tool B")
    def tool_b(x: str) -> dict:
        return {"b": x}

    assert len(registry.tools) == 2
    names = [t["name"] for t in registry.tools]
    assert "tool_a" in names
    assert "tool_b" in names
