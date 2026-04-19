# Crucible Core Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the four core AI agents that power the optimization loop: game selector, profiler, analyzer, and optimizer. Each agent uses the Claude API with tool-use to reason about its domain and take actions via the guest agent RPC or local commands.

**Architecture:** Extend AgentBase with a ClaudeAgentBase that handles the Claude API conversation loop (send messages, handle tool calls, iterate until done). Each agent defines its own system prompt and tool set. Tools are Python functions decorated with metadata that maps to Claude's tool-use schema.

**Tech Stack:** Python 3.12+ (anthropic SDK, pydantic), Claude API with tool-use

**Spec reference:** `docs/superpowers/specs/2026-04-12-crucible-design.md` (Python Agents section)

**Plan series:**
- Plan 1: Foundation (complete)
- Plan 2: VM management (complete)
- **Plan 3 (this plan):** Core agents
- Plan 4: Orchestration loop

---

## File Map

| File | Responsibility |
|------|---------------|
| `agents/common/claude_agent.py` | ClaudeAgentBase: conversation loop with Claude API tool-use |
| `agents/common/tool_registry.py` | Tool decorator and registry for converting Python functions to Claude tool schemas |
| `agents/game_selector/__init__.py` | Package init |
| `agents/game_selector/agent.py` | GameSelectorAgent: picks game + benchmark strategy |
| `agents/game_selector/tools.py` | Tools: query Steam library, detect benchmarks |
| `agents/profiler/__init__.py` | Package init |
| `agents/profiler/agent.py` | ProfilerAgent: configures and collects measurements |
| `agents/profiler/tools.py` | Tools: start/stop profiling, read PSI, collect metrics |
| `agents/analyzer/__init__.py` | Package init |
| `agents/analyzer/agent.py` | AnalyzerAgent: interprets profiles, identifies bottlenecks |
| `agents/analyzer/tools.py` | Tools: read traces, parse MangoHud CSV, read source |
| `agents/optimizer/__init__.py` | Package init |
| `agents/optimizer/agent.py` | OptimizerAgent: generates code changes |
| `agents/optimizer/tools.py` | Tools: read/write source, generate patches, invoke builds |
| `tests/python/test_tool_registry.py` | Tool registry unit tests |
| `tests/python/test_claude_agent.py` | ClaudeAgentBase unit tests (mocked API) |
| `tests/python/test_game_selector.py` | Game selector tests (mocked API) |
| `tests/python/test_profiler.py` | Profiler tests (mocked API) |
| `tests/python/test_analyzer.py` | Analyzer tests (mocked API) |
| `tests/python/test_optimizer.py` | Optimizer tests (mocked API) |

---

## Task 1: Tool Registry

**Files:**
- Create: `agents/common/tool_registry.py`
- Create: `tests/python/test_tool_registry.py`

The tool registry converts Python functions into Claude API tool schemas and dispatches tool calls back to the right function.

- [ ] **Step 1: Write failing tests**

```python
# tests/python/test_tool_registry.py
from agents.common.tool_registry import ToolRegistry


def test_register_tool():
    registry = ToolRegistry()

    @registry.tool(description="List installed Steam games")
    def list_games(library_path: str = "~/.steam") -> dict:
        """List games in Steam library."""
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_tool_registry.py -v`

- [ ] **Step 3: Implement ToolRegistry**

```python
# agents/common/tool_registry.py
import inspect
from typing import Any, Callable, get_type_hints

# Map Python types to JSON Schema types
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json_schema(annotation: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema type."""
    origin = getattr(annotation, "__origin__", None)

    # Handle Optional (X | None)
    if origin is type(int | str):  # types.UnionType
        args = annotation.__args__
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _python_type_to_json_schema(non_none[0])

    # Handle list[X]
    if origin is list:
        inner_args = getattr(annotation, "__args__", None)
        schema: dict[str, Any] = {"type": "array"}
        if inner_args:
            schema["items"] = _python_type_to_json_schema(inner_args[0])
        return schema

    # Handle dict[K, V]
    if origin is dict:
        return {"type": "object"}

    # Basic types
    if annotation in _TYPE_MAP:
        return {"type": _TYPE_MAP[annotation]}

    return {"type": "string"}


class ToolRegistry:
    """Registry that converts Python functions to Claude API tool schemas."""

    def __init__(self) -> None:
        self._functions: dict[str, Callable] = {}
        self._schemas: list[dict[str, Any]] = []

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._schemas

    def tool(self, description: str) -> Callable:
        """Decorator to register a function as a Claude API tool."""

        def decorator(func: Callable) -> Callable:
            hints = get_type_hints(func)
            sig = inspect.signature(func)

            properties: dict[str, Any] = {}
            required: list[str] = []

            for name, param in sig.parameters.items():
                if name == "self":
                    continue
                annotation = hints.get(name, str)
                properties[name] = _python_type_to_json_schema(annotation)

                if param.default is inspect.Parameter.empty:
                    required.append(name)

            schema: dict[str, Any] = {
                "name": func.__name__,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }

            self._functions[func.__name__] = func
            self._schemas.append(schema)
            return func

        return decorator

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a registered tool by name with arguments."""
        if name not in self._functions:
            raise KeyError(f"unknown tool: {name}")
        return self._functions[name](**arguments)
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_tool_registry.py -v`
Expected: All 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/common/tool_registry.py tests/python/test_tool_registry.py
git -c commit.gpgsign=false commit -m "feat: add tool registry for converting Python functions to Claude API tool schemas"
```

---

## Task 2: Claude Agent Base Class

**Files:**
- Create: `agents/common/claude_agent.py`
- Create: `tests/python/test_claude_agent.py`

Extends AgentBase with a Claude API conversation loop that handles tool-use.

- [ ] **Step 1: Write failing tests**

```python
# tests/python/test_claude_agent.py
import json
import io
import sys
import uuid
from unittest.mock import MagicMock, patch

