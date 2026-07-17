# EEVDF Runqueue-Latency Deep-Dive — Civ6 on Steam Deck (Van Gogh 4c/8t)

**Question:** are the busy-CPU >250µs wakeup latencies in the Civ6 AI-benchmark trace a
patchable EEVDF placement/latency bottleneck, or unavoidable oversubscription?

**Verdict: NO distinct kernel-addressable bottleneck. Do not write a patch.**
The finding decomposes into (1) RT-audio preemption (~63% of all wait time — priority-class,
not EEVDF), (2) EEVDF slice protection working as designed (~12%, tunable not patchable),
(3) genuine all-CPUs-busy oversubscription during shader-compile bursts, and (4) our own
tracer (`traced_probes`) as interferer. Total addressable time is ≈0.3% of critical-thread
CPU time — an order of magnitude below the project's ±5% A/A noise band on this benchmark.
It is a **different and much smaller** phenomenon than what SIS_PREFER_CORE fixes.

Method: perfetto `thread_state` runnable intervals for the 6 critical threads, split into
**wakeup waits** (`R` interval with a matching `sched_wakeup`, joined for `target_cpu`) and
**requeue waits** (switched out still-runnable, no wakeup — preemption). Per-wait: enqueue
CPU, the task occupying it, per-CPU occupancy snapshot (all 8 CPUs), idle-CPU overlap during
the wait, and the preemptor's priority. Traces: `baseline-ai.pftrace` (44.9s),
`baseline-gfx.pftrace` (44.9s). Deck tunables read live over SSH.

## Workload structure (AI trace)

| thread | tid | CPU time / 44.9s |
|---|---|---|
| main/coordinator "Civ6 (WinID 2)" | 5377 | 35.75s (~80% of one CPU) |
| 5 AI workers | 5417–5421 | ~8.7s each (43.7s total) |

6 heavy threads, 79.4s CPU on 8 logical CPUs (SMT pairs (0,1)(2,3)(4,5)(6,7)) → average
system load well under capacity; contention is **bursty**, not sustained.

## 1. Runqueue-wait distribution (AI trace, 6 critical threads)

| kind | n | total | p50 | p95 | p99 | max | enqueued on busy CPU |
|---|---|---|---|---|---|---|---|
| wakeup→run | 2,946 | 59.8ms | 17.5µs | 38µs | 50µs | 2,368µs | **18 (0.6%)** — 11 while another CPU idle |
| preempt-requeue | 3,366 | 180.5ms | 36µs | 164µs | 211µs | 1,015µs | all (by definition); 2,504 while ≥1 CPU idle |

- **Total runnable-wait: 240ms = 0.30% of critical-thread CPU time** (79.4s). Per thread:
  main 50.8ms (0.14% of its runtime, max wakeup wait just **105µs**); worst worker (5417)
  74.4ms (0.17% of wall).
- The headline reframe: **wakeup placement is essentially not a problem** (99.4% of wakeups
  land on an idle CPU; p99 = 50µs ≈ idle-exit + switch cost). The wait time lives in the
  *requeue* bucket — critical threads being **preempted**, not mis-placed at wake.

## 2. The worst events (31 waits >250µs; prior "16" was a narrower count)

Every >250µs event, categorized:

**(a) The 2.37ms + 1.06ms pair (the trace maxima)** — ts≈3493.4418s, shader-compile burst.
Workers 5417/5418 woken **by the main thread**, enqueued on cpu4 = **the waker's own CPU**,
because at enqueue **all 8 CPUs were busy** (4× `Civ6:sh_opt*` shader threads + Xwayland +
game threads → 10+ runnable threads). The main thread then ran a **3.30ms uninterrupted
slice** — the just-woken workers were not entitled to preempt it (RUN_TO_PARITY + 2.8ms
base slice, see §4). Mid-wait, cpu7 flickered idle for a cumulative ~1.0ms in short gaps and
never pulled the queued worker (newidle-balance `avg_idle`/cost heuristics skip balancing on
short idles). The workers finally migrated to cpu2 — where they then ping-ponged with each
other at 6µs switch granularity. Primary cause: **transient oversubscription** (a 6-thread
game + its own shader-compile pool briefly needs >8 CPUs). Secondary: ~1ms of stranded
runnable-while-a-sibling-idled.

