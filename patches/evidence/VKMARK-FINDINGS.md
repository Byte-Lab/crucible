# vkmark GPU-Submission Pipeline Analysis — Steam Deck (Van Gogh, kernel 6.16.12-neptune)

Trace: `/tmp/claude-1000/vkmark-shading.pftrace` — 14.894s, 142,798 CS submissions on ring
`gfx_0.0.0` (one job per frame, ~9,588 fps, mean frame interval 104.3µs). All stages joined
per-job via `sched_job_id` (cs_ioctl ↔ drm_sched_job ↔ drm_run_job) and fence seqno
(entity finished-fence ctx 1534 ↔ amdgpu HW fence ctx 1, constant offset 1,694,083 —
verified by index-pairing with 0 causality violations; an off-by-one offset was detected
and corrected during analysis).

## Headline verdict

**The workload is GPU-execution-bound (94.4% GPU busy) with a co-saturated submitting
thread (vkmark uses 96.3% of one core). There is NO strong kernel-addressable
GPU-submission bottleneck. `drm-sched-wq-highpri` (WQ_HIGHPRI) would NOT help — the
drm-sched workers experience essentially zero runqueue contention.** A real but bounded
dispatch-latency cost exists (2.13% of wallclock GPU idle attributable to the drm-sched
worker wake round-trip), but its mechanism is wake/round-trip latency on *idle* CPUs, which
worker priority does not address.

## 1. Per-job pipeline latencies (n = 142,798)

| Stage | mean | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|---|
| amdgpu_cs_ioctl → drm_sched_job (submit→queue) | 0.57µs | 0.52 | 0.67 | 1.06 | 6.4 | 124 |
| drm_sched_job → drm_run_job (**queue→dispatch, worker latency**) | 11.5µs | 9.25 | 14.5 | 41.2 | 110 | 428 |
| drm_run_job → HW fence signaled (**GPU exec**) | 128.9µs | 125.4 | 166 | 216 | — | 1255 |
| HW fence → finished fence signaled (completion propagation) | 1.4µs | 1.35 | — | — | — | 191 |
| dma_fence_wait duration (app-side) | 0.83µs | 0.79 | 0.93 | 1.26 | 7.1 | 106 |

- Inter-submit interval = inter-HW-completion interval = 104.3µs mean — the GPU exactly
  keeps pace with submissions via 2-deep pipelining (per-job exec 98µs solo /
  137µs when pipelined; hw_job_count at dispatch: 2 for 79% of jobs).
- Total submit→finished-fence: 142µs mean — i.e. ~1.4 frames in flight, healthy pipelining.

## 2. drm-sched worker behavior

- `drm_run_job` executes on **unbound kworkers** (`kworker/u32:6/1/4`), combined CPU
  2.91s / 14.9s ≈ 19.5% of one core ≈ **20.3µs of worker CPU per job** (includes
  free_job work on the same wq).
- **Runqueue wait is effectively zero**: across ~254k worker scheduling instances, only
  18 "R" (runnable-waiting) states totaling 2.1ms, plus 85 "R+" preemptions totaling
  6.6ms — over the whole 15s trace. The workers always land on an idle CPU immediately
  (4c/8t leaves 6-7 idle hardware threads; vkmark pins only one).
- Therefore the 9-11µs queue→dispatch latency is the **wake round-trip itself**
  (queue_work → IPI → idle exit → context switch → worker loop → run_job), not
  scheduling competition. The p99 tail (41µs) / p99.9 (110µs) is plausibly C-state exit
  latency, but this trace has no cpuidle events to confirm (limitation — retrace with
  `cpu_idle` if pursuing).

## 3. CPU-bound vs GPU-bound split of the 104µs frame

- **GPU busy (union of dispatch→completion intervals): 14.06s / 14.89s = 94.4%.**
- vkmark thread: 14.35s CPU / 14.9s = 96.3% of one core (100.5µs CPU per 104.3µs frame),
  migrating across CPUs 0/4/5/6; **fence waits total 0.118s = 0.8%** — the app almost
  never blocks on the GPU.