from agents.common.protocol import AgentConfig, ApiUsage, TaskEnvelope
from agents.common.tool_registry import ToolRegistry


def make_task(context: dict | None = None) -> str:
    task = TaskEnvelope(
        task_id=uuid.uuid4(),
        agent="test",
        context=context or {},
        config=AgentConfig(
            model="claude-sonnet-4-6-20250414",
            max_tokens=8192,
            timeout_seconds=300,
        ),
    )
    return task.model_dump_json()


def test_claude_agent_simple_text_response():
    """Agent returns text response without tool use."""
    from agents.common.claude_agent import ClaudeAgentBase

    class TestAgent(ClaudeAgentBase):
        def system_prompt(self) -> str:
            return "You are a test agent."

        def build_user_message(self, task: TaskEnvelope) -> str:
            return "Say hello"

    # Mock the Anthropic client
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="Hello!")]
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 10
    mock_response.usage.output_tokens = 5

    task_json = make_task()

    with patch("agents.common.claude_agent.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = mock_response

        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(task_json.encode()))
        captured = io.StringIO()
        sys.stdout = captured

        try:
            agent = TestAgent()
            agent.run()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout

    output = json.loads(captured.getvalue())
    assert output["status"] == "success"
    assert output["result"]["response"] == "Hello!"
    assert output["usage"]["input_tokens"] == 10


def test_claude_agent_with_tool_use():
    """Agent uses a tool and returns final response."""
    from agents.common.claude_agent import ClaudeAgentBase

    class TestAgent(ClaudeAgentBase):
        def system_prompt(self) -> str:
            return "You are a test agent."

        def build_user_message(self, task: TaskEnvelope) -> str:
            return "Add 3 and 4"

        def setup_tools(self, registry: ToolRegistry) -> None:
            @registry.tool(description="Add two numbers")
            def add(a: int, b: int) -> dict:
                return {"sum": a + b}

    # First response: tool use
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

    # Second response: final text
    mock_response_2 = MagicMock()
    mock_response_2.content = [MagicMock(type="text", text="The sum is 7")]
    mock_response_2.stop_reason = "end_turn"
    mock_response_2.usage.input_tokens = 30
    mock_response_2.usage.output_tokens = 10

    task_json = make_task()

    with patch("agents.common.claude_agent.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = [mock_response_1, mock_response_2]

        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(task_json.encode()))
        captured = io.StringIO()
        sys.stdout = captured

        try:
            agent = TestAgent()
            agent.run()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout

    output = json.loads(captured.getvalue())
    assert output["status"] == "success"
    assert output["result"]["response"] == "The sum is 7"
    # Tokens should be summed across both calls
    assert output["usage"]["input_tokens"] == 50
    assert output["usage"]["output_tokens"] == 25
    assert output["usage"]["api_calls"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_claude_agent.py -v`

- [ ] **Step 3: Implement ClaudeAgentBase**

```python
# agents/common/claude_agent.py
import json
from typing import Any

import anthropic

from agents.common.agent_base import AgentBase
from agents.common.protocol import ApiUsage, TaskEnvelope
from agents.common.tool_registry import ToolRegistry


class ClaudeAgentBase(AgentBase):
    """Base class for agents that use Claude API with tool-use.

    Subclasses must implement:
      - system_prompt() -> str
      - build_user_message(task) -> str

    Optionally override:
      - setup_tools(registry) to register tools
      - extract_result(response, task) to customize result extraction
    """

    MAX_TOOL_ROUNDS = 20

    def setup_tools(self, registry: ToolRegistry) -> None:
        """Override to register tools with the registry."""

    def system_prompt(self) -> str:
        raise NotImplementedError

    def build_user_message(self, task: TaskEnvelope) -> str:
        raise NotImplementedError

    def extract_result(self, final_text: str, task: TaskEnvelope) -> dict[str, Any]:
        """Extract structured result from the final text response."""
        return {"response": final_text}

    def execute(self, task: TaskEnvelope) -> tuple[dict[str, Any], ApiUsage]:
        client = anthropic.Anthropic()
        registry = ToolRegistry()
        self.setup_tools(registry)

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self.build_user_message(task)},
        ]

        total_usage = ApiUsage()
        api_kwargs: dict[str, Any] = {
            "model": task.config.model,
            "max_tokens": task.config.max_tokens,
            "system": self.system_prompt(),
            "messages": messages,
        }
        if registry.tools:
            api_kwargs["tools"] = registry.tools

        for _ in range(self.MAX_TOOL_ROUNDS):
            response = client.messages.create(**api_kwargs)
            total_usage.input_tokens += response.usage.input_tokens
            total_usage.output_tokens += response.usage.output_tokens
            total_usage.api_calls += 1

            if response.stop_reason != "tool_use":
                # Extract final text
                final_text = ""
                for block in response.content:
                    if block.type == "text":
                        final_text += block.text
                result = self.extract_result(final_text, task)
                return result, total_usage

            # Handle tool use
            assistant_content = []
            tool_results = []

            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

                    self.log(f"tool call: {block.name}({json.dumps(block.input)})")
                    try:
                        tool_output = registry.call(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(tool_output),
                        })
                    except Exception as exc:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"error": str(exc)}),
                            "is_error": True,
                        })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})
            api_kwargs["messages"] = messages

        raise RuntimeError(f"agent exceeded {self.MAX_TOOL_ROUNDS} tool rounds")
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_claude_agent.py -v`
Expected: Both tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/common/claude_agent.py tests/python/test_claude_agent.py
git -c commit.gpgsign=false commit -m "feat: add ClaudeAgentBase with API conversation loop and tool-use handling"
```

