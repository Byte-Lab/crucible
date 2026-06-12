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
        game = context.get("game", context.get("game_name", "unknown"))
        hypothesis = context.get("optimization_hypothesis", "")
        workload_kind = context.get("workload_kind", "game")

        if workload_kind == "synthetic":
            args = context.get("benchmark_args", ["--cpu", "2"])
            duration = context.get("duration_secs", 30)
            msg = (
                f"Collect {phase} measurements via the synthetic CPU workload.\n"
                f"Call run_benchmark(name='stress-ng', args={args!r}, "
                f"duration_secs={duration}) exactly once. After it returns:\n"
                "  fps_avg = 0.0\n"
                "  fps_p1 = 0.0\n"
                "  frame_time_p99_ms = 1000.0 / ops_per_sec when ops_per_sec > 0, "
                "else 0.0\n"
                "  psi_cpu_avg = psi_cpu_delta from the tool result\n"
                "  psi_memory_avg = psi_memory_delta from the tool result\n"
                "Emit only the final JSON object described in the system prompt; "
                "set collection_paths to {} for synthetic runs."
            )
            if hypothesis:
                msg = f"Hypothesis: {hypothesis}\n" + msg
            return msg

        benchmark = context.get("game_benchmark")
        if benchmark:
            duration = int(context.get("duration_secs") or 30)
            # One scene with an explicit duration: vkmark's full default
            # suite runs for minutes, and MangoHud's log window (sized from
            # duration_secs in the guest) must elapse before the app exits.
            if benchmark == "vkmark":
                args = ["-b", f"vertex:duration={duration}"]
            else:
                args = ["-b", f"build:duration={duration}"]
            mangohud_output = context.get(
                "mangohud_output", "/tmp/crucible_mangohud.csv"
            )
            msg = (
                f"Collect {phase} measurements via the native GPU benchmark.\n"
                f"1. Call launch_benchmark(name={benchmark!r}, args={args!r}, "
                f"mangohud_output={mangohud_output!r}, "
                f"duration_secs={duration}) exactly once.\n"
                f"2. Call fetch_mangohud_log(log_path={mangohud_output!r}) to "
                "retrieve frame statistics.\n"
                "Then emit the final JSON object from the system prompt with:\n"
                "  fps_avg = fps_avg from fetch_mangohud_log\n"
                "  fps_p1 = fps_p1 from fetch_mangohud_log\n"
                "  frame_time_p99_ms = frametime_p99_ms from fetch_mangohud_log\n"
                "  psi_cpu_avg = psi_cpu_delta from the launch_benchmark result\n"
                "  psi_memory_avg = psi_memory_delta from the launch_benchmark "
                "result\n"
                "Set collection_paths to "
                f'{{"mangohud": {mangohud_output!r}}}.\n'
                "If launch_benchmark or fetch_mangohud_log returns an error, "
                "or launch_benchmark reports log_found=false, do NOT invent "
                "metrics and do NOT emit zeros: respond with "
                '{"error": "<what failed and the exact tool error>"} instead. '
                "A zero fps_avg from a real run is impossible and would be "
                "silently accepted as a measurement."
            )
            if hypothesis:
                msg = f"Hypothesis: {hypothesis}\n" + msg
            return msg

        msg = f"Collect {phase} measurements for {game}.\n"
        if hypothesis:
            msg += f"Hypothesis: {hypothesis}\nConfigure profiling relevant to this.\n"
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        guest_rpc = getattr(self, "_guest_rpc", None)
        make_profiler_tools(registry, guest_rpc=guest_rpc)


if __name__ == "__main__":
    ProfilerAgent().run()
