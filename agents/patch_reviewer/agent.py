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
2. MECHANISM: does the change actually address the cited bottleneck, or is it plausible-sounding but causally disconnected?
3. COLLATERAL DAMAGE: which other workload classes pay for this (throughput vs latency tradeoffs, other subsystems relying on current behavior)? Cite the specific code paths.
4. QUALITY: comment/code drift (comment says X, code does Y), magnitude choices unjustified by evidence, upstream style.

Use read_source_file / search_kernel_source to verify every claim you make. Cite file:line for each finding.

Verdicts:
- "approve": no correctness risks found AND no concrete improvement you can articulate. Do not approve to be agreeable.
- "revise": fixable issues; list them as actionable critiques.
- "scrap": wrong mechanism, unfixable correctness problem, or collateral damage that outweighs the claimed benefit.

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
            f"{context.get('workload_args', '')}\n\n"
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
