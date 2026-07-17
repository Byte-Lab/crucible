# XP2 Benchmark Trace Analysis — Steam Deck (Van Gogh), Civ 6 xp2benchmark

Trace: `/tmp/claude-1000/baseline-xp2b.pftrace` (54.9s, capture started launch+48s).
CSV: `/tmp/claude-1000/baseline-xp2b.csv` (1203 frames, 30.0s of frames, mean 24.9ms, median 25.8ms, p95 31.1ms, max 297ms).
No ftrace event loss (`stats` clean).

## 1. Scene coverage: PARTIAL — the capture caught only the first ~6s of the scene

The 48s pre-delay was not enough. xp2 loading took ~97s from launch this run, not ~44s:

| Trace window | What it is | Evidence |
|---|---|---|
| 0–2s | load burst | 935 cs_ioctl/s, no presents |
| 11–16s | animated loading/menu screen | 69 flips/s but Civ6 CPU only 0.2 cores, ~1 GPU job/frame |
| 17–33s | single-threaded load | ~1.1 cores busy, GPU idle |
| 34–42s | multi-threaded load (asset/map prep) | Civ6 ~5.9 cores busy, GPU idle, zero presents |
| **49.1–52.8s** | **scene, steady** | sustained ~160 cs_ioctl/s, ~60 commits/s, render threads (gl0/gdrv0/cs0) active |
| 52.8–54.9s | scene, hitching (warm-up) | Civ6:sh_opt0-3 shader-compile threads burn 0.8s/2.5s per sec; 32 bo_moves; irregular CS; CSV max frame 297ms |

