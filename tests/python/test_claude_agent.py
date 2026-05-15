import json
import io
import sys
import uuid
from unittest.mock import MagicMock, patch

from agents.common.protocol import AgentConfig, TaskEnvelope


def make_task(context=None):
    task = TaskEnvelope(
        task_id=uuid.uuid4(), agent="test",
        context=context or {},
        config=AgentConfig(model="claude-sonnet-4-20250514", max_tokens=8192, timeout_seconds=300),
    )
    return task.model_dump_json()


def test_claude_agent_simple_text_response():
    from agents.common.claude_agent import ClaudeAgentBase

    class TestAgent(ClaudeAgentBase):
        def system_prompt(self): return "You are a test agent."
        def build_user_message(self, task): return "Say hello"

    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="Hello!")]
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 10
    mock_response.usage.output_tokens = 5

    with patch("agents.common.claude_agent.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = mock_response

        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(make_task().encode()))
        captured = io.StringIO()
        sys.stdout = captured
        try:
            TestAgent().run()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout

    output = json.loads(captured.getvalue())
    assert output["status"] == "success"
    assert output["result"]["response"] == "Hello!"
    assert output["usage"]["input_tokens"] == 10


def test_claude_agent_with_tool_use():
    from agents.common.claude_agent import ClaudeAgentBase

    class TestAgent(ClaudeAgentBase):
        def system_prompt(self): return "You are a test agent."
        def build_user_message(self, task): return "Add 3 and 4"
        def setup_tools(self, registry):
            @registry.tool(description="Add two numbers")
            def add(a: int, b: int) -> dict:
                return {"sum": a + b}

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tool_123"
    tool_use_block.name = "add"
    tool_use_block.input = {"a": 3, "b": 4}

    mock_response_1 = MagicMock()
    mock_response_1.content = [tool_use_block]
    mock_response_1.stop_reason = "tool_use"
    mock_response_1.usage.input_tokens = 20
    mock_response_1.usage.output_tokens = 15

    mock_response_2 = MagicMock()
    mock_response_2.content = [MagicMock(type="text", text="The sum is 7")]
    mock_response_2.stop_reason = "end_turn"
    mock_response_2.usage.input_tokens = 30
    mock_response_2.usage.output_tokens = 10

    with patch("agents.common.claude_agent.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = [mock_response_1, mock_response_2]

        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(make_task().encode()))
        captured = io.StringIO()
        sys.stdout = captured
        try:
            TestAgent().run()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout

    output = json.loads(captured.getvalue())
    assert output["status"] == "success"
    assert output["result"]["response"] == "The sum is 7"
    assert output["usage"]["input_tokens"] == 50
    assert output["usage"]["output_tokens"] == 25
    assert output["usage"]["api_calls"] == 2
