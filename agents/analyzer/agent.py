import json
from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.analyzer.tools import make_analyzer_tools


class AnalyzerAgent(ClaudeAgentBase):
    def system_prompt(self) -> str:
        return """You are the Analyzer agent for Crucible. Analyze profiling data to identify performance bottlenecks.
Respond with JSON: {"bottleneck": "<subsystem>", "severity": "high|medium|low", "root_cause": "<description>", "evidence": "<data>", "optimization_targets": [{"subsystem": "<str>", "component": "<str>", "suggestion": "<str>", "confidence": <float>}]}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        game = context.get("game_name", "unknown")
        metrics = context.get("metrics", {})
        trace_paths = context.get("trace_paths", [])
        attempt = context.get("attempt_number", 1)
        previous_attempts = context.get("previous_attempts", [])
        msg = f"Analyze profiling data for {game} (attempt {attempt}).\n"
        if metrics:
            msg += f"Metrics:\n{json.dumps(metrics, indent=2)}\n"
        if trace_paths:
            msg += f"Traces: {trace_paths}\n"
        if previous_attempts:
            msg += (
                f"Previous optimization attempts failed at the margin: "
                f"{json.dumps(previous_attempts)}.\n"
                f"Consider alternate bottlenecks; do not re-recommend the same subsystem.\n"
            )
        msg += "Use tools to identify the primary bottleneck."
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        make_analyzer_tools(registry)

if __name__ == "__main__":
    AnalyzerAgent().run()
