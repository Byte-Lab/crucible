# XP2 Benchmark Trace Analysis — FINDINGS

**Trace:** `/tmp/claude-1000/baseline-xp2.pftrace` (44.87s, boot-ts 3792.04–3836.91s)
**CSV:** `/tmp/claude-1000/baseline-xp2.csv` (1202 frames, sum 30.0s, avg 24.96ms)

## HONEST VERDICT (read this first)

**The perfetto trace does not cover the benchmark scene. No xp2 bottleneck verdict is
possible from this capture — recapture required.** The 45s trace window landed almost
entirely on the xp2 *loading sequence*; the benchmark's first render submissions appear
at t=44.05–44.47s, i.e. the scene starts right as the capture cuts off. The CSV's 30s of
frames are entirely post-trace.

Evidence for the mismatch (all quantified):

| Check | Expected if trace covered the bench | Observed |
|---|---|---|
| Game CS submissions (`amdgpu_cs_ioctl`, all from `Civ6:cs0` tid 44481) | ≥1202 at ~25ms cadence for 30s | 1793 total; 980 in the *first second* (menu/warmup burst), 335 in t=10–15s, 375 in a 1s burst at t=22, near-zero elsewhere; burst restarting at t=44.05 |
| gfx-ring busy (`drm_run_job`→`drm_sched_process_job`) | sustained high occupancy for 30s | 1.32s / 44.87s = **2.9% overall**; only sustained window is t=10–15s at 22–24% busy |
| Display flips (`amdgpu_dm_atomic_commit_tail_begin`) | ~30–40/s for 30s | 358 total; ~69/s only during t=10–15s with inter-flip gaps of 11.1ms/22.2ms (90Hz Deck OLED cadence with skips — a vsync'd *loading screen*, ~14.5ms frames, not the CSV's 25–34ms frames) |
| Frame pacing match with CSV | 25–34ms frame cadence | no window in the trace has that cadence |

What the trace actually contains (xp2 load pipeline on the Deck):
- **t=0–1s:** ~980 CS in one second — pre-bench menu / warmup burst.
- **t=1–10s and t=20–30s:** main thread (`Civ6 (WinID 2)` tid 44491) pegged ~100% of
  one CPU, GPU idle — single-threaded loading.
- **t=10–15s:** vsync'd loading screen at ~69fps effective (90Hz with skipped vblanks),
  GPU 22–24% busy, main thread 85% asleep (timer wakeups).
- **t=22s:** 375-CS warmup burst; bo_moves peak here (569 of 987 total in t=20–25s).
- **t=30–45s:** main thread + 5 worker threads (tids 44527–31) all ~100% pegged
  (bucket t=35–40s: 6 threads × ~5s/5s = 6 CPUs saturated), zero GPU — map/AI init,
  well parallelized.
- **t=44.05s:** benchmark scene CS submissions begin; trace ends at 44.87s.

## Answers to the numbered questions (as far as the data allows)

1. **CPU vs GPU bound:** UNANSWERABLE for the scene itself (not captured). During the
   only rendered window in-trace the GPU was 22–24% busy — but that's a loading screen.
2. **Submission latency (loading-phase only):** paired all 3044 `drm_sched_job`→
   `drm_run_job` by job id: avg 319us, max 6.1ms, 392 jobs >1ms. Caveat: this includes
   ring serialization behind in-flight jobs and cross-ring dependency waits, and it is
   loading-phase traffic — **not evidence of a drm-sched worker scheduling problem.**
3. **dma_fence waits:** 2979 waits (all from `Civ6:cs0` + kworkers, none from the main
   thread), **134ms total over 44.9s**, avg 45us, max 5.0ms. No fence-wait problem visible.
4. **amdgpu IH / fence-signal latency:** with total fence-wait time at 134ms and avg
   45us there is no measurable IH bottom-half latency issue in the captured window.
5. **bo_move / TTM:** 987 bo_moves in 44.9s (22/s), 58% concentrated in the t=20–25s
   asset-load burst. No steady-state eviction pattern observable (no steady state captured).
6. **Known-bottleneck rule-out:** cannot be done for the scene. Note: **this trace has
   no `cpu_frequency` events at all** (only `cpu_idle`) — amd-pstate active mode doesn't
   fire cpufreq transition notifiers, so even a correct-window recapture cannot measure
   the downclock without adding `power/cpu_frequency` via the passive path, polling
   `amd_pstate` sysfs, or the `amd_pstate_epp` tracepoints.
   Scheduling-health facts that *were* measurable: main thread runnable-wait was only
   66ms total across 44.9s (no run-queue delay anywhere), workers 50ms — whatever the
   xp2 bottleneck is, the loading phases show no SMT/placement pathology.

## Incidental observations (not the asked-for deliverable, but real)

- The load sequence is **~44s of mostly single-thread CPU-bound work** with two pegged
  single-thread phases (t=0–10, 20–30). If the busy core sags like the graphics-bench
  case, the existing amd-pstate EPP patch would speed up *loading*, not the scene.
- The t=30–45s phase saturates 6 hardware threads on a 4c/8t part — during that phase
  SMT co-scheduling is unavoidable (more runnable threads than cores).

## Required next step (recapture recipe)

1. Re-run xp2benchmark with the perfetto capture **started when frames begin flowing**
   — trigger on the MangoHud CSV file appearing, or on the first sustained
   `amdgpu_cs_ioctl` burst — or simply extend `duration_ms` to ≥90s (load alone eats
   ~44s) and trim to the frame window in analysis.
2. Add a CPU-frequency source to the config (e.g. `linux.sys_stats` cpufreq polling at
   ≤50ms period), since ftrace `power/cpu_frequency` is silent under amd-pstate active
   mode on this kernel. Without it, ruling the EPP-downclock in/out is impossible.
3. Then re-run this exact analysis: gfx-ring occupancy per frame, main-thread state
   split, fence-wait vs GPU-busy overlap, submit latency in steady state.

## Bottom line

No distinct kernel-addressable bottleneck can be claimed for xp2 from this capture,
and none of the known bottlenecks can be ruled in or out — **the trace window missed
the benchmark**. The only defensible statement: during the 45s captured, the machine
was CPU-bound in the game's load path (single-threaded for ~20s of it), the GPU was
≤3% busy overall, and the drm-sched / dma_fence / IH paths showed no pathology at
loading-phase load levels.