---

## Task 3: Game Selector Agent

**Files:**
- Create: `agents/game_selector/__init__.py`
- Create: `agents/game_selector/agent.py`
- Create: `agents/game_selector/tools.py`
- Create: `tests/python/test_game_selector.py`

The game selector picks which game to benchmark next. For v1, it queries the local Steam library and checks for built-in benchmarks.

- [ ] **Step 1: Write failing tests**

```python
# tests/python/test_game_selector.py
import json
import os

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

    # Shadow of the Tomb Raider has a built-in benchmark
    result = registry.call("check_benchmark_support", {"app_id": 750920})
    assert result["has_benchmark"] is True
    assert result["benchmark_args"] is not None

    # A game without known benchmark support
    result = registry.call("check_benchmark_support", {"app_id": 99999})
    assert result["has_benchmark"] is False
```

- [ ] **Step 2: Implement game selector tools**

```python
# agents/game_selector/__init__.py
```

```python
# agents/game_selector/tools.py
import os
import json
from typing import Any

from agents.common.tool_registry import ToolRegistry

# Known games with built-in benchmarks and their launch args
BENCHMARK_GAMES: dict[int, dict[str, Any]] = {
    750920: {
        "name": "Shadow of the Tomb Raider",
        "benchmark_args": ["--benchmark"],
        "workload_profile": "gpu_heavy",
    },
    1091500: {
        "name": "Cyberpunk 2077",
        "benchmark_args": ["--benchmark"],
        "workload_profile": "gpu_heavy",
    },
    435150: {
        "name": "Divinity: Original Sin 2",
        "benchmark_args": ["--benchmark"],
        "workload_profile": "cpu_heavy",
    },
    546560: {
        "name": "Half-Life: Alyx",
        "benchmark_args": ["+vr_benchmark", "1"],
        "workload_profile": "gpu_heavy",
    },
    1174180: {
        "name": "Red Dead Redemption 2",
        "benchmark_args": ["-benchmark"],
        "workload_profile": "gpu_heavy",
    },
}


def make_game_selector_tools(registry: ToolRegistry) -> None:
    """Register game selector tools with the registry."""

    @registry.tool(description="List installed Steam games from the local library. Returns app IDs and names.")
    def list_steam_games(library_path: str = "") -> dict:
        """Scan Steam library folders for installed games."""
        if not library_path:
            library_path = os.path.expanduser("~/.steam/steam/steamapps")

        games = []
        if os.path.isdir(library_path):
            for filename in os.listdir(library_path):
                if filename.startswith("appmanifest_") and filename.endswith(".acf"):
                    app_id = int(filename.replace("appmanifest_", "").replace(".acf", ""))
                    # Parse the ACF file for the game name
                    acf_path = os.path.join(library_path, filename)
                    name = _parse_acf_name(acf_path)
                    games.append({"app_id": app_id, "name": name})

        return {"games": games, "count": len(games)}

    @registry.tool(description="Check if a game has a built-in benchmark mode. Returns benchmark args if available.")
    def check_benchmark_support(app_id: int) -> dict:
        """Check if a game supports built-in benchmarking."""
        if app_id in BENCHMARK_GAMES:
            info = BENCHMARK_GAMES[app_id]
            return {
                "has_benchmark": True,
                "benchmark_args": info["benchmark_args"],
                "workload_profile": info["workload_profile"],
            }
        return {"has_benchmark": False, "benchmark_args": None, "workload_profile": "unknown"}

    @registry.tool(description="Get metadata about a game including size, last played, and optimization history.")
    def get_game_metadata(app_id: int) -> dict:
        """Get game metadata from Steam and optimization history."""
        # Check if we have benchmark info
        bench_info = BENCHMARK_GAMES.get(app_id, {})
        return {
            "app_id": app_id,
            "name": bench_info.get("name", f"Unknown ({app_id})"),
            "has_benchmark": app_id in BENCHMARK_GAMES,
            "workload_profile": bench_info.get("workload_profile", "unknown"),
        }


def _parse_acf_name(acf_path: str) -> str:
    """Parse game name from a Steam ACF manifest file."""
    try:
        with open(acf_path) as f:
            for line in f:
                line = line.strip()
                if '"name"' in line.lower():
                    parts = line.split('"')
                    if len(parts) >= 4:
                        return parts[3]
    except (OSError, IndexError):
        pass
    return "Unknown"
```

