---
title: "scx_lavd: boost lock-holder criticality for ntsync-backed waits"
slug: lavd-ntsync-lock-boost
class: scx
state: created
tier: TIER_3
created: 2026-07-10
target: "sched-ext/scx GitHub PR - HOLD until measurable"
suggested_cc: "Changwoo Min"
base: "sched-ext/scx worktree ~/upstream/scx-ntsync"
review_rounds: 1
review_status: "mechanism approved, BPF verifier passes - but unmeasurable"
---

## Summary
Boost lock-holder criticality when waiters block via ntsync (Wine/Proton
NT synchronization primitives) so the holder gets scheduled promptly.
Mechanism reviewed and approved; verifier-clean.

## Why TIER_3 (blocked, not wrong)
Unmeasurable in our environment: SteamOS Proton uses fsync/futex, NOT
ntsync - 0 ntsync fds observed even with PROTON_USE_NTSYNC=1 forced
(build lacks support or session env never reaches Proton games).
Micro-bench neutral x2. Cannot ship a perf claim we cannot measure.
Upgrade path: a Proton build with working ntsync + a title that
exercises it; then re-run the lock-contention A/B.

## Review trajectory
1 round mechanism review (approved) + verifier validation. Measurement
attempts documented in deck-patch-pipeline memory + SUMMARY.md
(RIGOROUSLY KILLED section notes the measurement story; the CODE was
never falsified).

## Artifacts (this directory)
diff only (no commitmsg/EVIDENCE - blocked before evidence stage).
Worktree: ~/upstream/scx-ntsync.
