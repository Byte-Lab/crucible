import json
from typing import Any

import anthropic

from agents.common.agent_base import AgentBase
from agents.common.guest_rpc import GuestRpc
from agents.common.protocol import ApiUsage, TaskEnvelope
from agents.common.tool_registry import ToolRegistry


class ClaudeAgentBase(AgentBase):
    MAX_TOOL_ROUNDS = 20

    def setup_tools(self, registry: ToolRegistry) -> None:
        pass

    def system_prompt(self) -> str:
        raise NotImplementedError

    def build_user_message(self, task: TaskEnvelope) -> str:
        raise NotImplementedError

    def extract_result(self, final_text: str, task: TaskEnvelope) -> dict[str, Any]:
        return {"response": final_text}

    def execute(self, task: TaskEnvelope) -> tuple[dict[str, Any], ApiUsage]:
        client = anthropic.Anthropic()
        registry = ToolRegistry()
        # Expose a guest-RPC client to tools when the orchestrator threaded
        # a vsock CID through context. Tests construct TaskEnvelope without
        # this key, in which case tools fall back to dry-run behavior.
        cid = task.context.get("vsock_cid")
        self._guest_rpc = GuestRpc(int(cid)) if isinstance(cid, int) else None
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
                final_text = ""
                for block in response.content:
                    if block.type == "text":
                        final_text += block.text
                return self.extract_result(final_text, task), total_usage

            assistant_content = []
            tool_results = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use", "id": block.id,
                        "name": block.name, "input": block.input,
                    })
                    self.log(f"tool call: {block.name}({json.dumps(block.input)})")
                    try:
                        tool_output = registry.call(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(tool_output),
                        })
                    except Exception as exc:
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps({"error": str(exc)}), "is_error": True,
                        })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})
            api_kwargs["messages"] = messages

        raise RuntimeError(f"agent exceeded {self.MAX_TOOL_ROUNDS} tool rounds")
