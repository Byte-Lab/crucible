from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.profiler.tools import make_profiler_tools


class ProfilerAgent(ClaudeAgentBase):
    def system_prompt(self) -> str:
        return """You are the Profiler agent for Crucible. Configure and collect performance measurements.
Decide which tools to deploy based on the optimization target.
Respond with JSON: {"fps_avg": <float>, "fps_p1": <float>, "frame_time_p99_ms": <float>, "psi_cpu_avg": <float>, "psi_memory_avg": <float>, "collection_paths": {"traces": [...], "mangohud": "<path>"}}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        phase = context.get("phase", "baseline")
        game = context.get("game_name", "unknown")
        hypothesis = context.get("optimization_hypothesis", "")
        msg = f"Collect {phase} measurements for {game}.\n"
        if hypothesis:
            msg += f"Hypothesis: {hypothesis}\nConfigure profiling relevant to this.\n"
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        guest_rpc = getattr(self, "_guest_rpc", None)
        make_profiler_tools(registry, guest_rpc=guest_rpc)


if __name__ == "__main__":
    ProfilerAgent().run()
