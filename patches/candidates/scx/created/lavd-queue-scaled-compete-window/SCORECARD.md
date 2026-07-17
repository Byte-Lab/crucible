---
title: "scx_lavd: scale DL compete window by queue depth to restore deadline aging under oversubscription (fixes #3303)"
slug: lavd-queue-scaled-compete-window
class: scx
state: created
tier: TIER_1
created: 2026-07-11
target: "sched-ext/scx GitHub PR (fixes reported issue #3303)"
suggested_cc: "Changwoo Min (scx_lavd author), sched-ext maintainers"
base: "sched-ext/scx main; worktrees at ~/upstream/scx-bisect/var-*"
review_rounds: 5
review_status: "fresh-fable APPROVE steady state"
---

## Summary
Fixes upstream sched-ext/scx#3303 (hackbench +50% regression). Bisected
to a8a25fcb, isolated to the constant LAVD_DL_COMPETE_WINDOW head-start:
under oversubscription the constant window freezes cur_logical_clk,
disabling deadline aging -> starvation. Fix: scale the window by
nr_active/nr_queued when oversubscribed; storm protection preserved
(~75us window under fork storms).

## Key numbers (host, interleaved)
- hackbench 25.5s -> 17.4s (n=10; EEVDF reference 16.9s) - closes most
  of the reported regression
- Interactive wake p99 -36%, messaging -13/-20% (all p<=0.002)
- powersave governor run clean
- 3 rejected alternative curve designs documented in EVIDENCE (lat_cri-
  relative curves all fail: avg dominated by frequent schedulers, thr
  rides max under storms) - preempts "why not X" review questions

## Why TIER_1
Closes an OPEN reported issue with a bisect, a mechanism, and numbers.
Fastest reputation win in the corpus; scx review bar is pragmatic.

## Review trajectory
Bisect (deck/bisect-3303.sh) -> mechanism isolation -> 5 adversarial
review rounds (3 alternative designs proposed, tested, rejected with
data) -> fresh-fable APPROVE. Trail in EVIDENCE.md. Repro + A/B
harness: deck/lavd-ab.sh, deck/lavd-ab-stats.py, results
~/.crucible/{lavd-ab,bisect-3303}/.

## Prep before sending
- Rebase onto current scx main (lavd moves fast); rerun hackbench A/B
  once on rebased version
- PR text: adapt commitmsg; link bisect + numbers; reference #3303

## Artifacts (this directory)
diff, commitmsg, EVIDENCE.md.
