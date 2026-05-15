import asyncio
import json
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from agents.common.agent_base import AgentBase
from agents.common.guest_rpc import GuestRpc
from agents.common.protocol import ApiUsage, TaskEnvelope
from agents.common.tool_registry import ToolRegistry


# Built-in tools the bundled `claude` CLI would otherwise offer to the model.
# We don't want the host's Read/Edit/Bash leaking into agent runs (the
# optimizer in particular has its own kernel-tree-scoped read_file/edit_file).
# `tools=[]` on ClaudeAgentOptions is the canonical way to disable all
# built-ins; this list is belt-and-braces in case the SDK adds new builtins
# that the empty-list switch doesn't yet cover.
_BUILTIN_TOOLS_TO_DISALLOW = [
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "Read",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
]


class ClaudeAgentBase(AgentBase):
    # 40 agentic turns. Same rationale as before: synthetic optimizer chains
    # of read → edit_file × N → finalize_patch are chatty. The real safety
    # net is `task.config.timeout_seconds` (default 600s); this cap just
    # bounds runaway tool loops.
    MAX_TOOL_ROUNDS = 40

    def setup_tools(self, registry: ToolRegistry) -> None:
        pass

    def system_prompt(self) -> str:
        raise NotImplementedError

    def build_user_message(self, task: TaskEnvelope) -> str:
        raise NotImplementedError

    def extract_result(self, final_text: str, task: TaskEnvelope) -> dict[str, Any]:
        return {"response": final_text}

    def execute(self, task: TaskEnvelope) -> tuple[dict[str, Any], ApiUsage]:
        registry = ToolRegistry()
        # Stash the task so subclasses' setup_tools can read context fields
        # (e.g. the optimizer reads kernel_src to point its file tools at the
        # right tree). Avoids threading task through every setup_tools sig.
        self._task = task
        # Expose a guest-RPC client to tools when the orchestrator threaded
        # a vsock CID through context. Tests construct TaskEnvelope without
        # this key, in which case tools fall back to dry-run behavior.
        cid = task.context.get("vsock_cid")
        self._guest_rpc = GuestRpc(int(cid)) if isinstance(cid, int) else None
        self.setup_tools(registry)
        return asyncio.run(self._run(task, registry))

    async def _run(
        self, task: TaskEnvelope, registry: ToolRegistry
    ) -> tuple[dict[str, Any], ApiUsage]:
        sdk_tools = [
            tool(t["name"], t["description"], t["input_schema"])(
                _make_handler(registry, t["name"])
            )
            for t in registry.tools
        ]

        options_kwargs: dict[str, Any] = {
            "model": task.config.model,
            "system_prompt": self.system_prompt(),
            "max_turns": self.MAX_TOOL_ROUNDS,
            # Disable all built-in tools — agents must drive everything
            # through registry tools (which are MCP-namespaced below).
            "tools": [],
            "disallowed_tools": _BUILTIN_TOOLS_TO_DISALLOW,
            # No interactive prompts; we're spawned as a subprocess.
            "permission_mode": "bypassPermissions",
            # Don't load the user's CLAUDE.md, project settings, or plugins
            # into the agent's context. Isolation matters here — we want
            # only the system prompt the agent explicitly provides.
            "setting_sources": [],
            # `task.config.max_retries` historically controlled the
            # anthropic SDK's retry count. Threading it through to the
            # bundled `claude` CLI keeps the same knob meaningful.
            #
            # Scrub ANTHROPIC_API_KEY: the SDK merges parent env into the
            # CLI subprocess, so a leaked console key in the host env
            # silently overrides the user's OAuth Pro/Max session and bills
            # against the wrong account (or fails with `billing_error` when
            # the leaked account has no credit). An empty value tells the
            # CLI to fall back to its stored OAuth credentials.
            "env": {
                "CLAUDE_CODE_MAX_RETRIES": str(task.config.max_retries),
                "ANTHROPIC_API_KEY": "",
            },
        }
        if sdk_tools:
            options_kwargs["mcp_servers"] = {
                "crucible": create_sdk_mcp_server(
                    name="crucible", version="0.1.0", tools=sdk_tools
                ),
            }
            options_kwargs["allowed_tools"] = [
                f"mcp__crucible__{t['name']}" for t in registry.tools
            ]

        options = ClaudeAgentOptions(**options_kwargs)

        final_text = ""
        usage = ApiUsage()
        async for msg in query(
            prompt=self.build_user_message(task), options=options
        ):
            if isinstance(msg, AssistantMessage):
                if msg.error:
                    raise RuntimeError(f"assistant error: {msg.error}")
                turn_text = ""
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        turn_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        self.log(
                            f"tool call: {block.name}({json.dumps(block.input)})"
                        )
                # Each assistant turn overwrites — the final non-empty turn
                # before ResultMessage wins. Mirrors the old code's
                # stop_reason != "tool_use" final-text capture.
                if turn_text:
                    final_text = turn_text
            elif isinstance(msg, ResultMessage):
                if msg.usage:
                    usage.input_tokens = int(msg.usage.get("input_tokens", 0))
                    usage.output_tokens = int(msg.usage.get("output_tokens", 0))
                usage.api_calls = msg.num_turns
                if msg.is_error:
                    err = (msg.errors or [msg.stop_reason or "unknown"])[0] or "unknown"
                    raise RuntimeError(f"agent failed: {err}")
                if not final_text and msg.result:
                    final_text = msg.result

        return self.extract_result(final_text, task), usage


def _make_handler(registry: ToolRegistry, name: str):
    """Return an async MCP tool handler that delegates to the sync registry.

    Bound in a separate factory to capture `name` correctly (avoid the
    classic late-binding loop closure pitfall)."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = registry.call(name, args)
            return {
                "content": [
                    {"type": "text", "text": json.dumps(result, default=str)}
                ]
            }
        except Exception as exc:
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"error": str(exc)})}
                ],
                "is_error": True,
            }

    return handler
