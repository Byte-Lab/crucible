> **2026-07-17:** patch corpus now lives at repo-root patches/ (candidates/ + negative-results/) — see patches/candidates/README.md and patches/SUMMARY.md. Paths below are historical.

# Steam Deck / scx_lavd patch series — evidence package

Produced by Crucible's adversarial patch pipeline, 2026-07-10/11. Every patch
went through an author → adversarial-reviewer loop (independent reviewer
context, explicit REFUTE instruction) until approval, with measurement gates
between rounds. Host: AMD 7950X (32 threads, 2 LLC domains), scx repo @
d624f332 (v1.1.2). "real-8CPU" = CPUs 4-15,20-31 offlined via chcpu, making
the host a genuine 4c/8t single-LLC machine (Deck-class topology).

Statistical method: Welch's t-test, n as stated; harness =
`deck/lavd-ab.sh` (vkmark capped-90fps jank + uncapped fps + schbench under
6-hog contention + perf bench sched messaging, on a 4c/8t cpuset).

## 1. lavd-queue-scaled-compete-window.diff — fixes sched-ext/scx#3303

**Bug**: hackbench `-g 8 -l 10000` +50% (v1.0.19→v1.0.20), unbisected upstream.
We bisected to a8a25fcb and isolated to the `LAVD_DL_COMPETE_WINDOW` constant
alone (300ms↔5ms revert: 26.0s → 17.4s).

**Mechanism**: whenever a task's deadline delta < window, its enqueued vtime
lands behind `cur_logical_clk`, which then cannot advance
(`advance_cur_logical_clk` early-returns) — deadline aging is disabled and
ordering degenerates to pure deadline-delta ranking. Under oversubscription of
similar tasks this starves whichever task the pipeline is blocked on.
A plain revert restores aging but loses the fork-storm protection the commit
existed for (storm wake p99 76µs → 607µs).

**Fix**: scale the window by the oversubscription ratio — full window while
`nr_queued <= nr_active` (bit-identical to current behavior at light load),
`window × nr_active / nr_queued` beyond. Same normalization the scheduler
already uses for slice sizing and clock aging.

**Rejected designs** (measured): per-task lat_cri-scaled windows (avg-linear
and steep-avg) fix hackbench + storm but regress the interactive harness
+65-91% because avg_lat_cri is dominated by the frequently-scheduling workers
themselves; thr_lat_cri-gated step/ramp lose storm protection (510-737µs)
because under a storm the threshold rides within 1.6% of max.

**Evidence**:
| scenario | stock (main) | patched | CFS ref |
|---|---|---|---|
| hackbench 2c/4t n=10 | 25.54 ±0.37 s | **17.43 ±0.04** | 16.9 |
| fork-storm wake p99 n=6 (proxy) | 73-80 µs | 73-85 µs | 3604 µs |
| fork-storm wake p99 n=6 (real-8CPU) | 68-86 µs | 70-87 µs | — |
| harness wake p99 n=5 (proxy) | 6526 µs | 4192 (-36%, p<0.001) | 4225 |
| harness messaging n=5 (proxy) | 0.571 s | 0.499 (-13%, p<0.001) | 0.731 |
| real-8CPU messaging n=5 | 0.592 s | 0.475 (-20%, p=0.002) | 0.661 |
| real-8CPU wake p99 n=5 | 44.6 µs | 59.4 (n.s.) | 3823 |
| powersave mode messaging n=5 | 0.554 s | 0.477 (-14%, p=0.001) | — |

Review: 5 rounds; final fresh-eyes verdict APPROVE (arithmetic/underflow/
staleness/preemption-interplay all verified; idiom-consistent with
can_boost_slice + calc_sys_time_slice normalization).

PR notes: light-load behavior bit-identical by construction; window and slice
shrinking are orthogonal (slice = run duration, window = aging regime);
division guarded (`nr_q > nr_a ≥ 1`); verifier-accepted on 6.16+.
Still-open coverage: heterogeneous/multi-LLC topology run, on-Deck game run.

## 2. lavd-preempt-kick-escalation.diff — makes won preemptions effective

**Finding** (12s raw-ftrace capture, 0 overruns, 496k events, 12,383 wake
delays >3ms analyzed): LAVD's IPI-free soft preemption (victim slice := 1, no
kick) relies on the victim reaching a scheduling point; a tight-loop CPU-bound
victim only reaches one at the next HZ tick. Result: only <7% of delayed
wakeups ever displaced a hog; 92% ran only when a peer voluntarily slept;
wake delay scales with slice length.