- [ ] **Step 3: Implement game selector agent**

```python
# agents/game_selector/agent.py
from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.game_selector.tools import make_game_selector_tools


class GameSelectorAgent(ClaudeAgentBase):
    """Selects the next game to benchmark for optimization."""

    def system_prompt(self) -> str:
        return """You are the Game Selector agent for Crucible, a Linux gaming performance optimization system.

Your job is to select the best game to benchmark next for performance optimization.

Priorities:
1. Games with built-in benchmarks (most reproducible)
2. Games that exercise code paths related to recent optimization work
3. Games that haven't been profiled recently
4. Mix of GPU-heavy and CPU-heavy workloads

Use your tools to query the Steam library, check benchmark support, and gather metadata.

Respond with your selection in this exact JSON format:
{"app_id": <int>, "name": "<string>", "benchmark_method": "builtin" or "ai_play", "benchmark_args": [...], "workload_profile": "<string>", "rationale": "<string>"}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        history = context.get("optimization_history", [])
        goals = context.get("optimization_goals", "general performance improvement")

        msg = f"Select the next game to benchmark.\n\nOptimization goals: {goals}\n"
        if history:
            msg += f"\nRecent optimization history:\n{history}\n"
        msg += "\nUse your tools to find installed games, check benchmark support, and select the best candidate."
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        make_game_selector_tools(registry)


if __name__ == "__main__":
    GameSelectorAgent().run()
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_game_selector.py -v`
Expected: Both tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/game_selector/ tests/python/test_game_selector.py
git -c commit.gpgsign=false commit -m "feat: add game selector agent with Steam library and benchmark detection tools"
```

---

## Task 4: Profiler Agent

**Files:**
- Create: `agents/profiler/__init__.py`
- Create: `agents/profiler/agent.py`
- Create: `agents/profiler/tools.py`
- Create: `tests/python/test_profiler.py`

The profiler configures and runs measurement collection inside the guest VM via the guest agent RPC.

- [ ] **Step 1: Write failing tests**

```python
# tests/python/test_profiler.py
from agents.profiler.tools import make_profiler_tools
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
    """PSI snapshot should read from /proc/pressure even without guest RPC."""
    registry = ToolRegistry()
    make_profiler_tools(registry, guest_rpc=None)
    result = registry.call("collect_psi_snapshot", {})
    assert "system_psi" in result
    # On a real Linux system, we should have cpu/memory/io
    assert "cpu" in result["system_psi"] or result.get("error") is not None


def test_parse_mangohud_csv():
    """Test MangoHud CSV parsing with sample data."""
    from agents.profiler.tools import parse_mangohud_csv

    csv_data = "fps,frametime,cpu_load,gpu_load\n60.1,16.6,45,80\n59.8,16.7,46,82\n"
    result = parse_mangohud_csv(csv_data)
    assert result["frame_count"] == 2
    assert abs(result["fps_avg"] - 59.95) < 0.1
```

- [ ] **Step 2: Implement profiler tools**

```python
# agents/profiler/__init__.py
```

```python
# agents/profiler/tools.py
import csv
import io
import json
import os
import subprocess
from typing import Any

from agents.common.tool_registry import ToolRegistry


