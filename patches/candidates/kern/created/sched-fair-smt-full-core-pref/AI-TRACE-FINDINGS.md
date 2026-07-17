# Civ 6 AI Benchmark (Steam Deck / Van Gogh) — Perfetto Trace Analysis

Trace: `/tmp/claude-1000/baseline-ai.pftrace` (44.94s, ts base 3473973965182).
Analyzed with `trace_processor_shell` v56.1 (`/home/void/.cache/infera-train/trace_processor_shell`).
Frame CSV: `/tmp/claude-1000/baseline-ai.csv` — n=1215, sum 30.0s, p50 25.4ms, p99 43.4ms, p999 74.3ms, max 295.4ms.

**CSV/trace alignment caveat:** the CSV implies ~40fps continuous presenting for 30s; the trace
shows presents (`amdgpu_dm_atomic_commit_tail`) only in a 21.9s window and steady ~34fps rendering
for only ~6s (trace sec 12–18). The CSV window does not coincide with this 45s capture (different
run or different phase). Trace-internal reconstruction below is self-consistent and is what the
conclusions rest on.

## 1. Workload structure (this is NOT a frame-paced workload)

The AI benchmark is **bimodal fork/join CPU compute with sparse presents**:

| Phase | Wall time | Evidence |
|---|---|---|
| Serial AI (exactly 1 Civ6 thread busy) | **26.9s (60%)** | busy-count timeline from 123k sched slices |
| Parallel burst (exactly 6 Civ6 threads busy) | **8.5s (19%)** | same; 2–5 busy states are ~0 |
| All Civ6 threads asleep | **9.2s (20%)** | main thread S-state total 9.06s |

- Main thread tid 5377 (`Civ6 (WinID 2)`): Running **35.75s / 44.9s (80%)**, max continuous
  on-CPU slice 512ms. It is the primary waker of the 5 workers (392 explicit wakeups) → coordinator/join thread.
- Workers tids 5417–5421: Running ~8.7s each, only during bursts; they also wake each other (work distribution).
- Rendering: all 2671 `amdgpu_cs_ioctl` come from `Civ6:cs0` (tid 5367), gfx ring ctx 767,
  ~2.2 CS/frame. Steady rendering only sec 12–18 (~69 CS/s ≈ 34fps); bursts at sec 0–3 and 25 (load/UI);
  **19 CS gaps > 300ms totaling 35.2s** — the game simply does not render while computing AI turns.
- During the sec 12–18 render window the main thread sleeps ~94% (≈940ms/s) — vsync/present-paced, not CPU-bound.

## 2. The frame tail: reconstructed worst present gaps and what fills them

Worst present-to-present gaps (`amdgpu_dm_atomic_commit_tail_finish`):
5489ms (@sec 27.5), 4089ms (@32.1), 2811ms (@35.0), 1378ms (@19.5), 944ms (@20.4),
567ms (@21.0), 533ms (@22.0), 511ms (@28.0), 500ms (@21.5).

What the main thread was doing inside these windows (per-second state breakdown + slice inspection):

- **Pure userspace AI compute**: e.g. the 5489ms gap = sec 22–27: main thread Running 984–998ms
  of every second, then two exact 500ms sleeps.
- **Exact round-number timer sleeps**: the main thread's only long sleeps are
  **8 × 500.05–500.25ms + 1 × 200.07ms ≈ 4.2s total**, all with **no waker** (= timer expiry,
  i.e. `nanosleep`/timed-wait poll loops in game code). Every 500–570ms present gap in the list
  above coincides 1:1 with one of these sleeps.

So the 200–340ms p999 frames are presents straddling (a) multi-hundred-ms uninterrupted AI compute
slices and (b) the game's own 200/500ms poll-sleeps. **Neither is a kernel wait.**

## 3. Kernel-side wait budget (the critical thread, whole 44.9s trace)

| Component | Main thread 5377 | Notes |
|---|---|---|
| Running | 35,750ms | userspace compute |
| Sleeping (S) | 9,062ms | 4.2s exact timer poll-sleeps + ~4.4s vsync waits in render window |
| **Runnable wait (R)** | **48.2ms (0.13% of runtime)** | workers: 8–70ms each |
| **Preempted (R+)** | **2.6ms** | negligible |
| **Uninterruptible (D)** | **64.3ms** (max 18.4ms) | minor I/O |
| dma_fence waits (whole system) | game: **2.2ms** total (Civ6:cs0, 2596 waits, max 15µs); kworkers on sdma0: 114ms | **not GPU-bound, no submission stalls** |
| amdgpu_bo_move | 1629 moves / 3.96GB, concentrated at sec 0–3 and 25–26 (load/UI burst), ~0 during AI phase | TTM/eviction **not** coincident with spikes |
| GPU sched | drm_sched_job→run→process healthy; GPU essentially idle during AI compute | CPU-bound confirmed |
| cpuidle | C3(deepest): 88k entries/237 CPU-s; C2: 71k @ avg 420µs | exit latency is already contained in the 48ms R total → immaterial |
| cpu_frequency | **zero events in trace** | amd-pstate active/EPP mode does fast-switch with no cpufreq tracepoints — frequency behavior is *invisible*, see §5 |

Total attributable kernel-side waiting on the critical path: **< 120ms out of 44,900ms (≈0.25%)**.
Runqueue latency, EEVDF placement latency, fence stalls, TTM migrations and idle-exit latency are
all individually quantified and all negligible **as waits**.

## 4. The one real, measured kernel-side inefficiency: SMT victim selection

CPU busy time: cpus {0..7} = {5.9, 13.3, 16.0, 16.6, 5.3, 12.7, 9.8, 7.8}s; avg 1.95 CPUs busy.
Assuming standard AMD adjacent SMT enumeration (cores = {0,1},{2,3},{4,5},{6,7}):

