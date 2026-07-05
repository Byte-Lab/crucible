import json
from typing import Any

from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.optimizer.tools import make_optimizer_tools


class OptimizerAgent(ClaudeAgentBase):
    # Optimizer chains read_source_file/search_kernel_source/edit_file calls
    # to navigate the kernel tree and apply patches. On a moderately complex
    # bottleneck this routinely needs more than the 40-turn base cap; the
    # `timeout_seconds` subprocess kill (default 600s) is the real safety net.
    MAX_TOOL_ROUNDS = 80

    def system_prompt(self) -> str:
        return """You are the Optimizer agent for Crucible. Write a real Linux kernel source patch that improves the measured bottleneck.
Layers: kernel (scheduler, memory, IO), userspace (Mesa, Wine, gamescope), tuning (sysctl).

Aim for an UPSTREAM-QUALITY patch: minimal and targeted, touching the hot path
the profile implicates; correct and safe (no UB, no locking/lifetime changes you
can't justify); behaviour-improving, not merely a config/knob toggle. Prefer
small heuristic/fast-path improvements (e.g. avoid redundant work, tighten a
loop bound, cache a recomputed value, skip an unnecessary barrier) grounded in
what the Perfetto trace and bottleneck show. Explain the reasoning and the
expected effect as you would in a commit message.

Build correctness is mandatory — a patch that does not compile is worthless.
Before you call any kernel function, macro, or struct field that your change
introduces, VERIFY it exists and is reachable from the file you are editing:
`search_kernel_source` for its definition and confirm the necessary header is
already included in that file. Do not assume an API from general knowledge
(e.g. a *_trylock/_nowait variant) exists in THIS tree — check first, and if it
is missing, use an API the file already uses. Keep the change small enough to
reason about its compilation.

Workflow (do not hand-write unified diffs — let git produce them):
  1. Navigate with `read_source_file`, `search_kernel_source`, `list_source_files`.
  2. Apply each change with `edit_file(path, old_text, new_text)`. old_text must
     match exactly (whitespace, indentation) and appear exactly once; extend it
     with surrounding context if not unique. Stack as many edit_file calls as you
     need across one or more files.
  3. When done, call `finalize_patch("<short-name>.diff")` exactly once. It captures
     a `git diff` of all your edits into .crucible_patches/<filename> and reverts
     the working tree. The returned `path` is what you put in `patch_path` below.
  4. If you cannot produce a safe change, leave `patch_path` as the empty string.

Respond with JSON only (no prose, no fences):
{"layer": "kernel|userspace|tuning",
 "patch_path": "<absolute path returned by finalize_patch, or empty string>",
 "patches": [{"filename": "<name>.diff", "description": "<what>", "risk": "low|medium|high"}],
 "sysctl_changes": [],
 "rationale": "<approach>"}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        bottleneck = context.get("bottleneck", {})
        game = context.get("game_name", "unknown")
        attempt = context.get("attempt_number", 1)
        kernel_src = context.get("kernel_src", "/home/void/upstream/linux")
        msg = f"Generate optimizations for {game} (attempt {attempt}).\n"
        msg += f"Bottleneck:\n{json.dumps(bottleneck, indent=2)}\n"
        review = context.get("review_feedback")
        if review:
            msg += (
                "\nADVERSARIAL REVIEW of your previous patch (below). The "
                "reviewer's verdict was "
                f"'{review.get('verdict', 'revise')}'. Critiques:\n"
                f"{json.dumps(review.get('critiques', []), indent=1)}\n"
                f"Reviewer summary: {review.get('summary', '')}\n"
                "Your previous diff:\n```diff\n"
                f"{context.get('prior_patch_diff', '')}\n```\n"
                "Address every critique with a REVISED patch (re-read the "
                "source, re-edit from the clean tree, finalize_patch with "
                "the SAME filename), and put a point-by-point response to "
                "the critiques in your rationale. Concede ONLY when a "
                "critique is both fatal AND unfixable (wrong mechanism, "
                "correctness hole no gating can close) — conceding on "
                "fixable or judgment-call critiques wastes the whole "
                "cycle's measurement slot. You have several rounds: use "
                "them. If you believe a critique is mistaken, push back "
                "with source citations — reviewers approve well-defended "
                "positions. If a critique asks for empirical proof of "
                "benefit, respond that measurement follows review by "
                "design; that is not a code defect. To concede, return "
                "patch_path as the empty string with the reason.\n"
            )
        if attempt > 1:
            msg += f"Previous attempts: {json.dumps(context.get('previous_attempts', []))}\nTry different approach.\n"
        gpu = context.get("gpu_stack")
        if gpu:
            msg += f"GPU stack reality (do not patch drivers that are not in use): {gpu}\n"
        msg += f"Kernel source: {kernel_src}\n"
        workload_args = context.get("workload_args")
        if workload_args:
            msg += f"Measured workload: {context.get('workload_mode','')} {workload_args}\n"
        explored = context.get("explored_areas") or []
        if explored:
            msg += (
                f"Earlier cycles already patched these areas, annotated with "
                f"their MEASURED outcome: {json.dumps(explored)}. Never "
                "re-emit the same change or re-edit the same function. "
                "Neutral/regressed entries are dead ends — avoid their "
                "files. Accepted entries measurably improved the benchmark: "
                "a DIFFERENT function/mechanism in that same subsystem "
                "family is a promising target.\n"
            )
        if context.get("tuning_only"):
            msg += (
                "\nTUNING-ONLY MODE. Do NOT edit kernel source and do NOT call "
                "finalize_patch — leave patch_path as the empty string and set "
                'layer to "tuning". Propose one or more runtime sysctl changes '
                "in sysctl_changes as "
                '[{"key": "<dotted.sysctl.name>", "value": "<new value>", '
                '"note": "<why, and the expected effect on this bottleneck>"}]. '
                "Every key MUST be a knob that already exists under /proc/sys on "
                "a stock Linux 7.1 guest (e.g. vm.dirty_ratio, vm.dirty_background_ratio, "
                "vm.swappiness, vm.compaction_proactiveness, vm.watermark_scale_factor, "
                "vm.page-cluster, kernel.sched_cfs_bandwidth_slice_us, "
                "kernel.sched_autogroup_enabled, kernel.numa_balancing, "
                "kernel.timer_migration, kernel.randomize_va_space, "
                "kernel.sched_rt_runtime_us). Do NOT invent debugfs-only knobs "
                "(e.g. sched/base_slice_ns is NOT a sysctl on this kernel). Pick "
                "changes whose direction plausibly helps the specific bottleneck "
                "above, and that differ from previous attempts. Prefer knobs with "
                "LARGE, reliable effects on a CPU-contended headless render "
                "workload — the strongest are: kernel.numa_balancing=0 (kills "
                "periodic NUMA scan/migration overhead), kernel.timer_migration=0 "
                "(keeps timers CPU-local), kernel.sched_autogroup_enabled=0 "
                "(removes autogroup fairness overhead for a foreground workload), "
                "vm.compaction_proactiveness=0 and vm.numa_stat=0 (cut background "
                "kernel work), kernel.sched_cfs_bandwidth_slice_us raised to "
                "10000-20000 (fewer bandwidth reschedules). Change ONE or TWO "
                "high-impact knobs per attempt so the effect is attributable, and "
                "pick ones the trace's bottleneck actually implicates.\n"
                "CRITICAL: the measured workload IS the benchmark to optimize — "
                "NOT interference to remove. If it stresses memory (stress-ng "
                "--vm/--vm-bytes or any allocation-heavy load) and no previous "
                "attempt already set it, you MUST make exactly this change and "
                "nothing else: sysctl_changes = [{\"key\": "
                '"/sys/kernel/mm/transparent_hugepage/enabled", "value": '
                '"always", "note": "promote anonymous pages to huge pages for '
                'the allocation-heavy workload"}]. Transparent Huge Pages is the '
                "single largest reliable win for such workloads; do not substitute "
                "numa/compaction/other knobs on the first attempt.\n"
                "For a MEMORY-bound bottleneck the highest-impact tuning is "
                "Transparent Huge Pages: set key "
                '"/sys/kernel/mm/transparent_hugepage/enabled" to "always" '
                "(large, reliable throughput gain for anonymous-memory workloads "
                "vs the madvise/never default). Related high-impact /sys knobs: "
                '"/sys/kernel/mm/transparent_hugepage/defrag" = "always", '
                '"/sys/kernel/mm/transparent_hugepage/khugepaged/defrag" = "1". '
                "For a CPU-bound bottleneck, "
                '"/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor" = '
                '"performance" (if present). These /sys tunables are accepted by '
                "apply_sysctls exactly like /proc/sys keys — use the full path as "
                "the key. Prefer a /sys high-impact knob when the bottleneck fits."
            )
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        kernel_src = "/home/void/upstream/linux"
        task = getattr(self, "_task", None)
        if task is not None:
            kernel_src = task.context.get("kernel_src", kernel_src)
        make_optimizer_tools(registry, kernel_src=kernel_src)

    def extract_result(self, final_text: str, task: TaskEnvelope) -> dict[str, Any]:
        """Lift the inner JSON to the top of the envelope so the orchestrator
        can read `patch_path`/`layer` directly. Falls through to
        `{"response": final_text}` when parsing fails so the orchestrator's
        `parse_agent_response` fallback still has a chance."""
        parsed = _try_parse_json_block(final_text)
        if parsed is None:
            return {"response": final_text}
        result = dict(parsed)
        result["response"] = final_text
        return result


def _try_parse_json_block(text: str) -> dict[str, Any] | None:
    """Mirror `crucible-orchestrator::parse_agent_response`: strip optional
    ```json fences, then try to parse. Returns None on failure."""
    trimmed = text.strip()
    if trimmed.startswith("```json"):
        trimmed = trimmed[len("```json"):]
    elif trimmed.startswith("```"):
        trimmed = trimmed[len("```"):]
    if trimmed.endswith("```"):
        trimmed = trimmed[: -len("```")]
    trimmed = trimmed.strip()
    try:
        value = json.loads(trimmed)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


if __name__ == "__main__":
    OptimizerAgent().run()
