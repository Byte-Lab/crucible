---
title: "scx_lavd: escalate won preemption to SCX_KICK_PREEMPT (soft-yield cannot evict tight-loop hogs)"
slug: lavd-preempt-kick-escalation
class: scx
state: created
tier: TIER_1
created: 2026-07-11
target: "sched-ext/scx GitHub PR"
suggested_cc: "Changwoo Min, sched-ext maintainers"
base: "sched-ext/scx main; worktree ~/upstream/scx-bisect/var-kickipi"
review_rounds: 2
review_status: "fresh-fable APPROVE steady state"
---

## Summary
Trace-proven (12k delayed wakeups analyzed): lavd's IPI-free soft-yield
(slice=1) cannot evict tight-loop CPU-bound victims - no scheduling
point until tick; <7% of delayed wakeups actually displaced a hog. Fix:
when a preemption is won (CAS), escalate to SCX_KICK_PREEMPT (real IPI),
gated by --no-preempt-kick for opt-out.

## Key numbers (real-8CPU topology via chcpu offline, interleaved)
- fps_p1 +10.8% (p=0.009)
- Harness wake p99 -35% (p=0.001), zero collateral regressions
- Honest scope in commitmsg: #3119 32-CPU residual tail is a different
  mechanism (waker-side), explicitly left open

## Why TIER_1
Trace-derived mechanism + measured win + opt-out flag + honest scoping.
Composes cleanly with lavd-queue-scaled-compete-window (composed-pair
tested: no interference; messaging -13.5% p<0.001, wake p99 -30%).

## Review trajectory
Kernel-trace analysis of delayed wakeups -> mechanism (soft-yield
displacement rate <7%) -> 2 adversarial rounds -> fresh-fable APPROVE.
Trail in EVIDENCE.md. Falsified sibling ideas (small-machine profile:
>100x proxy inflation lesson) documented in deck-patch-pipeline memory
and SUMMARY.md.

## Prep before sending
- Rebase onto current scx main; one A/B rerun
- Send AFTER or WITH lavd-queue-scaled-compete-window (same files,
  compose-tested as a pair)

## Artifacts (this directory)
diff, commitmsg, EVIDENCE.md. Composed-pair worktree: ~/upstream/scx-deckbuild.
