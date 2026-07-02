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

        steam_app_id = context.get("steam_app_id")
        if workload_kind == "steam" and steam_app_id:
            duration = int(context.get("duration_secs") or 60)
            mangohud_output = context.get(
                "mangohud_output", "/tmp/crucible_mangohud.csv"
            )
            # v1 measures uncapped main-menu rendering (-novid skips the
            # intro video); demo-driven timedemo passes land once a demo
            # file ships in the rootfs.
            args = ["-novid"]
            msg = (
                f"Collect {phase} measurements from the Steam title.\n"
                f"1. Call launch_steam_benchmark(app_id={steam_app_id}, "
                f"args={args!r}, mangohud_output={mangohud_output!r}, "
                f"duration_secs={duration}) exactly once. It launches the "
                "game under weston-headless + MangoHud and blocks until the "
                "frame log is complete (game load can take minutes).\n"
                f"2. Call fetch_mangohud_log(log_path={mangohud_output!r}) to "
                "retrieve frame statistics.\n"
                "Then emit the final JSON object from the system prompt with:\n"
                "  fps_avg = fps_avg from fetch_mangohud_log\n"
                "  fps_p1 = fps_p1 from fetch_mangohud_log\n"
                "  frame_time_p99_ms = frametime_p99_ms from fetch_mangohud_log\n"
                "  psi_cpu_avg = psi_cpu_delta from launch_steam_benchmark\n"
                "  psi_memory_avg = psi_memory_delta from launch_steam_benchmark\n"
                f'Set collection_paths to {{"mangohud": {mangohud_output!r}}}.\n'
                "If either tool returns an error or log_found is false, do "
                "NOT invent metrics and do NOT emit zeros: respond with "
                '{"error": "<what failed and the exact tool error>"} instead.'
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
            capture_perfetto = bool(context.get("capture_perfetto"))
            perfetto_output = context.get(
                "perfetto_output", "/tmp/crucible_trace.perfetto-trace"
            )
            perfetto_host_dir = context.get("perfetto_host_dir", "/tmp")
            step = 1
            lines = [f"Collect {phase} measurements via the native GPU benchmark."]
            lines.append(
                f"{step}. Call launch_benchmark(name={benchmark!r}, args={args!r}, "
                f"mangohud_output={mangohud_output!r}, "
                f"duration_secs={duration}) exactly once. This clean run is "
                "the measurement — report its numbers."
            )
            step += 1
            lines.append(
                f"{step}. Call fetch_mangohud_log(log_path={mangohud_output!r}) to "
                "retrieve frame statistics."
            )
            step += 1
            if capture_perfetto:
                # The clean run doubles as warmup; the profiled repeat runs
                # against hot caches, and its trace is what the analyzer
                # mines for kernel bottlenecks.
                lines.append(
                    f"{step}. Call start_profiling(duration_secs={duration + 30}, "
                    f"output={perfetto_output!r}) to begin a Perfetto "
                    "kernel-scheduler trace (it auto-stops)."
                )
                step += 1
                lines.append(
                    f"{step}. Repeat the workload under the trace: call "
                    f"launch_benchmark(name={benchmark!r}, args={args!r}, "
                    f"mangohud_output='/tmp/crucible_mangohud_profiled.csv', "
                    f"duration_secs={duration}) once more. Do NOT use this "
                    "run's frame numbers — the trace perturbs them."
                )
                step += 1
                lines.append(
                    f"{step}. Call fetch_perfetto_trace(trace_path={perfetto_output!r}, "
                    f"host_output_dir={perfetto_host_dir!r}) to pull the kernel "
                    "trace to the host."
                )
                step += 1
            collection = (
                f'{{"mangohud": {mangohud_output!r}, '
                f'"traces": ["<host_path from fetch_perfetto_trace>"]}}'
                if capture_perfetto
                else f'{{"mangohud": {mangohud_output!r}}}'
            )
            lines.append(
                "Then emit the final JSON object from the system prompt with:\n"
                "  fps_avg = fps_avg from fetch_mangohud_log\n"
                "  fps_p1 = fps_p1 from fetch_mangohud_log\n"
                "  frame_time_p99_ms = frametime_p99_ms from fetch_mangohud_log\n"
                "  psi_cpu_avg = psi_cpu_delta from the launch_benchmark result\n"
                "  psi_memory_avg = psi_memory_delta from the launch_benchmark "
                "result\n"
                f"Set collection_paths to {collection}.\n"
                "If launch_benchmark or fetch_mangohud_log returns an error, "
                "or launch_benchmark reports log_found=false, do NOT invent "
                "metrics and do NOT emit zeros: respond with "
                '{"error": "<what failed and the exact tool error>"} instead. '
                "A zero fps_avg from a real run is impossible and would be "
                "silently accepted as a measurement. A failed Perfetto capture "
                "is non-fatal: report the frame metrics and note the missing "
                "trace in collection_paths."
            )
            msg = "\n".join(lines)
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
