"""Adversarial patch reviewer: prompt independence + read-only tooling."""
from uuid import uuid4

from agents.common.protocol import AgentConfig, TaskEnvelope
from agents.common.tool_registry import ToolRegistry
from agents.patch_reviewer.agent import PatchReviewerAgent


def _task(extra=None):
    ctx = {
        "action": "review_patch",
        "round": 1,
        "patch_diff": "diff --git a/kernel/sched/fair.c b/kernel/sched/fair.c",
        "bottleneck": {"root_cause": "migration-bound render thread"},
        "kernel_src": "/tmp/k",
        "workload_mode": "steam",
    }
    ctx.update(extra or {})
    return TaskEnvelope(
        task_id=uuid4(),
        agent="patch_reviewer",
        context=ctx,
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )


def test_round1_prompt_is_independent_of_author_rationale():
    msg = PatchReviewerAgent().build_user_message(_task())
    assert "THE PATCH UNDER REVIEW" in msg
    assert "migration-bound" in msg
    # Round 1 carries no author advocacy and no prior rounds.
    assert "author" not in msg.lower()
    sys = PatchReviewerAgent().system_prompt()
    assert "adversarial" in sys
    assert "scrap" in sys and "approve" in sys and "revise" in sys


def test_later_rounds_label_author_claims_for_verification():
    msg = PatchReviewerAgent().build_user_message(_task({
        "round": 2,
        "prior_rounds": [{
            "round": 1,
            "reviewer_verdict": "revise",
            "reviewer_critiques": [{"issue": "locking"}],
            "author_response_claims": "fixed by holding rq lock",
        }],
    }))
    assert "assertions to VERIFY" in msg
    assert "fixed by holding rq lock" in msg


def test_reviewer_tools_are_read_only():
    agent = PatchReviewerAgent()
    agent._task = _task()
    registry = ToolRegistry()
    agent.setup_tools(registry)
    names = {t["name"] for t in registry.tools}
    assert "read_source_file" in names
    assert "search_kernel_source" in names
    assert "edit_file" not in names
    assert "finalize_patch" not in names
    assert "apply_sysctl" not in names
