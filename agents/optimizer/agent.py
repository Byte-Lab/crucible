import json
from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.optimizer.tools import make_optimizer_tools


class OptimizerAgent(ClaudeAgentBase):
    def system_prompt(self) -> str:
        return """You are the Optimizer agent for Crucible. Generate code changes to address performance bottlenecks.
Layers: kernel (scheduler, memory, IO), userspace (Mesa, Wine, gamescope), tuning (sysctl).
Make minimal, targeted changes. Explain reasoning.
Respond with JSON: {"layer": "kernel|userspace|tuning", "patches": [{"filename": "<name>.diff", "description": "<what>", "risk": "low|medium|high"}], "sysctl_changes": [...], "rationale": "<approach>"}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        bottleneck = context.get("bottleneck", {})
        game = context.get("game_name", "unknown")
        attempt = context.get("attempt_number", 1)
        kernel_src = context.get("kernel_src", "/home/void/upstream/linux")
        msg = f"Generate optimizations for {game} (attempt {attempt}).\n"
        msg += f"Bottleneck:\n{json.dumps(bottleneck, indent=2)}\n"
        if attempt > 1:
            msg += f"Previous attempts: {json.dumps(context.get('previous_attempts', []))}\nTry different approach.\n"
        msg += f"Kernel source: {kernel_src}\n"
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        make_optimizer_tools(registry, kernel_src="/home/void/upstream/linux")

if __name__ == "__main__":
    OptimizerAgent().run()
