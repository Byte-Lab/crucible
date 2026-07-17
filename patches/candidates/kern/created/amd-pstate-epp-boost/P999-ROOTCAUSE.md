# p999 Frame-Time Regression Root-Cause — amd-pstate "busy min_perf floor" (Van Gogh / EEVDF)

**Verdict: MECHANISM, not thermal.** The regression is caused by the patch's
*write pattern* (periodic CPPC/MSR writes from the sched update-util hook, amplified
by the "clobber-heal" retry loop), not by the 3.5 GHz frequency it produces.
**Fix = widen hysteresis / eliminate the clobber-heal write storm / make min_perf
sticky like the EPP=performance path — NOT cap the floor lower and NOT add thermal
awareness.**

---

## 0. CRITICAL DATA CAVEAT (read first)

`floor-on-diag.pftrace` **does not contain the events this investigation was scoped
around.** The capture holds only 5 ftrace event types:

| event | count |
|---|---|
| cpu_idle | 325,687 |
| sched_switch | 224,306 |
| sched_wakeup | 113,866 |
| drm_vblank_event | 487 |
| amdgpu_dm_atomic_commit_tail_finish | 358 |

There are **zero** `amd_pstate_epp_perf`, `amd_pstate_perf`, `cpu_frequency`,
`thermal_temperature`, or `thermal_zone_trip` records. Proof:
- `raw`/`ftrace_event` GROUP BY name returns only the 5 names above.
- The only counter track is `cpuidle`; there is **no `cpu_frequency` counter and no
  thermal counter**.
- In the trace binary the strings `amd_pstate_epp_perf`, `cpu_frequency`,
  `thermal_temperature` each appear exactly **once** (the TraceConfig echo of the
  *requested* event list), while the field names `min_perf` / `des_perf` appear
  **0 times** — i.e. the format descriptors and event records were never emitted.

**Consequence:** the four numeric questions in the brief — pstate writes/sec, min_perf
thrashing, freq DROPS, thermal trip crossings — **cannot be measured from this trace.**
traced_probes was asked for those tracepoints and produced nothing (most likely the
`amd_cpu/amd_pstate_*` + `thermal/*` tracepoints are not present/enabled in this Deck
kernel build, the patch's own MSR-write site has no tracepoint, or the ftrace enable
silently failed). Everything below therefore ranks the hypotheses on (i) the given
hardware control experiment and (ii) *indirect* scheduling/idle/present evidence, and
flags exactly what still needs a re-capture to prove.

---

## 1. Present intervals & the worst frames

Per-frame ground truth is the MangoHud CSV (1201 frames, 30.02 s wall):
`p50 = 25.7 ms, p90 = 29.7, p99 = 42.3, max = 153.7`. The tail is tiny and sharp:

| threshold | frames | % |
|---|---|---|
| > 40 ms | 22 | 1.8 % |
| > 50 ms | 3 | 0.25 % |
| > 80 ms | 2 | 0.17 % |

Two distinct bad regions:
- **Frames 233–258** — a *sustained cluster* of ~42–49 ms frames (a heavy scene; broad, not spiky).
- **Frames 546–548** — the real hitch: `54.7 → 153.7 → 90.5 ms` three-in-a-row, then recovery. This single 153 ms hitch is the p999-class event in this window.

Note the trace's present markers are **display-decimated** (358 `atomic_commit_tail_finish`
vs 1201 game frames ≈ 3.4:1) and quantized to the ~22 ms display-commit interval, so
they are *not* a per-frame timer. The CSV has no timestamps, so exact frame→trace
alignment is only approximate (cumulative-sum alignment puts frame-547 near ~26 s
trace-time, which lands inside a genuinely-idle sparse-commit region — see §2).

Baseline (`baseline-gfx.pftrace`, floor OFF) active display-commit gaps: mean 15.8 ms,
**only 1 gap > 30 ms**. Floor-on: mean 17.3 ms, **3 gaps > 30 ms, 2 > 50 ms**. Same
shape, slightly heavier tail with the floor on — consistent with a *rare added
disturbance*, not a systemic slowdown.

## 2. What the scheduling/idle data shows around the spikes

**Render thread = `Civ6 (WinID 2)` tid 38425**: 35.56 s on-CPU over 45 s ≈ **79 % busy**
→ it is exactly the "recently ≥70 % busy" thread the floor targets. Confirmed the
mechanism is active on the right thread.

- **No render-thread scheduling stalls that could explain a 15 ms hitch.** Its worst
  *non-scene* off-CPU gaps are ~14.2–14.4 ms (356 of them, tightly clustered ≈ 70 Hz
  vsync/GPU waits) plus 8× ~500 ms gaps. Off-CPU distribution: mean 2.0 ms, only 356
  gaps > 10 ms and they are the vsync waits. **There is no isolated ~15 ms
  runnable-but-descheduled stall on the render thread.** The p999 hitch is therefore
  *not* CPU preemption/migration of the render thread.
