---
title: "cpufreq/amd-pstate: per-core dynamic EPP boost for recently-busy CPUs (dynamic_epp)"
slug: amd-pstate-epp-boost
class: kern
state: created
tier: TIER_1
created: 2026-07-12
target: linux-pm
suggested_cc: "Mario Limonciello, Gautham R. Shenoy, Perry Yuan (AMD), Rafael J. Wysocki"
base: "neptune 6.16 (Steam Deck) - PORT TO CURRENT linux-pm/mainline REQUIRED before sending"
review_rounds: 2
review_status: "fresh-fable APPROVE (production-clean version final-reviewed)"
---

## Summary
Found by perfetto-tracing a real game (Civ6) on Steam Deck Van Gogh:
the graphics benchmark is single-thread CPU-bound but the busy core
downclocks to 2.43GHz (vs 3.5 fmax) because frequent short futex sleeps
collapse amd-pstate's util signal under EPP balance_performance.
Causally confirmed via runtime EPP A/B (p999 -40%). Patch: edge-triggered
per-core EPP=performance boost for recently-busy CPUs (C0-residency
trigger, hwp_boost-style precedent), default-off module param.

## Key numbers (Deck, interleaved thermally-controlled A/B, n=6)
- Civ6 graphics 1%-low fps +31.8% (p=0.014)
- frametime p99 -4.1% (p=0.015), p999 no regression
- Iteration story (maintainer gold): first tried min_perf floor ->
  REGRESSED p999; refuted write-rate hypothesis (50x fewer writes still
  regressed); root-caused the lever (floor pins >=nominal during
  micro-idle, perturbs SMU boost on shared-TDP APU); switched to EPP
  hint -> clean. Rejected iterations preserved in iterations/.

## Review trajectory
2 rounds + fresh-fable APPROVE; production-clean version (scaffolding
stripped) separately final-reviewed. Trail summarized in
amd-pstate-epp-boost.EVIDENCE.md. Root-cause perfetto trace:
traces/baseline-gfx.pftrace (this directory); design notes
PSTATE-DESIGN.md and P999-ROOTCAUSE.md (this directory).

## Prep before sending
- Port from neptune 6.16 to current linux-pm bleeding-edge (amd-pstate
  churns fast; dynamic EPP discussions ongoing upstream - check for
  collisions/prior art on linux-pm lore first)
- One fresh Deck A/B on the ported version to confirm numbers hold
- Decide default-off param vs sysfs knob framing with Mario's recent
  amd-pstate direction in mind

## Artifacts (this directory)
amd-pstate-epp-boost-clean.diff (SEND THIS), -clean.commitmsg,
EVIDENCE.md, iterations/ (epp-boost pre-clean + 3 rejected busy-floor
variants - do not send, evidence of process).
Perfetto traces: traces/ (baseline-gfx = root-cause, baseline-xp2b = duplicate-mechanism confirmation);
raw A/B runs: ab-data/ (eppb-*, eppm-ai-*, lw-* = rejected lowwrite iteration).
