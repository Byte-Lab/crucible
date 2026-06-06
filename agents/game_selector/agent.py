from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.game_selector.tools import make_game_selector_tools


class GameSelectorAgent(ClaudeAgentBase):
    def system_prompt(self) -> str:
        return """You are the Game Selector agent for Crucible, a Linux gaming performance optimization system.
Your job is to select the best game to benchmark next.
Priorities: 1) Games with built-in benchmarks 2) Games exercising recent optimization targets 3) Mix of GPU/CPU workloads
Respond with JSON: {"app_id": <int>, "name": "<str>", "benchmark_method": "builtin"|"ai_play", "benchmark_args": [...], "workload_profile": "<str>", "rationale": "<str>"}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        history = context.get("optimization_history", [])
        goals = context.get("optimization_goals", "general performance improvement")
        msg = f"Select the next game to benchmark.\nGoals: {goals}\n"
        if history:
            msg += f"Recent history: {history}\n"
        if context.get("workload_kind") == "game":
            msg += (
                "The guest rootfs has no Steam library. Call "
                "list_native_benchmarks and select one of the native GPU "
                "benchmarks instead. Respond with app_id 0, the benchmark "
                'binary name as "name", benchmark_method "builtin", and its '
                "benchmark_args."
            )
            return msg
        msg += "Use tools to find installed games and check benchmark support."
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        make_game_selector_tools(registry)


if __name__ == "__main__":
    GameSelectorAgent().run()
