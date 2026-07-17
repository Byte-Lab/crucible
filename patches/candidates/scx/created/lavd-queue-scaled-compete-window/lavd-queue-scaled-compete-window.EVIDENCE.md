# lavd-queue-scaled-compete-window — EVIDENCE

Patch: `lavd-queue-scaled-compete-window.diff` (+ `.commitmsg`). scx_lavd BPF
(`scheds/rust/scx_lavd/src/bpf/lat_cri.bpf.c`). **Fixes upstream
github.com/sched-ext/scx#3303.**

## The bug (why this is a win)
Issue #3303: hackbench `-g 8 -l 10000` regressed ~50% (20s→32s) between scx_lavd
v1.0.19 and v1.0.20, unbisected upstream.

- **Bisected** to commit a8a25fcb ("scx_lavd: Increase greedy factor in deadline
  calculation"), and **isolated to the LAVD_DL_COMPETE_WINDOW constant alone**
  (300ms vs the prior 5ms): reverting just that constant recovers 26.0→17.4s.
  Method: built 8 bisect worktrees + per-constant variant builds, measured each
  under the issue's hackbench on a 2c/4t-pinned cpuset.
- **Mechanism** (verified in code): the constant head-start makes an enqueued
  task's vtime land behind `cur_logical_clk`, which then cannot advance
  (`advance_cur_logical_clk` early-returns on `vlc <= clc`) → deadline aging is
  disabled → under oversubscription of similar tasks the pipeline's blocked task
  starves and the whole workload serializes.
- A **plain revert is not acceptable**: it destroys the commit's fork-bomb
  protection — fork-storm schbench wake p99 goes 76µs → 607µs.

## The fix
Scale the compete window by the oversubscription ratio: full window while
`nr_queued_task <= nr_active` (bit-identical to current behavior at light load),
`window × nr_active / nr_queued` beyond. Uses the same normalization
`calc_sys_time_slice`/`advance_cur_logical_clk` already apply, and the same
`nr_q <= nr_a` boundary as `can_boost_slice`.

## Rejected alternatives (measured, in the commit msg)
Per-task lat_cri-scaled windows (avg-linear and steeper) fixed hackbench + storm
but REGRESSED the interactive harness +65-91% (avg_lat_cri is dominated by the
frequently-scheduling workers). thr_lat_cri-gated step/ramp lost storm
protection (threshold rides within 1.6% of max under a storm). Queue-depth
scaling keeps the window uniform across tasks → changes only the aging regime.

## Measured (host 7950X, interleaved, thermally controlled)
| scenario | stock | patched | ref |
|---|---|---|---|
| hackbench -g8 -l10000, 2c/4t, n=10 | 25.54 ±0.37s | **17.43 ±0.04s** | EEVDF 16.9 |
| fork-storm schbench wake p99, n=6 | 73-85µs | 73-85µs (preserved) | revert 607, EEVDF 3604 |
| interactive harness wake p99, n=5 | 6526µs | 4192µs (**−36%**, p<0.001) | — |
| real-8CPU (chcpu) messaging, n=5 | 0.592s | 0.475s (**−20%**, p=0.002) | — |
| powersave mode messaging, n=5 | 0.554s | 0.477s (−14%, p=0.001) | — |
Light-load bit-identical by construction.

## Review record
5 review rounds during development + a fresh-fable steady-state review: **APPROVE,
build green, all attack vectors run to ground, zero required changes.** Arithmetic
(div-by-zero/overflow/underflow), EWMA staleness, aging-mechanism consistency, and
commit-message accuracy all verified.

## Note
scx_lavd is sched_ext (not EEVDF); this is a host-validated upstream bug fix. It
does not run on the Deck (SteamOS ships EEVDF, scx disabled), so it is a general
scx_lavd/upstream contribution rather than a Deck-specific win.