- Sibling both-busy time: **19.4 pair-s** (17.7 pair-s among Civ6 threads alone).
- Of that, only **0.41 pair-s** had a fully-idle core available → during 6-thread bursts on
  4 cores the *amount* of SMT sharing is mathematically forced. EEVDF placement is near-optimal
  in quantity terms.
- **BUT the choice of victim is wrong**: the coordinator/join thread 5377 runs
  **9.04s of its 35.75s runtime (25.3%) with its SMT sibling busy — 8.67s of that with the
  same worker (tid 5421) co-resident on its sibling.** On Zen 2, co-runner throughput loss is
  ~30–40%, so the burst-phase critical path (main thread, which every join waits on) is degraded
  for essentially the entire 8.5s parallel phase.
- Wake-placement evidence: of **140** worker burst-start wakeups, **15 (11%)** landed on the main
  thread's SMT sibling **while a fully-idle core existed at that instant**. Also observed
  simultaneous-wake clumping: two workers placed onto both threads of one just-idle core while
  another core was entirely idle (e.g. @0.873s: 5421→cpu5 with 5418 on cpu4, cores {0,1} idle).
  Once placed, nothing corrects it — during the burst there are no idle cores, so periodic/idle
  balancing cannot change which threads pair up.

Mechanism: in `select_idle_sibling()` (kernel/sched/fair.c), the `target`/`prev` fast paths return
an idle CPU **before** checking `test_idle_cores()` — an idle prev whose sibling is busy wins over
a fully-idle core elsewhere. For a fork/join burst, the first workers to wake grab whole cores, and
late wakers whose `prev` happens to be the coordinator's sibling get parked there for the entire burst.

## 5. Frequency: the unmeasured elephant

The trace has **no CPU frequency data at all** (amd-pstate active mode = autonomous CPPC
fast-switch, no `cpu_frequency` tracepoints; the perfetto config's `cpu_frequency` category
recorded nothing). Given (a) the graphics benchmark on this device already exposed an
amd-pstate downclock that needed patching, (b) 60% of this workload is single-thread serial
compute where sustained boost (~3.5GHz) vs a mid clock is worth tens of percent directly, and
(c) the 15W shared CPU/GPU envelope plus constant background wakeup churn
(pipewire/data-loop/SDLAudioDev ≈ 18k slices, 2.6k wakeups/s system-wide) — frequency policy is
potentially the *largest* lever but is **unverifiable from this capture**.
Re-capture with `amd_pstate` tracepoints (`amd_pstate_perf`) or periodic APERF/MPERF (or
`scaling_cur_freq` polling) before ruling it in or out.

## 6. Ranked bottleneck list

1. **Userspace AI compute + the game's own 200/500ms poll-sleep loops — dominates the p999 tail.**
   ~80% of wall is single/multi-thread compute; 4.2s of exact timer sleeps sits directly under the
   0.5s-class present gaps. **Not kernel-addressable.** (Honest bottom line: the p999 200–340ms
   frame tail in the AI benchmark is game-behavior, not kernel latency — every kernel wait class
   sums to <0.25% of the critical path.)
2. **SMT victim selection at burst start (kernel, measured):** critical coordinator thread SMT-degraded
   for 25.3% of its runtime; 11% of worker wakes provably misplaced onto its sibling despite an idle
   core. Upside bound: ~35% × 9.04s ≈ up to ~3s of critical-path recovery over 45s (≈5–6% of
   benchmark wall) *if* the coordinator is on the burst critical path (its role as primary waker and
   its 512ms uninterrupted burst slices support this); realistically less. This is the **largest
   kernel-addressable item measurable in this trace**.
3. **amd-pstate frequency policy during the serial phase (kernel, unmeasured):** potentially larger
   than #2 (affects 60% of wall) but invisible in this capture — needs a re-capture with frequency
   visibility before patching.
4. Everything else — runqueue latency (48ms), dma_fence (2.2ms), TTM/bo_move (load-time only),
   cpuidle exit latency (contained in the 48ms), preemption (2.6ms) — quantified and dismissed.

## 7. Top candidate: kernel patch hypothesis

**Subsystem/file:** scheduler wake placement — `kernel/sched/fair.c`, `select_idle_sibling()` /
`select_idle_cpu()`.

**Hypothesis:** when `sched_smt_active()` and the LLC still has a fully-idle core
(`test_idle_cores(target)`), do not take the `target`/`prev` idle-CPU shortcuts if that CPU's SMT
sibling is currently running — fall through to `select_idle_cpu(..., has_idle_core=true, ...)` so
the waker lands on a whole idle core instead of half of an occupied one. (Sacrifices a little
cache-warmth for guaranteed full-core throughput; directly fixes both observed failure modes:
worker parked on the coordinator's sibling, and two simultaneous wakers clumping onto one core.)

**Predicted effect on aibenchmark:** shorter parallel-burst phases (coordinator runs unshared →
faster joins), bounded at ≈5% wall; no effect on the p999 tail (which is userspace). Validate with
A/B runs of turn-time totals, plus `perf bench sched messaging/pipe` + schbench for regression per
the winner-validation protocol. A zero-risk pre-test: `taskset` the 5 workers away from the
coordinator's core in userspace and measure — if that shows no win, the kernel patch won't either.

**Caveat:** sibling pairing assumed adjacent ({0,1},{2,3},… — standard AMD enumeration, matches
Steam Deck lscpu); if Van Gogh paired ({0,4},…) the victim analysis would need re-running (raw
overlap under that pairing is 24.4 pair-s, so SMT sharing is substantial under either pairing).
