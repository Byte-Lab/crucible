"""Adversarial kernel-patch reviewer.

Sits between GenerateOptimization and ApplyChanges in the cycle: audits
the optimizer's diff before a measurement cycle is spent on it. The
review is deliberately adversarial and independently grounded — round 1
sees the diff, the trace-derived bottleneck facts, and the source tree,
but NOT the author's persuasive rationale, so the reviewer forms its own
model of what the change does. Author responses in later rounds are
claims to verify against the source, not accept.
"""
import json

from agents.common.claude_agent import ClaudeAgentBase
from agents.common.protocol import TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.optimizer.tools import make_kernel_read_tools


class PatchReviewerAgent(ClaudeAgentBase):
    def system_prompt(self) -> str:
        return """You are an adversarial Linux-kernel patch reviewer. A separate agent wrote a patch; your job is to poke holes in it. Assume the patch is wrong until the source code convinces you otherwise.

Audit, in order:
1. CORRECTNESS: locking/RCU context of every touched line, preemption/irq context, API contracts (verify signatures and semantics in the tree with your tools — do not trust the diff's claims), error paths, CONFIG variants that change the code's meaning, integer/overflow issues.
2. MECHANISM: FIRST establish the touched code actually EXECUTES in the measured guest (see the GPU/execution-stack reality in the user message — e.g. virt/kvm is dead code inside a guest that hosts no VMs, drivers for absent hardware never run). A patch to unreachable code is wrong-mechanism regardless of its internal correctness: verdict scrap. THEN judge whether the change addresses the cited bottleneck or is plausible-sounding but causally disconnected.
3. COLLATERAL DAMAGE: which other workload classes pay for this (throughput vs latency tradeoffs, other subsystems relying on current behavior)? Cite the specific code paths.
4. QUALITY: comment/code drift (comment says X, code does Y), magnitude choices unjustified by evidence, upstream style.

Use read_source_file / search_kernel_source to verify every claim you make. Cite file:line for each finding.

Scope boundary: you judge CORRECTNESS, MECHANISM PLAUSIBILITY, and COLLATERAL RISK. You do NOT demand empirical proof of benefit — this pipeline MEASURES every approved patch against the live benchmark immediately after your review; requiring benchmark evidence before measurement inverts the pipeline. If the mechanism is causally plausible for the cited bottleneck and the code is correct, measurement is the arbiter of benefit. Magnitude/threshold choices that are safe-but-unproven are measurement's job too — note them, do not block on them.

Verdicts:
- "approve": no correctness risks found and the mechanism plausibly addresses the bottleneck. Remaining uncertainty about BENEFIT is not a reason to withhold approval — say so in notes and let the measurement decide. Do not approve to be agreeable; do not block to be safe.
- "revise": concrete, fixable defects; list them as actionable critiques.
- "scrap": wrong mechanism (causally disconnected from the bottleneck), unfixable correctness problem, or collateral damage no revision can gate away.

Respond with JSON only:
{"verdict": "approve|revise|scrap", "summary": "<one paragraph>", "critiques": [{"severity": "critical|major|minor", "issue": "<specific, file:line>", "suggestion": "<concrete fix or verification>"}]}"""

    def build_user_message(self, task: TaskEnvelope) -> str:
        context = task.context
        patch_diff = context.get("patch_diff", "")
        bottleneck = context.get("bottleneck", {})
        round_num = int(context.get("round", 1))
        msg = (
            f"Review round {round_num}.\n"
            f"Kernel source tree: {context.get('kernel_src', '')}\n"
            f"Workload being optimized: {context.get('workload_mode', '')} "
            f"{context.get('workload_args', '')}\n"
            f"GPU stack reality: {context.get('gpu_stack', 'unknown')} — a "
            "patch to a driver that is not in use is automatically "
            "wrong-mechanism (scrap).\n\n"
            f"Trace-derived bottleneck evidence (facts, from the analyzer):\n"
            f"{json.dumps(bottleneck, indent=1)[:4000]}\n\n"
            f"THE PATCH UNDER REVIEW:\n```diff\n{patch_diff}\n```\n"
        )
        prior = context.get("prior_rounds") or []
        if prior:
            msg += (
                "\nPrior review rounds (your earlier critiques and the "
                "author's responses — treat the author's claims as "
                "assertions to VERIFY in the source, not accept):\n"
                f"{json.dumps(prior, indent=1)[:6000]}\n"
                "The diff above is the author's REVISED patch. Check "
                "whether each earlier critique is actually resolved, then "
                "audit the new diff as a whole (revisions introduce new "
                "bugs).\n"
            )
        msg += (
            "\nVerify against the tree with your tools, then emit the "
            "verdict JSON from the system prompt. Approve ONLY if you "
            "cannot find a hole or a concrete improvement."
        )
        return msg

    def setup_tools(self, registry: ToolRegistry) -> None:
        kernel_src = "/home/void/upstream/linux"
        task = getattr(self, "_task", None)
        if task is not None:
            kernel_src = task.context.get("kernel_src", kernel_src)
        make_kernel_read_tools(registry, kernel_src)


if __name__ == "__main__":
    PatchReviewerAgent().run()
