# Trace Findings — Steam Deck (Van Gogh) Civ 6 Graphics Benchmark

**Trace:** `baseline-gfx.pftrace` (44.8 s span). **Ground truth:** `baseline-gfx.csv`, 1222 frames, 30.01 s, mean **24.56 ms/frame (40.7 fps)**, median 25.37 ms, p99 40.8 ms, max 298.8 ms.

## TL;DR — the frame time is CPU-execution-bound on ONE thread, not GPU/fence/scheduler-bound

Of the ~24.6 ms per frame, **>96 % is CPU execution on a single render/submit thread** (`Civ6 (WinID 2)`, tid **4798**). The GPU, the DRM/fence path, and the scheduler are all provably NOT the bottleneck. The only kernel lever that can move this number is **CPU frequency / perf-state on the pegged core** (amd-pstate), with a measurable secondary contributor in **cpuidle deep-state over-selection**.

## Per-frame budget (~24.6 ms)

| Component | Evidence | Per-frame | Verdict |
|---|---|---|---|
| **CPU exec on render thread 4798** | 4798 on-CPU 35.6 s / 44.8 s overall; **98 % pegged** in the heavy window with Runnable-wait ≈ 0 | **~23.7 ms** | **THE bottleneck** |
| GPU execution | `job` slices: 2001 jobs, avg 711 µs, total **1424 ms = 3.2 % GPU busy** | ~0.8 ms | not bound |
| GPU/fence wait | `dma_fence_wait`: 2036 waits, avg 61 µs, total **126 ms = 0.28 %** | ~0.1 ms | not bound |
| CPU-scheduling latency | Runnable (R) state, all Civ6 threads, heavy window: **399 ms vs 55.6 s running = 0.7 %** | negligible | not bound |

## Systematic evidence (what was ruled OUT, with numbers)

1. **NOT GPU-bound.** GPU busy 3.2 % of wall. Longest GPU job 7 ms; per-frame GPU exec ~0.8 ms. The RDNA2 iGPU is nearly idle during a "graphics" benchmark.
2. **NOT GPU-submission/fence-stalled.** Total time the CPU spends inside `dma_fence_wait` is 126 ms across the whole 44.8 s trace (0.28 %). Average wait 61 µs. There is no submission stall and no GPU back-pressure.
3. **NOT scheduler-queueing-bound (EEVDF is fine).** Even in the CPU-heavy window (sec 32–44, 54 active Civ6 threads on 4c/8t), aggregate Runnable-wait for all Civ6 threads is **399 ms vs 55.6 s running (0.7 %)**, avg wakeup-to-run **21 µs**. The lead thread 4798's own Runnable-wait is 18 ms in that window. Threads run essentially the instant they wake — no CPU starvation, no EEVDF placement penalty.
4. **NOT migration/cache-thrash-bound.** Thread 4798 migrated **47 times total**; it resumes on the *same* CPU 98.4 % of the time (mostly pinned to CPU3, then CPU6). The scheduler keeps the hot thread sticky.
5. **NOT TTM/BO-migration-bound.** `amdgpu_bo_move` = 883 total; a burst of 463/s occurs in a ~2 s asset-load window (sec 21–23) but does not stall the render thread (fence waits stay flat, 4798's uninterruptible-D time is 33 ms total). Off the critical path.

## The workload has two regimes

- **Sec 0–31 (light / menu + early scenes):** thread 4798 pegs **100 % of one core while the other 7 cores are ~idle** (system-wide util ~14 %). Purely single-thread-limited. The long 500 ms blocking episodes here are `select()` timeouts (menu idle), not stalls.
- **Sec 32–44 (heavy flythrough):** 54 active Civ6 threads, aggregate core util ~53 %, 4798 still 98 % pegged. Still no runnable backlog (R = 0.7 %). Multi-threaded but *still* gated by the single lead thread.

In both regimes the frame pace is set by one core executing thread 4798's per-frame draw-submission work (classic Aspyr D3D→GL/DXVK single-thread submit cost). The kernel cannot reduce the instruction count.

## The one measurable kernel-side pathology: cpuidle over-selects the deepest C-state

Van Gogh exposes idle states 0–3 (3 = deepest, CC6-class). Whole-trace, all CPUs:

| State | Entries | Total idle | Entries waking < 250 µs |
|---|---|---|---|
| 1 (shallow C1) | 6,917 | 0.75 s | 6,175 |
| 2 | 67,255 | 29.0 s | **25,758** |
| 3 (deepest) | 87,623 | 239 s (÷8 cores) | **15,753** |

The governor picks **state 2/3 ~155k times**, and **~41,500 of those wake within 250 µs** — i.e. it repeatedly commits cores to deep idle for sub-target-residency naps. In the heavy window alone: 25,517 deepest-state entries (10,206 sub-250 µs) vs only 1,914 shallow-state selections.

**Caveat (rigor):** measured average wakeup-to-run latency is only 21 µs, so the *exit-latency* cost of this is small and NOT what is eating the frame budget. Its real relevance is the AMD-specific coupling below.

## TOP bottleneck → kernel area and patch hypothesis

**Diagnosis:** single-thread CPU-execution-bound. The only kernel-addressable variable is the **effective clock of the core running thread 4798.**

**Primary lever — `drivers/cpufreq/amd-pstate.c` (perf-state / EPP).**
The trace contains **zero `cpu_frequency` events** despite the config requesting them. On this platform that is the *signature of amd-pstate in active/EPP mode*, where the SMU picks frequency autonomously and the kernel emits no P-state transitions to trace. Consequences and hypothesis:
- A single latency-critical thread is pegged while 7 cores idle. Under SteamOS's default EPP (`balance_performance`) the SMU biases toward efficiency, and the render thread's frequent short futex hand-offs (≈1,700 sub-0.5 ms sleeps during active rendering) collapse the per-core utilization/perf signal so the hardware down-clocks between bursts and each burst re-ramps.
- **Patch:** for a pegged foreground/latency-critical task, raise the amd-pstate `min_perf` floor and/or drive EPP toward `performance` on the cores running it (a scheduler→cpufreq hint, or `amd_pstate_epp` policy). On a workload that already leaves 7 cores idle this costs almost no power and directly shortens the ~23.7 ms/frame CPU execution.

**Secondary, coupled lever — `drivers/cpuidle/governors/teo.c` (idle-state selection) + AMD CC6 frequency-restore.**
On AMD, entering the deepest CC6 idle drops the core's boosted frequency; on wake it restarts low and ramps (~1 ms). The governor's habit of committing worker cores to state 2/3 for sub-250 µs naps (41.5k such events) therefore penalizes the *frequency* of post-idle work, not just its latency. **Patch:** bias the idle governor against the deepest state for short predicted idles on this platform (or apply a PM-QoS/`per-cpu` latency hint for cores running game threads), keeping them in C1 so they retain frequency residency.

**Confidence / next step:** the trace *proves* CPU-bound and *rules out* GPU, fence, EEVDF, migration, and TTM. It *points* to frequency as the sole remaining lever but **cannot confirm under-clocking because it lacks `cpu_frequency`/`amd_pstate` tracepoints.** Before committing a patch, re-capture with `amd_pstate` / CPPC-perf tracepoints (or sample `scaling_cur_freq`) to confirm the pegged core is below fmax during the heavy window. If it is at fmax already, the frame time is irreducibly userspace-bound and no kernel patch will help; if it is below fmax, the amd-pstate `min_perf`/EPP patch above is the highest-value change.
