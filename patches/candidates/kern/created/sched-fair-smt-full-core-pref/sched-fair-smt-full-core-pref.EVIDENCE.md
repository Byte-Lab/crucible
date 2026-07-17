# SIS_PREFER_CORE — evidence & review record

Patch: `sched-fair-smt-full-core-pref.diff` (kernel/sched/fair.c + features.h)
Feature: `SCHED_FEAT(SIS_PREFER_CORE, false)` — **default OFF**, runtime toggle
`echo [NO_]SIS_PREFER_CORE > /sys/kernel/debug/sched/features`.

## What / why
On SMT with a fully-idle core available, `select_idle_sibling()` takes the
idle-`target` shortcut before checking for idle cores, so a wakee can land on
a merely-SMT-idle CPU (its sibling busy) while a full core sits idle. Traced on
Civ6 aibenchmark: coordinator thread 25% SMT-degraded, 15/140 worker-burst
wakes took the SMT-degraded shortcut with a full core free. The gate makes the
`target` shortcut fall through to `select_idle_cpu(has_idle_core=true)` when a
full idle core is advertised. Reuses in-tree `is_core_idle` (numa_idle_core
policy). Extends PeterZ's 2011 SMT-avoidance intent (4dcfe1025b51).

## Measured (Steam Deck Van Gogh 4c/8t, interleaved same-kernel A/B, runtime toggle)
BENEFIT — Civ6 aibenchmark, n=6/arm, Welch:
- fps_avg 40.26 → 42.24, **+4.9% (p=0.0004)**, tight (off 40.0–40.6, on 42.1–42.3)
- frametime p99/p999, 1%-low: all neutral (no frame regression)

COST — schbench wakeup p99 (runtime toggle):
- low load (-m1 -t2): 28µs → 38µs (**+36%, 3/3**) — the predicted forced-migration
  cache-warmth loss on a near-idle machine.
- mid load (-m2 -t6): 3764µs both — NEUTRAL. The guard self-disables under load
  (`test_idle_cores()` false once cores fill), exactly as designed.

## Review record
Two adversarial fable rounds. Round 1 (ungated) REVISE: asym+SMT broken, stale-flag
fallback migrated off warm target, low-load cost, un-gated. Round 2 fixes: sched_feat
gate (default off), sched_asym_cpucap_active() suppression, guard_forced stale-flag
fallback (prefer warm target when scan finds no full core), target-only (prev/recent_used
left stock). Fresh round-2 review: **APPROVE** for Deck default-off carry — all 4 fixes
correct, no correctness bug ON or OFF, OFF path = jump-label NOP + one dead test.

## Known nits (non-blocking, recorded per reviewer)
- Nanosecond race: if `has_idle_cores` flips false between the guard read and the
  `select_idle_smt(prev)` re-read, a wakee can migrate off warm target to an idle
  sibling of prev's core for no capacity gain. Destination shares prev's cache;
  self-heals next wakeup. Not covered by guard_forced. Benign.

## Upstream distance (NOT sufficient for lkml as-is; default-off makes it carryable, not mergeable)
Need: full tbench / netperf TCP_RR / hackbench / schbench table across load points;
≥1 multi-LLC part (EPYC / 2+ CCD Ryzen — test_idle_cores is per-LLC, cost structure
differs); ≥1 hybrid part to exercise the asym-suppression path; cover letter must LEAD
with the +36% low-load schbench number (upstream has rejected always-prefer-idle-core
variants on exactly this cost) and answer "why a feat, not a fix to nr_idle_scan/SIS_UTIL".