**Fix**: when this CPU wins the per-victim CAS (est_stopping_clk old→0),
escalate with `scx_bpf_kick_cpu(victim, SCX_KICK_PREEMPT)`. At most one IPI
per victim per running-epoch (CAS-gated); IPI rate further bounded by the
thr_lat_cri gate and by each CPU's context-switch rate. Runtime opt-out:
`--no-preempt-kick` (for architectures where IPI cost dominates — the
original comment's concern).

**Evidence**:
| scenario | stock | patched |
|---|---|---|
| harness wake p99 n=5 | 6526 µs | 4262 (-35%, p=0.001) |
| hackbench 2c/4t n=10 | 25.54 ±0.37 | 25.84 ±0.29 (n.s.) |
| fork-storm wake p99 n=6 | 73-80 µs | 73-89 µs |
| vkmark fps_avg / fps_p1 (proxy) | — | +4.7% / +31% (n.s., trending up) |
| **real-8CPU vkmark fps_p1 n=5** | 3951 | **4379 (+10.8%, p=0.009)** |
| real-8CPU wake p99 / messaging | 44.6 µs / 0.592 s | 40.2 (n.s.) / 0.577 (n.s.) |

Honest scope: the #3119 32-CPU repro tail (p99 ≈ 4.8ms with big slices) is
NOT fixed by this — LAVD stats show ~3k preemptions/s already firing there;
the residual tail has a different mechanism (possibly waker-side batching)
and remains open. This patch makes preemptions that are already granted
actually take effect.

Review: 2 rounds; correctness dimensions all PASS (stale-victim race benign —
kick targets CPU, successor just re-dispatches; no double-IPI via CAS;
overrun-boosted-task IPI matches the sibling path's stated intent).

## 3. Composed pair (1+2) — Deck deployment build

`~/upstream/scx-deckbuild` (both patches applied):
harness messaging -13.5% (p<0.001), wake p99 -30% (p=0.001), fps_avg +5.4%
(p=0.095), fps_p1 +31% — no interference between patches.
Real-8CPU: messaging -19.6% (p=0.002), wake p99 33.8µs (vs stock 44.6),
fork-storm 69-84µs, jank/fps flat — clean on Deck-class topology.

## 4. lavd ntsync lock-boost (worktree ~/upstream/scx-ntsync) — staged

Fills the maintainer's own TODO (lock.bpf.c:350): extends futex lock-holder
boost to ntsync (Wine/Proton NT-sync driver) via fexit on the two ioctl
dispatchers (per-op functions are inlined; dispatchers are fops-pinned).
2 review rounds: mechanism approved, decode/return-conventions verified
against 6.16 driver, attach gate airtight (module-BTF + kallsyms double
gate). Verifier passes live. Overhead ≈ existing futex hooks.
Micro-bench A/B neutral ×2 (mid-CS preemption is tick-driven; boost is
second-order there) — needs a real Proton title on the Deck to demonstrate
value. Ship as gap-fill with neutral-perf disclosure, or hold for Deck data.

## 5. drm-sched-wq-highpri-neptune.diff — staged, Deck measurement required

Port of the prior measured winner (fps_p1 +58% on 7900 XT VM under CFS) to
the Deck's neptune 6.16 tree. Applies clean, compile-verified. Review flagged
(correctly): the desktop evidence was measured under CFS where nice -20 is
the only boost; under scx_lavd the submit workers already receive hardirq
(+1024) boosts and weight enters lat_cri logarithmically — the effect on
Deck+LAVD is unproven. Comment updated to state the weight mechanism
accurately. Upstream shape should be a per-driver opt-in via
drm_sched_init_args. Measure on Deck slot B before trusting.

## Deck deployment plan (when hardware returns)

1. Recover slot A → re-clone slot B (deck/deck-slot-b.sh, /etc-overlay fix in).
2. Deploy scx-deckbuild scx_lavd binary (pair) + A/B vs SteamOS stock LAVD on
   a real title (gamescope) — frame-time CSV via MangoHud.
3. ntsync build A/B on a Proton title (fsync→ntsync path).
4. drm-sched kernel to slot B, A/B same title.
5. TDP/autopilot candidate (AP_HIGH_UTIL 25% SMT) only measurable there.