- The two sides are co-saturated; the GPU is the slightly longer pole. GPU idle totals
  0.832s (5.6%), split by causality per gap (28,626 gaps, mean 29µs):
  - **3.46% of span: app late** — the next job wasn't yet queued when the GPU went idle
    (vkmark's own frame prep + pre-tracepoint CS ioctl cost; not separable here).
  - **2.13% of span (0.317s): dispatch latency** — job was already queued to drm-sched
    while the GPU sat idle waiting for the worker round-trip. **This is the entire
    theoretical upside for any dispatch-path patch: ≤ +2.25% vkmark score**, and
    realistically ~1-2% because the app thread has only ~3.7% CPU headroom to supply
    extra frames.

## 4. dma_fence

- Signal-path propagation HW-fence→finished-fence: **1.35µs p50** (IRQ context —
  `drm_sched_process_job` fires 90% in `swapper`, i.e. amdgpu IH on an idle CPU).
  No lock contention or signal-latency pathology visible at 9.6k signals/sec × 3 contexts
  (428k signals total). App fence waits are sub-µs (the app waits on fences ~32 frames
  old — always already signaled). **Nothing to patch.**

## 5. CPU frequency

- All 8 CPUs range ~1.40-2.67GHz, sample-average ~1.72-1.93GHz — mid-range, not pegged.
- On Van Gogh this is largely **STAPM shared power budget doing its job**: the GPU at 94%
  busy owns the thermal/power headroom. Boosting CPU EPP/frequency would steal GPU power
  and likely *regress* this GPU-bound workload. Not a duplicate of the dynamic_epp win
  (that was for the CPU-bound Civ6 class); here the power balance is already correct.

## 6. WQ_HIGHPRI (`drm-sched-wq-highpri`) assessment — would it raise the vkmark score?

**No.** WQ_HIGHPRI moves work items to a nice -20 worker pool; its only mechanism is
winning runqueue competition. Measured: the drm-sched workers wait in the runqueue for
~2ms out of 15s (~0.01%). There is no competition to win — 6-7 hardware threads idle at
all times. The 11.5µs dispatch latency is wake/IPI/idle-exit cost, which is identical at
any priority. Expected effect on vkmark: **~0, within noise** — consistent with the same
patch regressing on CPU-bound Civ6 (where the highpri pool adds preemption pressure
against the game threads; here it would merely be inert).

## 7. Is there any kernel patch worth trying? (honest ranking)

1. **Bypass the worker round-trip: dispatch inline from the submit context when the ring
   has credit** (`drivers/gpu/drm/scheduler/sched_main.c` / `sched_entity.c`,
   `drm_sched_entity_push_job` → currently always `drm_sched_wakeup` → wq). Upside is the
   measured 2.13% (≤ +2.25% score). This is a known, historically NAK'd upstream idea
   (locking/recursion/fairness concerns in drm_sched); a Deck-local experiment is feasible
   but it is an architectural change with real hang risk, for ≤2%. **Only worth it as an
   experiment, not as a portfolio win.**
2. **Cut wake tail latency via cpuidle**: if a retrace with `cpu_idle` events shows the
   p99 dispatch tail is deep-C-state exit, a dev-PM-QoS resume-latency cap held by amdgpu
   while the gfx ring is non-idle (`drivers/gpu/drm/amd/amdgpu/amdgpu_ring.c` +
   `cpu_latency_qos`) would trim the tail. Upper bound is a fraction of the 2.13%
   (the p50 round-trip already costs 9µs with no deep idle involved). **Marginal; also
   raises package power on an APU, which can regress GPU clocks via STAPM.**
3. **Not worth doing**: WQ_HIGHPRI (§6), dma_fence signal path (1.4µs, §4), amdgpu IH
   (completion propagation already µs-scale in IRQ), EPP/frequency forcing (§5),
   increasing `hw_submission` ring credit (queue depth at submit is 0 for 99.9% of jobs —
   the app, not the credit limit, bounds pipelining: hw_cnt at queue is 0 or 1 for 97%
   of jobs).

## Measurement protocol (if experimenting with #1/#2)

vkmark `shading=phong` headless score, ≥10 runs per side, Welch's t-test; the clean
low-noise metric. Also re-collect this same trace and re-run the gap-attribution query:
success = dispatch-attributed GPU idle share drops from 2.13% toward 0 while app-late
share stays ~constant. Any patch that doesn't move that specific number is noise-fishing.

## Bottom line

vkmark at 9.6k fps on the Deck is a *balanced saturation* workload: GPU 94.4% busy,
submit thread 96.3% of a core, kernel completion path µs-scale, worker never
runqueue-starved. The only quantified kernel-side inefficiency is the 9-11µs
drm-sched worker wake round-trip costing 2.13% of GPU time — real, but its remedy is
architectural (inline dispatch), not priority (WQ_HIGHPRI), and caps at ~2%. This
workload class does not yield a distinct high-confidence kernel patch; the Civ6
CPU-bound lane remains the productive one.