So the analysis below is a **3.7s steady slice at the very start of the scene** (matching the CSV's first ~27–30ms frames), not mid-scene. It is internally consistent and event-loss-free, but magnitudes should be confirmed with a recapture using a **~115–125s delay (or trigger on sustained cs_ioctl rate)** — load time is evidently variable (44s → 97s between runs).

## 2. CPU vs GPU bound (steady window 49.1–52.8s)

- **GPU gfx ring busy: 34.9%** (1290ms/3700ms, 578 jobs; run_job→process_job union, fence-ptr paired). sdma0: 0.5%. → **NOT GPU-exec-bound.** The geometry-shader scene does not saturate the RDNA2 iGPU at this point in the scene.
- **Total CPU: 11.9%** of the machine (3527ms of 8×3700ms). Busiest thread = Civ6 main (tid 45117): **31% of one core** (1144ms). Then gl0 7%, gdrv0 6%, cs0 2.5%, five job-worker threads ~4% each.
- Per frame at ~40fps (~148 frames in window): **~8.7ms GPU + ~11.6ms serialized Civ6 CPU** (main 7.7ms + gl0 1.8ms + gdrv0 1.5ms + cs0 0.6ms) + pipeline bubbles = ~25ms frame.

**Neither side is saturated.** The frame is a serialized latency chain (main → gl0 → gdrv0 → GPU → back), each stage bursty with idle gaps — the classic pattern where per-core utilization stays low.

## 3. Is it a scheduling (SMT) problem? NO

Main thread state split over the window: Running 1144ms, Sleeping 2515ms, **Runnable only 41ms (1.1%, avg 18µs/wakeup over 2238 wakeups)**. gl0/gdrv0/cs0 runnable time similarly negligible. The machine is 88% idle; there is no run-queue contention and no SMT co-residency pressure. **Not a duplicate of the SMT full-core-placement patch, and no new scheduler-placement bottleneck.**

## 4. CPUFREQ — the dominant kernel-side finding (and it's a DUPLICATE)

During the steady scene, **all 8 CPUs average 1.97–2.15 GHz vs fmax 3.5 GHz** (weighted by sample duration; min 1.55 GHz, occasional 3.5 GHz bursts). The main thread ran mostly on CPUs 6/1/0 — all ~2.0–2.1 GHz average.

Because every thread in the frame chain has <35% duty cycle, amd-pstate sees "no busy core" and keeps clocks near the floor — while the workload is *latency*-bound on exactly those threads. This is **the same mechanism as the already-patched amd-pstate single-thread downclock** from the graphics benchmark, expressed here across a multi-thread bursty chain rather than one thread.

**Verdict: DUPLICATE of the amd-pstate/EPP patch.** Expected upside if clocks reached ~3.5 GHz on the chain: serialized CPU ~11.6ms → ~6.8ms per frame, an upper bound of ~4–5ms/frame (~+20–25% fps at 25ms frames); realistic gain lower due to CPU/GPU overlap. **Action: re-run xp2benchmark with the existing dynamic_epp/amd-pstate patch applied and diff the CSVs — no new patch needed for this.**

## 5. Submission / completion path — healthy, no distinct patch target

- **Submit latency (drm_sched_job → drm_run_job), gfx:** n=578, p50 **31.8µs**, p90 43µs, p95 46µs, p99 2.6ms, max 3.6ms. The ≥1ms outliers (~6 jobs, ~1%) cluster at scene start (49.16–49.28s) and the shader-compile hitch (53.2s) — dependency stalls during warm-up, not drm-sched worker latency. Aggregate impact <0.5% of the window. **No drm-sched patch warranted.**
- **Completion (amdgpu HW fence signal → drm_sched_process_job):** avg ~112µs (nearest-preceding pairing, slightly pessimistic due to sdma contamination) — ≤0.5% of a 25ms frame even at 4 jobs/frame. **No IH/IRQ bottleneck.**
- **dma_fence waits:** essentially zero (Civ6:cs0 total 0.63ms across 578 waits in the whole window). The app does not block on fences.
- **TTM/bo_move:** concentrated in loading (467/s at sec 25); steady scene window has ~none; 32 moves coincide with the sec-53 shader-compile hitch. **No memory-migration bottleneck in-scene.**
- **Presentation:** panel vblank 90Hz; gamescope commits avg 16.5ms (min 10.7/max 22.6) — adaptive, not vsync-quantized; game fps (~37–40) is not a clean 90/N divisor, so no pacing cap.

## 6. Honest verdict

**No distinct kernel-addressable bottleneck beyond what is already patched.** In the captured slice, xp2 is a low-duty-cycle serialized CPU chain running at ~2.1 GHz on an 88%-idle machine with a 35%-busy GPU — i.e., **a duplicate of the amd-pstate downclock finding**, in an amplified multi-thread form. drm-sched submit, IH/fence completion, TTM, and scheduler placement are all quantifiably healthy. The one new-ish observation — the shader-compilation hitch (sh_opt threads + bo_moves, 297ms max frame) — is a Mesa/userspace shader-cache warm-up issue, not a kernel patch target, and disappears with a warm cache.

**Caveats / next capture:** this slice is the first ~4s of the scene only. Mid-scene the geometry load may rise (GPU busy could climb toward saturation, changing the verdict toward GPU-exec-bound). Recapture with **delay ≥115s or an activity trigger** (start when cs_ioctl >100/s sustained for 3s), 60–90s duration, same config — then (a) re-check GPU busy %, and (b) A/B the existing amd-pstate patch on xp2.

## Appendix: key numbers

| Metric | Value (49.1–52.8s window) |
|---|---|
| GPU gfx busy | 34.9% (1290.7ms / 3700ms, 578 jobs) |
| GPU sdma busy | 0.5% |
| Total CPU util (8 threads) | 11.9% |
| Civ6 main thread | Running 31%, Runnable 1.1% (avg 18µs), Sleep 68% |
| CPU freq avg (all cores) | 1.97–2.15 GHz (fmax 3.5) |
| Submit latency gfx | p50 31.8µs, p95 46µs, p99 2.6ms |
| HW-fence→process_job | avg ~112µs |
| dma_fence wait (app) | 0.63ms total / 3.7s |
| Frame cadence (commits) | avg 16.5ms; CSV frames mean 24.9ms |