**(b) 3× ~500µs wakeup waits** — workers enqueued on a CPU running `Civ6:sh_opt*` while
exactly **1** CPU was idle. Classic SIS_UTIL scan-throttle miss under high LLC utilization
(the same failure family as the earlier `sis-util-idle-floor` patch on the desktop rig).
Total damage: **1.5ms across the whole benchmark**.

**(c) Requeue waits 250µs–1.01ms** —
- behind **`vpower`** (Valve power daemon, CFS nice-0): 3 events up to 1.01ms; worker 5419
  preempted on cpu0 while cpu4+cpu7 idle — but both idle CPUs were SMT siblings of busy
  cores, and `vpower`'s wakeup was placed on the worker's CPU (SIS bail) then allowed to
  preempt instantly (PLACE_LAG gives a long-sleeper an eligible, earlier deadline).
- behind **`traced_probes`**: 8 events, 3.1ms — **our own tracer; observer effect, discount**.
- behind Civ6 thread 5410 (~10 events × ~400µs) with **0 idle CPUs** — genuine game-internal
  contention.
- behind pinned `kworker/N` threads (main thread waited 254–437µs, 7 CPUs idle): per-CPU
  kworkers cannot run elsewhere; EEVDF let them take their slice. Only a pull by an idle CPU
  could have helped; none arrived within the wait.
- 1× behind `fancontrol.py`, 1× behind RT `data-loop.0` (639µs).

## 3. Placement vs oversubscription — the crux

Neither pure story wins; the decomposition by *cause* is what matters:

| bucket | time (AI) | kernel-addressable? |
|---|---|---|
| Requeue behind **RT audio** (`data-loop.0` prio 79, `irq/66-AudioDSP` prio 49) | **150.7ms** (63%) — 106ms of it while ≥1 CPU idle | Not via EEVDF — RT class preempts CFS unconditionally. RT wake placement (`select_task_rq_rt`) prefers cache locality over sparing the CFS victim; changing that is an rt.c policy patch with a hard ceiling of 0.24% here, and audio-latency risk. Better handled in userspace (pipewire affinity/priority). |
| Requeue behind **CFS tasks** | 29.7ms (12%) | Includes 3.1ms tracer + ~7ms pinned kworkers + ~8ms Civ6-vs-Civ6. Ceiling 0.07%. |
| Wakeup waits | 59.8ms (25%) | 99.4% land on idle CPUs; this is idle-exit latency, already near-optimal. Mis-placements total ~2ms. |

The "queued on a busy CPU while idle CPUs existed" pattern is real (2,504 requeues,
129ms) — but its dominant driver is **RT audio wakeups choosing the game thread's CPU**, and
the stranded task is typically recovered in well under a tick (p95 164µs). The >500µs
stragglers show the known newidle/nohz gap (idle CPUs don't aggressively pull preempted
tasks), but the total exposure is unmeasurable at benchmark scale.

## 4. EEVDF-specific analysis

Deck 6.16.12-neptune live state: features = `PLACE_LAG PLACE_DEADLINE_INITIAL
PLACE_REL_DEADLINE RUN_TO_PARITY PREEMPT_SHORT PICK_BUDDY CACHE_HOT_BUDDY DELAY_DEQUEUE
DELAY_ZERO WAKEUP_PREEMPTION SIS_UTIL NO_SIS_PREFER_CORE …`; `base_slice_ns=2,800,000`
(2.8ms); `migration_cost_ns=50,000` (Valve already lowered 10× from mainline default);
`CONFIG_HZ=1000`, full preempt.

Both EEVDF signature behaviors are visible, and they are **opposite sides of the same dial**:
- *Latency-sensitive waiter behind a peer:* the 2.37ms event is exactly "short-running,
  frequently-waking worker waits behind the CPU-hog main thread" — RUN_TO_PARITY + the 2.8ms
  base slice protected the main thread's slice from the fresh wakeup. Flipping
  `NO_RUN_TO_PARITY` or shrinking `base_slice_ns` would cut this wait — and directly
  penalize the main thread (the actual serialization bottleneck, 80% of one CPU) with more
  preemption. On this workload that trade is likely net-negative.
