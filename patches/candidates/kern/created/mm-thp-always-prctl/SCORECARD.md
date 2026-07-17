---
title: "mm: PR_SET_THP_ALWAYS prctl + khugepaged eager-collapse"
slug: mm-thp-always-prctl
class: kern
state: created
tier: TIER_2
created: 2026-07-12
target: linux-mm
suggested_cc: "David Hildenbrand, Usama Arif, Johannes Weiner, Barry Song, Andrew Morton"
base: "neptune 6.16 - PORT TO mm-unstable REQUIRED; commitmsg NOT YET WRITTEN"
review_rounds: 1
review_status: "fresh-fable steady-state APPROVE (after REVISE round: dead-kick sleep_expire, 32-bit break, comments - all fixed)"
---

## Summary
Per-mm MMF_THP_ALWAYS flag (prctl opt-in) treating the mm's anon VMAs
as MADV_HUGEPAGE, PLUS khugepaged enhancement (per-mm scan-budget boost
+ sleep-skip-while-collapsing + kick-on-opt-in) so the flagged heap
collapses promptly - flag alone was neutral because stock khugepaged is
too slow. Default-off, CONFIG_64BIT-gated, anon-only. Use case: launcher
opts in a third-party game binary that cannot madvise itself (Factorio:
update loop memory/TLB-bound, IPC 0.38, dTLB-miss 73%, only 18.5% of
154MB anon heap hugepage-backed under enabled=madvise).

## Key numbers (Deck, interleaved A/B)
- Factorio +2.29% UPS (n=28/side, Welch t=5.39, p<0.0001, ZERO regression)
  with stock global khugepaged - proves the patch drives collapse
- Aggressive-khugepaged ceiling +5.4% (n=18); gap explained by
  documented bimodal first-collapse latency

## Why TIER_2 not TIER_1
New UAPI (prctl) = high bar. Expect: "why not MADV_HUGEPAGE from the
app/loader" (answer: third-party binary, launcher-side knob;
PR_SET_THP_DISABLE precedent helps), "should khugepaged eagerness be
separable" (consider splitting into 2-patch series so the less
controversial khugepaged half can land alone). Multiple revisions
likely. Also: no commitmsg written yet.

## Review trajectory
1 review round REVISE (functional dead-kick sleep_expire bug, 32-bit
break, comment drift - all fixed) + fresh-fable steady-state APPROVE
(i386+x86_64 built clean). Trail in mm-thp-always-prctl.EVIDENCE.md;
design doc THP-DESIGN.md (this directory).

## Prep before sending
- Write commit message (numbers above + Factorio methodology)
- Port to mm-unstable; consider 2-patch split (khugepaged eagerness
  first, prctl second)
- lore search for competing per-process THP policy proposals (active
  area - David H's THP policy threads)

## Artifacts (this directory)
diff, EVIDENCE.md, thp_always_shim.c (LD_PRELOAD test shim for opting
in an unmodified binary). Design: THP-DESIGN.md; raw A/B: ab-data/ (factorio-*, thp-*).