def parse_mangohud_csv(csv_text: str) -> dict[str, Any]:
    """Parse MangoHud CSV output into summary metrics."""
    reader = csv.DictReader(io.StringIO(csv_text))
    fps_values = []
    frametime_values = []

    for row in reader:
        if "fps" in row and row["fps"]:
            fps_values.append(float(row["fps"]))
        if "frametime" in row and row["frametime"]:
            frametime_values.append(float(row["frametime"]))

    if not fps_values:
        return {"error": "no FPS data found", "frame_count": 0}

    fps_values.sort()
    frametime_values.sort()

    return {
        "frame_count": len(fps_values),
        "fps_avg": sum(fps_values) / len(fps_values),
        "fps_p1": fps_values[max(0, len(fps_values) // 100)],
        "fps_min": fps_values[0],
        "fps_max": fps_values[-1],
        "frametime_p50_ms": frametime_values[len(frametime_values) // 2] if frametime_values else 0,
        "frametime_p95_ms": frametime_values[int(len(frametime_values) * 0.95)] if frametime_values else 0,
        "frametime_p99_ms": frametime_values[int(len(frametime_values) * 0.99)] if frametime_values else 0,
    }


def make_profiler_tools(registry: ToolRegistry, guest_rpc: Any) -> None:
    """Register profiler tools. guest_rpc is a callable that sends commands to the guest agent."""

    @registry.tool(description="Start profiling session in the guest VM. Configures perfetto and MangoHud.")
    def start_profiling(perfetto_config: str = "", duration_secs: int = 60) -> dict:
        config = {"perfetto_config": perfetto_config, "duration_secs": duration_secs}
        if guest_rpc:
            return guest_rpc({"cmd": "start_profiling", "config": config})
        return {"status": "ok", "message": "profiling started (dry run)"}

    @registry.tool(description="Stop profiling and collect results. Returns paths to trace files and MangoHud logs.")
    def stop_profiling() -> dict:
        if guest_rpc:
            return guest_rpc({"cmd": "stop_profiling"})
        return {"status": "ok", "traces": [], "mangohud": None}

    @registry.tool(description="Collect a PSI (Pressure Stall Information) snapshot from /proc/pressure and per-cgroup.")
    def collect_psi_snapshot() -> dict:
        metrics: dict[str, Any] = {"system_psi": {}, "cgroup_psi": []}

        for resource in ("cpu", "memory", "io"):
            psi_path = f"/proc/pressure/{resource}"
            if os.path.exists(psi_path):
                with open(psi_path) as f:
                    for line in f:
                        if line.startswith("some"):
                            parts = line.split()
                            values = {}
                            for part in parts[1:]:
                                if "=" in part:
                                    k, v = part.split("=")
                                    values[k] = float(v)
                            metrics["system_psi"][resource] = values

        cgroup_root = "/sys/fs/cgroup/crucible"
        if os.path.exists(cgroup_root):
            for group in os.listdir(cgroup_root):
                group_path = os.path.join(cgroup_root, group)
                if not os.path.isdir(group_path):
                    continue
                cgroup_metrics: dict[str, Any] = {"group": group, "psi": {}}
                for resource in ("cpu", "memory", "io"):
                    psi_file = os.path.join(group_path, f"{resource}.pressure")
                    if os.path.exists(psi_file):
                        with open(psi_file) as f:
                            for line in f:
                                if line.startswith("some"):
                                    parts = line.split()
                                    values = {}
                                    for part in parts[1:]:
                                        if "=" in part:
                                            k, v = part.split("=")
                                            values[k] = float(v)
                                    cgroup_metrics["psi"][resource] = values
                metrics["cgroup_psi"].append(cgroup_metrics)

        return metrics

    @registry.tool(description="Read and parse a MangoHud CSV log file. Returns frame time statistics.")
    def read_mangohud_log(log_path: str = "/tmp/crucible_mangohud.csv") -> dict:
        if not os.path.exists(log_path):
            return {"error": f"log not found: {log_path}"}
        with open(log_path) as f:
            return parse_mangohud_csv(f.read())

    @registry.tool(description="Get system metrics from the guest VM including GPU utilization.")
    def get_guest_metrics() -> dict:
        if guest_rpc:
            return guest_rpc({"cmd": "get_metrics"})
        # Fallback: read local PSI
        return collect_psi_snapshot()
```

- [ ] **Step 3: Implement profiler agent**

```python
# agents/profiler/agent.py
from typing import Any

from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.profiler.tools import make_profiler_tools


class ProfilerAgent(ClaudeAgentBase):
    """Configures and collects performance measurements."""

    def system_prompt(self) -> str:
        return """You are the Profiler agent for Crucible, a Linux gaming performance optimization system.

Your job is to configure and run performance measurement for a game benchmark session.

You decide which profiling tools to deploy based on the optimization target:
- For scheduler investigations: enable sched tracepoints in perfetto
- For memory pressure: focus on PSI and page fault counters
- For GPU bottlenecks: ensure MangoHud captures frame times and GPU load
- Always collect system-wide and per-cgroup PSI

After collecting data, summarize the key metrics in this JSON format:
{"fps_avg": <float>, "fps_p1": <float>, "frame_time_p99_ms": <float>, "psi_cpu_avg": <float>, "psi_memory_avg": <float>, "collection_paths": {"traces": [...], "mangohud": "<path>"}}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        phase = context.get("phase", "baseline")
        game = context.get("game_name", "unknown")
        hypothesis = context.get("optimization_hypothesis", "")

        msg = f"Collect {phase} measurements for {game}.\n"
        if hypothesis:
            msg += f"\nCurrent optimization hypothesis: {hypothesis}\n"
            msg += "Configure profiling to capture data relevant to this hypothesis.\n"
        msg += "\nUse your tools to start profiling, wait for the benchmark to complete, then collect results."
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        guest_rpc = self._guest_rpc if hasattr(self, "_guest_rpc") else None
        make_profiler_tools(registry, guest_rpc=guest_rpc)


if __name__ == "__main__":
    ProfilerAgent().run()
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_profiler.py -v`
Expected: All 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/profiler/ tests/python/test_profiler.py
git -c commit.gpgsign=false commit -m "feat: add profiler agent with PSI, MangoHud, and perfetto tools"
```

---

## Task 5: Analyzer Agent

**Files:**
- Create: `agents/analyzer/__init__.py`
- Create: `agents/analyzer/agent.py`
- Create: `agents/analyzer/tools.py`
- Create: `tests/python/test_analyzer.py`

The analyzer interprets profiling data and identifies performance bottlenecks.

- [ ] **Step 1: Write failing tests**

```python
# tests/python/test_analyzer.py
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
    assert result["fps_avg"]["delta_pct"] > 0  # improvement
    assert result["frame_time_p99_ms"]["delta_pct"] < 0  # lower is better


def test_compare_measurements_regression():
    registry = ToolRegistry()
    make_analyzer_tools(registry)

    result = registry.call("compare_measurements", {
        "baseline": {"fps_avg": 60.0, "frame_time_p99_ms": 25.0},
        "comparison": {"fps_avg": 55.0, "frame_time_p99_ms": 30.0},
    })
    assert result["fps_avg"]["delta_pct"] < 0  # regression
```

- [ ] **Step 2: Implement analyzer tools**

```python
# agents/analyzer/__init__.py
```

```python
# agents/analyzer/tools.py
import json
import os
import subprocess
from typing import Any

from agents.common.tool_registry import ToolRegistry

# Metrics where lower is better
LOWER_IS_BETTER = {"frame_time_p99_ms", "frame_time_p95_ms", "frame_time_p50_ms", "psi_cpu_avg", "psi_memory_avg", "psi_io_avg"}


def make_analyzer_tools(registry: ToolRegistry) -> None:
    """Register analyzer tools."""

    @registry.tool(description="Read a file from disk. Use for reading source code, config files, or profiling output.")
    def read_file(path: str, max_lines: int = 500) -> dict:
        if not os.path.exists(path):
            return {"error": f"file not found: {path}"}
        try:
            with open(path) as f:
                lines = f.readlines()
            content = "".join(lines[:max_lines])
            return {"content": content, "total_lines": len(lines), "truncated": len(lines) > max_lines}
        except Exception as exc:
            return {"error": str(exc)}

    @registry.tool(description="Run a SQL query against a perfetto trace using trace_processor_shell. Returns query results.")
    def run_trace_processor_query(trace_path: str, query: str) -> dict:
        if not os.path.exists(trace_path):
            return {"error": f"trace not found: {trace_path}"}
        try:
            result = subprocess.run(
                ["trace_processor_shell", trace_path, "-q", query],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return {"error": result.stderr}
            return {"output": result.stdout, "rows": result.stdout.strip().count("\n")}
        except FileNotFoundError:
            return {"error": "trace_processor_shell not found"}
        except subprocess.TimeoutExpired:
            return {"error": "query timed out"}

    @registry.tool(description="Compare baseline and comparison measurements. Computes delta percentages for each metric.")
    def compare_measurements(baseline: dict, comparison: dict) -> dict:
        results = {}
        all_keys = set(baseline.keys()) | set(comparison.keys())
        for key in all_keys:
            if key not in baseline or key not in comparison:
                continue
            base_val = baseline[key]
            comp_val = comparison[key]
            if not isinstance(base_val, (int, float)) or not isinstance(comp_val, (int, float)):
                continue
            if base_val == 0:
                continue

            delta = comp_val - base_val
            delta_pct = (delta / abs(base_val)) * 100

            # For lower-is-better metrics, negate so positive = improvement
            improved = delta_pct < 0 if key in LOWER_IS_BETTER else delta_pct > 0

            results[key] = {
                "baseline": base_val,
                "comparison": comp_val,
                "delta": delta,
                "delta_pct": delta_pct,
                "improved": improved,
            }
        return results

    @registry.tool(description="Search for a pattern in source files under a directory. Like grep -rn.")
    def search_source(directory: str, pattern: str, file_glob: str = "*.c") -> dict:
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include", file_glob, pattern, directory],
                capture_output=True, text=True, timeout=30,
            )
            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
            return {"matches": lines[:50], "total_matches": len(lines), "truncated": len(lines) > 50}
        except Exception as exc:
            return {"error": str(exc)}
```

- [ ] **Step 3: Implement analyzer agent**

```python
# agents/analyzer/agent.py
from typing import Any

from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.analyzer.tools import make_analyzer_tools


class AnalyzerAgent(ClaudeAgentBase):
    """Analyzes profiling data to identify performance bottlenecks."""

    def system_prompt(self) -> str:
        return """You are the Analyzer agent for Crucible, a Linux gaming performance optimization system.

Your job is to analyze profiling data from a game benchmark session and identify performance bottlenecks.

You have access to:
- Profiling traces (perfetto) -- query with SQL via trace_processor
- MangoHud frame time logs
- PSI (Pressure Stall Information) data
- Kernel and userspace source code

Your analysis should:
1. Identify the primary bottleneck (CPU, GPU, memory, IO)
2. Correlate frame time spikes with system events
3. Examine per-cgroup PSI to isolate which component (game, compositor, wine, mesa) is under pressure
4. Look at specific kernel or userspace code paths if relevant

Respond with your analysis in this JSON format:
{"bottleneck": "<subsystem>", "severity": "high|medium|low", "root_cause": "<description>", "evidence": "<what data supports this>", "optimization_targets": [{"subsystem": "<str>", "component": "<str>", "suggestion": "<str>", "confidence": <float 0-1>}]}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        game = context.get("game_name", "unknown")
        metrics = context.get("metrics", {})
        trace_paths = context.get("trace_paths", [])
        hypothesis = context.get("previous_hypothesis", "")

        msg = f"Analyze profiling data for {game}.\n\n"
        if metrics:
            msg += f"Summary metrics:\n{json.dumps(metrics, indent=2)}\n\n"
        if trace_paths:
            msg += f"Trace files available: {trace_paths}\n\n"
        if hypothesis:
            msg += f"Previous hypothesis (attempt failed): {hypothesis}\nPlease investigate a different angle.\n"
        msg += "Use your tools to dig into the data and identify the primary bottleneck."
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        make_analyzer_tools(registry)


if __name__ == "__main__":
    import json
    AnalyzerAgent().run()
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_analyzer.py -v`
Expected: All 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/analyzer/ tests/python/test_analyzer.py
git -c commit.gpgsign=false commit -m "feat: add analyzer agent with trace processing and measurement comparison tools"
```

---

## Task 6: Optimizer Agent

**Files:**
- Create: `agents/optimizer/__init__.py`
- Create: `agents/optimizer/agent.py`
- Create: `agents/optimizer/tools.py`
- Create: `tests/python/test_optimizer.py`

The optimizer generates code changes based on the analyzer's bottleneck report.

- [ ] **Step 1: Write failing tests**

```python
# tests/python/test_optimizer.py
import os
import tempfile

from agents.optimizer.tools import make_optimizer_tools
from agents.common.tool_registry import ToolRegistry


def test_optimizer_tools_registered():
    registry = ToolRegistry()
    make_optimizer_tools(registry, kernel_src="/tmp")
    names = [t["name"] for t in registry.tools]
    assert "read_source_file" in names
    assert "write_patch" in names
    assert "apply_sysctl" in names
    assert "search_kernel_source" in names


def test_write_patch():
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        make_optimizer_tools(registry, kernel_src=tmp)
        result = registry.call("write_patch", {
            "filename": "test.diff",
            "content": "--- a/kernel/sched/core.c\n+++ b/kernel/sched/core.c\n@@ -1 +1 @@\n-old\n+new\n",
        })
        assert result["status"] == "ok"
        patch_path = result["path"]
        assert os.path.exists(patch_path)
        with open(patch_path) as f:
            assert "old" in f.read()


def test_read_source_file():
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        # Create a fake source file
        src_file = os.path.join(tmp, "test.c")
        with open(src_file, "w") as f:
            f.write("int main() { return 0; }\n")

        make_optimizer_tools(registry, kernel_src=tmp)
        result = registry.call("read_source_file", {"path": "test.c"})
        assert "int main" in result["content"]
```

- [ ] **Step 2: Implement optimizer tools**

```python
# agents/optimizer/__init__.py
```

```python
# agents/optimizer/tools.py
import os
import subprocess
import tempfile
from typing import Any

from agents.common.tool_registry import ToolRegistry


def make_optimizer_tools(registry: ToolRegistry, kernel_src: str) -> None:
    """Register optimizer tools."""

    patches_dir = os.path.join(kernel_src, ".crucible_patches")
    os.makedirs(patches_dir, exist_ok=True)

    @registry.tool(description="Read a source file relative to the kernel source tree or an absolute path.")
    def read_source_file(path: str, start_line: int = 0, max_lines: int = 200) -> dict:
        # Resolve relative paths against kernel source
        if not os.path.isabs(path):
            full_path = os.path.join(kernel_src, path)
        else:
            full_path = path

        if not os.path.exists(full_path):
            return {"error": f"file not found: {path}"}

        try:
            with open(full_path) as f:
                lines = f.readlines()
            selected = lines[start_line:start_line + max_lines]
            return {
                "content": "".join(selected),
                "total_lines": len(lines),
                "start_line": start_line,
                "lines_returned": len(selected),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @registry.tool(description="Search kernel or userspace source for a pattern. Returns matching lines with file paths.")
    def search_kernel_source(pattern: str, file_glob: str = "*.c", subdirectory: str = "") -> dict:
        search_dir = os.path.join(kernel_src, subdirectory) if subdirectory else kernel_src
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include", file_glob, pattern, search_dir],
                capture_output=True, text=True, timeout=30,
            )
            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
            return {"matches": lines[:50], "total_matches": len(lines)}
        except Exception as exc:
            return {"error": str(exc)}

    @registry.tool(description="Write a unified diff patch file. Returns the path where the patch was saved.")
    def write_patch(filename: str, content: str) -> dict:
        patch_path = os.path.join(patches_dir, filename)
        try:
            with open(patch_path, "w") as f:
                f.write(content)
            return {"status": "ok", "path": patch_path}
        except Exception as exc:
            return {"error": str(exc)}

    @registry.tool(description="Apply a sysctl tuning parameter. Returns the old and new values.")
    def apply_sysctl(key: str, value: str) -> dict:
        try:
            # Read current value
            result = subprocess.run(
                ["sysctl", "-n", key], capture_output=True, text=True,
            )
            old_value = result.stdout.strip() if result.returncode == 0 else "unknown"

            # Apply new value
            result = subprocess.run(
                ["sysctl", "-w", f"{key}={value}"], capture_output=True, text=True,
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()}

            return {"key": key, "old_value": old_value, "new_value": value}
        except Exception as exc:
            return {"error": str(exc)}

    @registry.tool(description="List files in a directory relative to the kernel source tree.")
    def list_source_files(path: str = "", file_glob: str = "*.c") -> dict:
        search_dir = os.path.join(kernel_src, path) if path else kernel_src
        if not os.path.isdir(search_dir):
            return {"error": f"directory not found: {path}"}
        try:
            result = subprocess.run(
                ["find", search_dir, "-maxdepth", "2", "-name", file_glob, "-type", "f"],
                capture_output=True, text=True, timeout=10,
            )
            files = result.stdout.strip().split("\n") if result.stdout.strip() else []
            # Make paths relative to kernel_src
            relative = [os.path.relpath(f, kernel_src) for f in files]
            return {"files": relative[:100], "total": len(files)}
        except Exception as exc:
            return {"error": str(exc)}
```

- [ ] **Step 3: Implement optimizer agent**

```python
# agents/optimizer/agent.py
import json
from typing import Any

from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.optimizer.tools import make_optimizer_tools


class OptimizerAgent(ClaudeAgentBase):
    """Generates code changes to optimize gaming performance."""

    def system_prompt(self) -> str:
        return """You are the Optimizer agent for Crucible, a Linux gaming performance optimization system.

Your job is to generate code changes that address a specific performance bottleneck identified by the Analyzer agent.

You can generate changes at three layers:
1. **Kernel**: Scheduler, memory management, IO subsystem patches
2. **Userspace**: Mesa, Wine/Proton, gamescope, compositor changes
3. **Tuning**: sysctl parameters, cgroup configurations

Guidelines:
- Read the relevant source code before making changes
- Make minimal, targeted changes -- one concern per patch
- Explain your reasoning in the patch description
- Consider side effects on other workloads
- Prefer well-understood optimizations over speculative ones

Respond with your changes in this JSON format:
{"layer": "kernel|userspace|tuning", "patches": [{"filename": "<name>.diff", "description": "<what and why>", "risk": "low|medium|high"}], "sysctl_changes": [{"key": "<key>", "value": "<value>", "rationale": "<why>"}], "rationale": "<overall approach>"}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        bottleneck = context.get("bottleneck", {})
        game = context.get("game_name", "unknown")
        attempt = context.get("attempt_number", 1)
        kernel_src = context.get("kernel_src", "/home/void/upstream/linux")

        msg = f"Generate optimizations for {game} (attempt {attempt}).\n\n"
        msg += f"Bottleneck analysis:\n{json.dumps(bottleneck, indent=2)}\n\n"
        if attempt > 1:
            previous = context.get("previous_attempts", [])
            msg += f"Previous attempts that did not work:\n{json.dumps(previous, indent=2)}\n"
            msg += "Try a different approach.\n\n"
        msg += f"Kernel source is at: {kernel_src}\n"
        msg += "Use your tools to read source code, understand the bottleneck, and generate patches."
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        kernel_src = "/home/void/upstream/linux"
        make_optimizer_tools(registry, kernel_src=kernel_src)


if __name__ == "__main__":
    OptimizerAgent().run()
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/test_optimizer.py -v`
Expected: All 3 tests pass.

- [ ] **Step 5: Run full test suite**

Run: `cargo test && PYTHONPATH=/home/void/upstream/crucible python3 -m pytest tests/python/ -v`
Expected: All Rust and Python tests pass.

- [ ] **Step 6: Commit**

```bash
git add agents/optimizer/ tests/python/test_optimizer.py
git -c commit.gpgsign=false commit -m "feat: add optimizer agent with source reading, patch generation, and sysctl tools"
```

---

## Completion Checklist

- [ ] `cargo test` -- all Rust tests pass
- [ ] `python3 -m pytest tests/python/ -v` -- all Python tests pass
- [ ] ToolRegistry converts Python functions to Claude API tool schemas
- [ ] ClaudeAgentBase handles multi-turn tool-use conversations
- [ ] Each agent has: system prompt, tools, build_user_message, extract_result
- [ ] Each agent is runnable via `python3 -m agents.<name>.agent`

## Next Plan

- **Plan 4: Orchestration Loop** -- state machine driving the full cycle: select game -> boot VM -> baseline measurement -> analyze -> optimize -> comparison measurement -> evaluate -> accept/reject
