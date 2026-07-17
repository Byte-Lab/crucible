---
title: "sched/fair: prefer a fully idle core over SMT-idle target in select_idle_sibling (SIS_PREFER_CORE)"
slug: sched-fair-smt-full-core-pref
class: kern
state: created
tier: TIER_1
created: 2026-07-12
target: "LKML sched (peterz/vincent) - NOT READY, hold"
suggested_cc: "Peter Zijlstra, Vincent Guittot, K Prateek Nayak (AMD)"
base: "neptune 6.16; needs mainline port + full benchmark campaign"
review_rounds: 2
review_status: "fresh-fable APPROVE for RFC posting (round 3); code+claims+citations recomputed clean; pending: non-RFC bare-metal multi-socket data"
---

## Summary
Civ6 AI-bench trace: coordinator thread runs 25% SMT-degraded while a
full idle core sits free (select_idle_sibling takes the idle-target
shortcut before checking for idle cores). Patch: prefer a full idle
core over an SMT-idle target, behind a default-off SCHED_FEAT,
asym-suppressed, target-only, stale-flag-safe.

## Key numbers (Deck)
- Civ6 aibench fps_avg +4.9% (p=0.0004), no frame regression
- KNOWN COST: schbench low-load wake p99 28 -> 38us (why default-off)

## Why TIER_3
Sched maintainers bounce default-off SCHED_FEATs without exhaustive
multi-topology data. Own EVIDENCE notes the requirement: full tbench/
netperf/hackbench/schbench table on multi-LLC + big-core machines,
plus an answer for why existing SIS_* heuristics don't cover it, plus
the wake-latency cost story. That campaign has not been run. Upgrade
path to TIER_2: run it on a big EPYC/Xeon; if the win generalizes and
cost is bounded, resubmit-able.

## Review trajectory
2 rounds + fresh-fable APPROVE scoped to "carry as default-off on the
Deck kernel" - the reviewer explicitly flagged the upstream data bar as
unmet. Trail in sched-fair-smt-full-core-pref.EVIDENCE.md; bench plan
SMT-BENCHPLAN.md (this directory).

## Artifacts (this directory)
diff, EVIDENCE.md. Bench plan: SMT-BENCHPLAN.md (this directory; AI-TRACE-FINDINGS.md + traces/baseline-ai.pftrace = the originating trace).
