# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

import io
import json
import sys
import uuid
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from agents.common.protocol import AgentConfig, TaskEnvelope


def make_task(context=None):
    task = TaskEnvelope(
        task_id=uuid.uuid4(), agent="test",
        context=context or {},
        config=AgentConfig(model="claude-sonnet-4-20250514", max_tokens=8192, timeout_seconds=300),
    )
    return task.model_dump_json()


def _result_message(input_tokens=0, output_tokens=0, num_turns=1, result=None, is_error=False, errors=None):
    """Build a minimal ResultMessage. ResultMessage is a dataclass with many
    required positional fields; default the ones we don't care about."""
    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=0,
        duration_api_ms=0,
        is_error=is_error,
        num_turns=num_turns,
        session_id="test-session",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        result=result,
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        deferred_tool_use=None,
        errors=errors,
        api_error_status=None,
        uuid=None,
    )


def _assistant_message(blocks):
    return AssistantMessage(
        content=blocks,
        model="claude-sonnet-4-20250514",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id=None,
        stop_reason=None,
        session_id="test-session",
        uuid=None,
    )


def _async_iter(items):
    async def _gen():
        for x in items:
            yield x
    return _gen()


def _run_agent(agent_cls, scripted_messages):
    """Run an agent end-to-end with claude_agent_sdk.query mocked to yield
    the scripted messages. Returns the parsed ResultEnvelope dict."""

    def fake_query(*, prompt, options=None, transport=None):
        return _async_iter(scripted_messages)

    with patch("agents.common.claude_agent.query", side_effect=fake_query):
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(make_task().encode()))
        captured = io.StringIO()
        sys.stdout = captured
        try:
            agent_cls().run()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout

    return json.loads(captured.getvalue())


def test_claude_agent_simple_text_response():
    from agents.common.claude_agent import ClaudeAgentBase

    class TestAgent(ClaudeAgentBase):
        def system_prompt(self): return "You are a test agent."
        def build_user_message(self, task): return "Say hello"

    msgs = [
        _assistant_message([TextBlock(text="Hello!")]),
        _result_message(input_tokens=10, output_tokens=5, num_turns=1),
    ]
    output = _run_agent(TestAgent, msgs)

    assert output["status"] == "success"
    assert output["result"]["response"] == "Hello!"
    assert output["usage"]["input_tokens"] == 10
    assert output["usage"]["output_tokens"] == 5
    assert output["usage"]["api_calls"] == 1


def test_claude_agent_with_tool_use():
    """Final text on the last AssistantMessage wins; intermediate tool-use
    turns get logged but don't overwrite final_text."""
    from agents.common.claude_agent import ClaudeAgentBase

    class TestAgent(ClaudeAgentBase):
        def system_prompt(self): return "You are a test agent."
        def build_user_message(self, task): return "Add 3 and 4"
        def setup_tools(self, registry):
            @registry.tool(description="Add two numbers")
            def add(a: int, b: int) -> dict:
                return {"sum": a + b}

    msgs = [
        _assistant_message([
            ToolUseBlock(id="tool_123", name="mcp__crucible__add", input={"a": 3, "b": 4}),
        ]),
        _assistant_message([TextBlock(text="The sum is 7")]),
        _result_message(input_tokens=50, output_tokens=25, num_turns=2),
    ]
    output = _run_agent(TestAgent, msgs)

    assert output["status"] == "success"
    assert output["result"]["response"] == "The sum is 7"
    assert output["usage"]["input_tokens"] == 50
    assert output["usage"]["output_tokens"] == 25
    assert output["usage"]["api_calls"] == 2
    # Tool call should have been logged.
    assert any("tool call: mcp__crucible__add" in line for line in output["logs"])


def test_claude_agent_propagates_result_error():
    """ResultMessage.is_error=True should fail the agent run."""
    from agents.common.claude_agent import ClaudeAgentBase

    class TestAgent(ClaudeAgentBase):
        def system_prompt(self): return "test"
        def build_user_message(self, task): return "hi"

    msgs = [
        _result_message(is_error=True, errors=["rate_limited"]),
    ]
    output = _run_agent(TestAgent, msgs)

    assert output["status"] == "failure"
    assert "rate_limited" in output["result"]["error"]