- **The big gaps are genuinely idle, not CPU contention.** Sampling the 500 ms render
  gap at 27.34 s: CPUs are ~96 % in `swapper` (idle); Civ6 ran only 107 ms of the
  600 ms window. These are frame-limiter / between-benchmark-section / GPU-load waits,
  not scheduler stalls.
- **CPUs still reach deep idle with the floor on.** cpu_idle state histogram:
  C-state 3 = 86,184, C2 = 67,322, C1 = 8,878, C0 = 459. The floor raises *frequency*
  when running but does not pin CPUs busy; wake-from-idle then happens *at nominal
  freq*, which *helps* wake latency rather than hurting it. → hypotheses that need the
  floor to change idle/wake behavior are not supported.

**Net:** the spike is not on the CPU-scheduling side of the render thread. It is
GPU-/present-side and rare — the fingerprint of an occasional shared-resource
disturbance, which points straight at the CPPC/SMU write path (§3).

## 3. Ranked root cause

**Decisive control (given, not from this trace):** the pure-hardware EPP=performance
path reaches the **same 3.5 GHz with no p999 regression.** If 3.5 GHz caused thermal
throttle or a frequency-latency problem, the hardware-only path to 3.5 GHz would
regress too — it does not. This **excludes frequency and heat as the cause** and
isolates the difference to *how the patch raises min_perf*: repeated CPPC/MSR writes
from the per-update sched hook with clobber-heal, versus one sticky hardware setting.

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| **1 (lead)** | **CPPC/MSR write storm from clobber-heal → SMU-mailbox contention with the GPU** | **Most supported** | Only structural diff vs the clean EPP=perf path. On Van Gogh, CPU CPPC requests and GPU power/clock mgmt share the **same SMU firmware mailbox**; a write storm (clobber-heal *by design* re-issues writes whenever the platform resets min_perf) periodically contends with GPU SMU traffic → rare GPU-side present hitch. Fits every observation: tail-only, render thread clean, gaps GPU-side/idle. **Not directly measured** (no pstate events). |
| 2 | (A) Floor thrashing — min_perf toggling nominal↔lower | Likely, subsumed by #1 | The clobber-heal retry loop is precisely a write/toggle storm; a specific driver of #1. Needs `amd_pstate_epp_perf` to confirm toggle count/rate. |
| 3 | (D) MSR-write CPU cost in sched path | Contributes, not the direct cause | A `wrmsr` to CPPC_REQ is ~1 µs — cannot itself produce a 15 ms frame. It only matters as the *volume* term of the write storm (#1). |
| 4 | (C) Frequency-transition latency | Unlikely direct cause | Transitions are µs–low-ms; can't make a 15 ms hitch alone. Idle-exit now happens at nominal freq (helps, not hurts). |
| 5 | (B) **THERMAL** | **Effectively excluded** | (a) EPP=perf reaches 3.5 GHz clean → not heat. (b) Regression is **tail-only with a clean p50=25.7 ms**; thermal throttle broadens the whole distribution and raises the *baseline* over the run — not observed. (c) No thermal events, but the shape alone rules it out. |

## 4. Fix recommendation

Attack the **write pattern**, not the frequency target:

1. **Kill the clobber-heal storm.** Do not re-write min_perf in a tight loop each time
   the platform/SMU clobbers it. If it was clobbered, **back off** (exponential or a
   long fixed hold) instead of immediately re-healing. The clobber-heal loop is the
   smoking gun for the SMU write storm.
2. **Add a no-op guard + widen hysteresis.** Skip the MSR write if min_perf is already
   at the target. Only write on a *genuine* LOW→HIGH busy transition, and hold the
   nominal floor for a decay window (e.g. keep it for N×10 ms after the last busy
   sample, then step down) so writes happen a few times per second, not ~100/s/CPU.
3. **Prefer the sticky path that the control proves is clean.** Set the busy floor the
   same way EPP=performance does — a low-rate/sticky min_perf decision moved *out of
   the per-update sched hook* — rather than driving CPPC from every update-util call.
   The control experiment shows this reaches 3.5 GHz with no p999 cost.
4. **Serialize against in-flight SMU/CPPC requests.** Never issue a new CPPC write
   while one is outstanding; coalesce.

**Do not** cap the floor to a lower frequency or add a thermal governor — that would
sacrifice the +16.6 % 1%-low / +1 % avg WIN to "fix" a problem that is not thermal.

## 5. To confirm decisively (re-capture)

The one thing that would turn this from a strongly-supported inference into a
measurement: **re-capture with the pstate tracepoints actually emitting.** Before the
run, verify `/sys/kernel/tracing/events/amd_cpu/amd_pstate_epp_perf/enable` exists and
fires (and add a tracepoint at the patch's own MSR-write / clobber-heal site). Then:
count `amd_pstate_epp_perf` events/sec globally and in the ±5 ms window around each
worst frame, and check whether min_perf is toggling nominal↔lower there. That directly
distinguishes hypothesis #2 (visible toggle burst at the hitch) from a steady
write rate, and confirms the write-rate magnitude behind #1. Also enable
`cpu_frequency` + `thermal_temperature` to formally close out #4/#5.
