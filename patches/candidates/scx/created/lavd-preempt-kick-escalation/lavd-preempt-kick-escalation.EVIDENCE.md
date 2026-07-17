# lavd-preempt-kick-escalation — EVIDENCE

Patch: `lavd-preempt-kick-escalation.diff` (+ `.commitmsg`). scx_lavd BPF
(`scheds/rust/scx_lavd/src/bpf/preempt.bpf.c`). Runtime gate `--no-preempt-kick`.

## The finding (why this is a win)
Traced root-cause of delayed wakeups under oversubscription (host 7950X, 16 CPU
hogs + schbench; raw ftrace, 0 buffer overruns, 496k events, 12,383 wake→run
delays >3ms analyzed):

- scx_lavd's preemption is **IPI-free by design** — it sets a victim's slice to
  1 and waits for the victim to hit a scheduling point. A tight-loop CPU-bound
  victim has **no voluntary scheduling point** and only re-enters the scheduler
  at the next HZ tick, so the shortened slice frequently never takes effect.
- Measured consequence: **only <7% of delayed wakeups ever displaced a hog;
  92% got a CPU only when a peer voluntarily slept**; the wake-latency tail
  scales with slice length.

## The fix
When this CPU wins the per-victim CAS on est_stopping_clk, escalate with
`scx_bpf_kick_cpu(victim, SCX_KICK_PREEMPT)`. At most one IPI per victim per
running-epoch (CAS-gated); rate further bounded by the thr_lat_cri gate and each
CPU's context-switch rate. Runtime opt-out `--no-preempt-kick` for platforms
where IPI cost dominates (the original comment's concern).

## Measured (host, interleaved)
| scenario | stock | patched | p |
|---|---|---|---|
| interactive harness schbench wake p99, n=5 | 6526µs | 4262µs (**−35%**) | 0.001 |
| real 4c/8t machine (24 CPUs offlined) vkmark 1%-low fps, n=5 | 3951 | 4379 (**+10.8%**) | 0.009 |
| hackbench -g8 -l10000, n=10 | 25.54±0.37 | 25.84±0.29 (n.s.) | — |
| fork-storm wake p99, n=6 | 73-80µs | 73-89µs (no regression) | — |

## Scope / honesty
Makes granted preemptions effective; does NOT close all of upstream #3119 — with
large slices on the 32-CPU repro a residual ~5ms tail remains while ~3k
preemptions/s already fire, pointing at a separate (likely waker-side) mechanism.
Stated honestly in the commit message.

## Review record
2 rounds (round 1 REVISE = add the runtime gate, done) + a fresh-fable
steady-state review: **APPROVE, ship as-is.** All correctness dimensions
re-confirmed (stale-victim race benign — kick targets CPU; no double-IPI via CAS;
overrun-boost IPI matches sibling intent; IPI rate triple-bounded; gate wiring
matches the no_wake_sync pattern exactly).

## Note
sched_ext (not EEVDF); host-validated, does not run on the Deck. General
scx_lavd contribution.
