# Hogwarts Legacy (UE4 / Proton) — Steam Deck main-menu trace analysis

Trace: `/tmp/claude-1000/hogwarts-menu.pftrace` (29.90 s, Van Gogh 4c/8t, kernel 6.16.12-neptune, EEVDF).
Tool: `trace_processor_shell` direct SQL. All numbers below are measured, not estimated.

## VERDICT (honest, up front)

**GPU-EXECUTION-BOUND. No distinct kernel-addressable bottleneck.** Same class as the
vkmark finding: the gfx ring is occupied **99.94%** of wall clock with **zero gaps > 100 µs
in the entire 30 s trace** — the CPU always has the next job queued before the previous one
retires. A kernel change cannot reduce GPU shader-execution time.

**It is also NOT a dynamic_epp duplicate.** The CPUs do run far below fmax
(mean ~1.45 GHz vs 3.5 GHz fmax), but this is not the bursty-thread-parked-at-low-EPP
pattern: all 8 CPUs sit pinned in a narrow 1.38–1.86 GHz band **continuously for 30 s**
(the GameThread is 87%-duty-cycle *sustained*, not bursty), which is Van Gogh SMU
power-budget sharing — the ~15 W envelope is being handed to the 100%-busy GPU. Boosting
CPU EPP here would steal package power from the GPU and would plausibly *regress* fps.
Measuring dynamic_epp on Hogwarts is therefore NOT clean 3rd-workload evidence for the
menu scene — a CPU-bound in-game scene (e.g. Hogsmeade crowds) would be the right
Hogwarts workload for that.

Not SIS_PREFER_CORE-relevant either: runnable-wait on every critical thread is < 1% (below).

## 1. Repeatability — EXCELLENT A/B scene

894 present intervals (`amdgpu_dm_atomic_commit_tail_finish`):

| metric | value |
|---|---|
| mean | 33.41 ms (29.9 fps) |
| p50 | 33.33 ms |
| p5 / p95 | 33.19 / 33.57 ms |
| p99 | 44.44 ms |
| stdev | 3.06 ms (CV 9.2%, entirely from vblank quantization) |
| min / max | 22.19 / 44.48 ms |

Panel is 90 Hz (`drm_vblank_event` = 88.8 Hz measured). Presents are quantized to the
vblank grid: 826/894 intervals land at 3 vblanks (33.3 ms), 31 at 2 vblanks (22.2 ms),
37 at 4 vblanks (44.4 ms). No drift, no loading transient — a steady scene, ideal for A/B.

## 2. GPU-bound confirmation — execution, not starvation

- gfx ring (`sched-gfx_0.0.0`, drm_run_job → drm_sched_process_job): 8,359 jobs,
  union busy **29.883 s of 29.901 s = 99.94%**.
- **Gaps > 100 µs between consecutive gfx jobs: 0** (zero) across the whole trace.
  Total GPU-idle-between-presents ≈ 18 ms / 30 s (0.06%), all at trace edges.
  → App-late (CPU submission gap) attribution: **0**. Genuine back-to-back GPU exec: **100%**.
- Jobs are perfectly serialized: sum(dur) == union coverage (no overlap); ~9.35 jobs/frame,
  mean job 3.58 ms, p50 0.45 ms, max 18.5 ms → 9.35 × 3.58 ≈ 33.4 ms = exactly the frame time.
- Compute rings are noise: comp_1.0.1 + comp_1.2.1 = 895 jobs, 137 ms total (0.5%).
- The 37 four-vblank hitches all contain a >8 ms gfx job, i.e. hitches are also GPU-exec
  (heavy pass runs long, misses the 3-vblank slot) — not CPU, not kernel.

## 3. UE4 CPU threads — nobody is on the critical path

Per-thread state over 29.9 s (thread_state):

| thread (tid) | Running | Sleep | Runnable (R+R+) | notes |
|---|---|---|---|---|
| GameThread `HogwartsLegacy.` 53508 | 25.93 s (86.7% of a core) | 3.73 s | **0.231 s (0.77%)** | busiest thread; sustained, not bursty |
| RHIThread 53577 | 9.58 s (32%) | 20.0 s | 0.301 s (1.0%) | submission never late (ring never starves) |
| RenderThread 2 53674 | 6.43 s (21.5%) | 23.2 s | 0.266 s (0.89%) | |
| vkd3d_queue 53571 | 2.24 s | 27.2 s | 0.396 s | |
| 18× TaskGraphThread | 13.3 s total | — | — | spread thin |