- *Batch-ish daemon preempting a worker:* `vpower`/`fancontrol` long-sleepers get
  PLACE_LAG-decayed lag ≈ 0 → eligible with an earlier deadline → instant wakeup preemption
  of a mid-slice worker. That's EEVDF fairness working as specified; the fix knobs
  (NO_WAKEUP_PREEMPTION, PLACE_LAG variants) already exist as feature flags.

Nothing here is a *bug* in EEVDF placement; total CFS-vs-CFS requeue time is 29.7ms/44.9s.
There is no patch-shaped hole — only existing tunables whose combined ceiling (~0.07%) is
two orders of magnitude below measurement noise.

## 5. Contrast with SIS_PREFER_CORE (+4.9% measured on this aibench)

**Distinct phenomena — and this one is not a second patch opportunity.**

- SIS_PREFER_CORE (already built into the Deck kernel as a feature flag, `NO_` at baseline)
  changes *which idle CPU* a wakeup selects, preferring fully-idle physical cores. Its
  +4.9% comes from **reduced SMT execution contention while running** — threads computing
  slower because their hyperthread sibling is busy. That cost is invisible to runqueue-latency
  metrics: the task is "Running" the whole time, just at reduced IPC.
- The runqueue-latency finding investigated here totals 240ms (0.30%) and is dominated by RT
  preemption. Even its select_idle-adjacent slice (the 3× ~500µs SIS_UTIL scan-bail misses +
  interferer placement, ~5–8ms) belongs to the *sis-util-idle-floor* family, not
  SIS_PREFER_CORE — and is worth ~0.02%.
- Cross-check: the gfx trace shows the same shape (243ms total; 194ms requeue of which
  **177ms behind RT audio**; 49ms wakeup with 17/2,306 on busy CPUs; no >2ms outlier since
  there is no shader-burst) — confirming the AI numbers are the steady-state of this system,
  not benchmark pathology.

## Honest bottom line

The prior analysis's raw observation was correct — the only large wakeup latencies are
runqueue delays on busy CPUs, not cpuidle — but it does **not** survive as a patch target:

1. **Magnitude:** all runnable-wait combined is 0.30% of critical-thread CPU time; the
   kernel-influenceable CFS share is ≤0.07%. The project's empirical A/A band is ±5% on
   fps/turn-time. Unmeasurable.
2. **Mechanism:** 63% is RT audio priority preemption (not EEVDF, config/userspace domain);
   the worst wakeup events are transient true oversubscription (game + own shader compiler >
   8 CPUs — cannot create cores); the EEVDF-attributable remainder is slice-protection policy
   with existing feature-flag/tunable coverage (RUN_TO_PARITY, base_slice_ns, PLACE_LAG).
3. **If anything were to be done** (not recommended as a Crucible patch): pin/deprioritize
   pipewire's `data-loop.0` away from game CPUs (userspace), or experiment with
   `base_slice_ns`/`NO_RUN_TO_PARITY` via debugfs — a 5-minute A/B, no kernel build — with
   the expectation of a null result at this workload's noise floor.

**Recommendation:** close this line of investigation. SIS_PREFER_CORE remains the only
scheduler-placement change with demonstrated signal on this machine; the runqueue-latency
tail is not a second bottleneck, it is the residue of RT audio + burst oversubscription.

---
*Method details: trace_processor_shell v56.1 on `/tmp/claude-1000/baseline-{ai,gfx}.pftrace`;
wait = `thread_state` R/R+ interval; wakeup kind matched to `sched_wakeup` (`target_cpu` =
enqueue CPU; only 13/2,946 migrated between enqueue and run); per-CPU occupancy sampled at
wait start from full `sched_slice` coverage (incl. swapper) and idle overlap integrated over
the wait for all >500µs events. Deck tunables read live 2026-07-12 from
`/sys/kernel/debug/sched/` over SSH. Query scripts:
`/tmp/claude-1000/-home-void-upstream-crucible/46ab6b49-4752-4403-b923-f676c7c98ba1/scratchpad/eevdf/`.*