Whole-package CPU utilization: swapper idle = 157.1 s / (8 × 29.9 s) → **CPU only ~34% busy**.
Runnable-wait is negligible everywhere → no scheduling/placement (SMT) issue in this trace.
The GameThread at 87% duty cycle is the closest thing to a CPU risk, but with the GPU at
99.94% it is not the pacer — it has ~13% headroom even at 1.45 GHz.

## 4. cpufreq — low, but SMU power sharing, not an EPP bug

`linux.sys_stats` cpufreq counters, all 8 CPUs, time-weighted over 30 s:

- mean **1450–1454 MHz** per CPU; observed range **1380 – 1859 MHz**; fmax 3.5 GHz never approached.
- While the key threads execute: GameThread **1462 MHz**, RHIThread **1447 MHz**,
  RenderThread **1447 MHz** — i.e. running at **~41% of fmax**.

Interpretation: uniform, sustained, all-core ~1.45 GHz with the GPU pegged is the Van Gogh
STAPM/fast-PPT budget split, not a per-thread EPP mis-governing. The dynamic_epp patch
targets bursty threads left below fmax when power headroom exists; here there is **no
package power headroom** — the GPU is consuming it productively. Expected upside from
dynamic_epp on this scene: **~0, possibly negative** (CPU boost taxes the shared 15 W
envelope). Do not use the menu scene as dynamic_epp evidence; if a Hogwarts data point is
wanted, capture a CPU-bound gameplay scene instead.

## 5. Shader compilation — none

- No shader-compile worker threads exist in the trace (no `ShaderCompilingThread`,
  no dxvk/vkd3d pipeline-compile workers among 60+ named threads; full list checked).
  Fossilize/precache has evidently done its job for the menu.
- The only sustained non-render workers are `BinkAsy0/1` (Bink video decode, 3.3 s CPU
  combined — the animated menu background) and they do not correlate with hitches
  (hitches are GPU-exec, §2).
- Present-interval spikes: p99 = 44.4 ms = a single vblank slip, 4.1% of frames; all
  explained by long gfx jobs. No compile-burst signature.

## 6. Submission / fence / TTM — all clean

- **dma_fence_wait (kernel-side blocked time): 11,943 waits totalling 19.0 ms over 30 s
  (0.06%), max single wait 0.17 ms.** Nothing is stuck on fences in the kernel.
- **`amdgpu_bo_move`: 0 events** (amdgpu ftrace category was active — `amdgpu_cs_ioctl`
  9,258 events present). No TTM/UMA eviction traffic at all; texture streaming is quiescent
  in the menu.
- drm_sched dispatch latency is irrelevant to fps here: with the ring 99.94% occupied,
  every submitted job queues behind GPU execution, never behind the scheduler.
- 9,258 `amdgpu_cs_ioctl` ≈ 9,258 `drm_sched_job` ≈ 9,257 `drm_run_job`: 1:1, no queuing
  anomaly.

## What WOULD move this scene (out of kernel scope, or different lane)

1. **GPU-side work reduction** — userspace (RADV/vkd3d-proton/game settings). Kernel can't help.
2. **GPU clock / power split**: the only kernel-adjacent lever is the SMU power algorithm
   (amdgpu `pm/swsmu/smu11` Van Gogh STAPM tuning) — giving the GPU an even larger share
   or a higher gfxclk cap. That is firmware-policy territory, high blast radius,
   and the SMU already appears to be doing the right thing (CPU floored near 1.4 GHz).
   Note: no GPU-frequency counter in this trace — capture `power/gpu_frequency` (or
   `amdgpu_smu` sysfs polling) next time to confirm gfxclk is at its cap before even
   considering this.
3. dynamic_epp validation on Hogwarts: use a CPU-bound gameplay scene, not the menu.

## Bottom line

| Question | Answer |
|---|---|
| Stable scene for A/B? | Yes — p5–p95 spread of 0.38 ms around 33.3 ms, vblank-locked |
| GPU busy | 99.94%, zero submission gaps → execution-bound |
| CPU thread critical path | None; max runnable-wait 1.0%; package 34% busy |
| CPU freq | ~1.45 GHz sustained all-core (41% of fmax) — SMU power sharing, not EPP mis-governing |
| Shader comp stutter | Absent |
| Fence/TTM/submission | 19 ms fence-block total; 0 bo_moves; dispatch not a factor |
| Distinct kernel-addressable bottleneck? | **NO** |
| dynamic_epp duplicate? | **No** — freq is low but sustained + power-budget-limited; menu scene unsuitable as dynamic_epp evidence |
| SIS/SMT duplicate? | No — runnable-wait negligible |
