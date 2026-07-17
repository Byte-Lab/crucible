# amd-pstate busy min_perf floor — design

Target: Steam Deck (Van Gogh APU), kernel 6.16.12 neptune (`neptune-6.16.12-valve24.4`),
`CONFIG_X86_AMD_PSTATE=y`, default mode 3 (active/EPP), governor `powersave`,
EPP `balance_performance`.
Patch: `deck/patches/amd-pstate-busy-floor.diff` (drivers/cpufreq/amd-pstate.{c,h}, +149 lines).
Compile-verified: `make LOCALVERSION= drivers/cpufreq/amd-pstate.o` — clean, no warnings.

## Problem (measured)

Civ6 graphics benchmark is single-thread CPU-bound (render thread 98% busy, GPU 3.2%).
The pegged thread's core runs at median 2.43 GHz — below 90% of fmax 58% of the time,
bimodal 56%@2.4 / 41%@3.5 — because frequent short futex sleeps (GPU/worker waits each
frame) collapse the hardware's utilization signal in autonomous EPP mode. Interleaved A/B
(n=6) EPP=performance vs balance_performance: frame-time p999 −40.7% (335→172 ms,
p=0.041), 1%-low +16%, avg fps unchanged. The downclock specifically hurts the
post-sleep-wakeup frame-time tail.

## Code-path findings (what the driver actually does in active mode)

1. **There is NO periodic kernel update path in active/EPP mode.** `amd_pstate_epp_driver`
   is a `setpolicy` driver (`amd-pstate.c` ~line 1690 pre-patch): no `.target`, no
   `.fast_switch`, and `epp_cpu_init` explicitly sets `current_pstate_driver->adjust_perf
   = NULL`. `MSR_AMD_CPPC_REQ` is written **only** on limit/policy changes
   (`amd_pstate_epp_update_limit()` → `amd_pstate_update_perf(min_limit_perf, 0,
   max_limit_perf, epp)`), EPP sysfs stores (`msr_set_epp`), suspend/offline (reset to
   BIOS min), and resume. Between those events, hardware autonomously picks the operating
   point in [min_perf, max_perf] biased by EPP, with des_perf = 0.
   **Consequence: the hysteresis needs its own trigger — the hardware never "re-reads"
   anything from the kernel; the kernel must write the MSR again. The trigger used is a
   scheduler `cpufreq_add_update_util_hook()` (see below).**

2. **min_perf is the correct and only available lever.** In active mode the kernel sets
   exactly {min, max, epp}; des_perf is 0. And the driver itself already uses min_perf
   elevation as its "performance" mechanism: `amd_pstate_update_min_max_limit()` sets
   `min_limit_perf = min(nominal_perf, max_limit_perf)` when the policy is PERFORMANCE.
   This patch applies that same floor *dynamically*, only while a CPU is recently busy.

3. **Upstream prior art:** intel_pstate `hwp_boost` does precisely this shape — a
   per-CPU `update_util` hook in HWP autonomous mode that raises the HWP min
   (`intel_pstate_hwp_boost_up`, local `wrmsrq(MSR_HWP_REQUEST)` from scheduler context)
   and decays it after `hwp_boost_hold_time_ns` (3 ms) of no trigger. Critical
   difference: hwp_boost triggers on `SCHED_CPUFREQ_IOWAIT`, and **futex/GPU-fence waits
   never set the iowait flag** — an iowait trigger would never fire for the Civ6 render
   thread. This design substitutes a C0-residency trigger.

## Mechanism (candidate A, chosen)

Module parameter `amd_pstate.busy_floor` (bool, 0644, **default off** — runtime-togglable,
which gives free interleaved A/B on the Deck without reboots).

Per-CPU, registered in `amd_pstate_epp_cpu_init()` (active mode only) and only when
`cpu_feature_enabled(X86_FEATURE_CPPC)` (MSR systems; Van Gogh reports highest_perf=255
from CAP1, i.e. the MSR interface):

- `cpufreq_add_update_util_hook(cpu, ...)` — the scheduler invokes it from
  enqueue/tick/load-update paths on that CPU; on a busy CPU at HZ=1000 that is ≥1 kHz.
- The hook rate-limits itself to one sample per **10 ms** (`BUSY_FLOOR_SAMPLE_NS`,
  same constant as `INTEL_PSTATE_SAMPLING_INTERVAL`), then reuses the driver's existing
  `amd_pstate_sample()` (APERF/MPERF/TSC deltas; unused by the EPP driver otherwise, so
  no conflict) and computes `busy = Δmperf * 100 / Δtsc`. MPERF counts only in C0 while
  TSC always counts, so this is exact C0 residency of the window — futex sleeps show up
  as idle time, sustained render work shows up as busy, regardless of what the
  utilization/CPPC signal thinks.
- If `busy ≥ 70%` (`BUSY_FLOOR_BUSY_PCT`), refresh `floor_last_busy = now`.
- Floor state = `now − floor_last_busy < 100 ms` (`BUSY_FLOOR_DECAY_NS`).
- While active, **every 10 ms sample re-asserts** the floor:
  `min_perf = clamp(nominal_perf, min_limit_perf, max_limit_perf)` RMW'd into a copy of
  `cppc_req_cached`, written with a local `wrmsrq(MSR_AMD_CPPC_REQ)`. The cached value is
  **never** modified (intel hwp_boost's exact discipline): all other MSR writers keep
  operating on the pure policy value and simply clobber the floor; the next sample heals
  it. On decay (or runtime disable, or `cpudata->suspended`), the hook writes back
  `cppc_req_cached` verbatim.
- Teardown: `cpufreq_remove_update_util_hook()` + `synchronize_rcu()` in
  `amd_pstate_epp_cpu_exit()` before `kfree(cpudata)` (intel_pstate pattern), so
  active→passive mode switches and driver unregister are safe.

The patch also drops a duplicated no-op `current_pstate_driver->adjust_perf = NULL;`
line in `epp_cpu_init` (Valve-tree artifact) at the insertion point.

## Why it is self-limiting

- The floor engages only when the last 10 ms window was ≥70% C0 — i.e. the core is
  *already* burning power executing. A genuinely idle core never refreshes
  `floor_last_busy`; 100 ms later the floor is gone. A core in deep C-states doesn't pay
  for a raised min at all (clock-gated), and its first sample after wake spans the idle
  period → busy% low → floor drops immediately.
- Light periodic loads (media playback, desktop) sit well under 70% C0 per 10 ms window
  and never trigger.
- The floor is `nominal_perf` — the *sustained* performance level, not boost — and is
  clamped into [min_limit_perf, max_limit_perf], so user/TDP frequency caps (common on
  Deck via the performance overlay) always win.
- Under the PERFORMANCE policy the floor is a no-op (min_limit_perf is already nominal).

## Expected effect on the p999 tail

The 335 ms p999 events are post-futex-wake frames executed while the core saggs to
≤2.4 GHz. During the benchmark the render core's C0 residency is ~98% at frame scale,
so the floor is continuously held (a 70%-busy 10 ms window recurs at least once per
frame; the 100 ms decay tolerates ~3 missed frames even at 30 fps). Every post-wake
burst therefore starts at ≥ nominal frequency instead of ramping from ~2.4 GHz.
Expected: a large fraction of the −40.7% p999 / +16% 1%-low that EPP=performance
delivered, with avg fps unchanged (it already was). Honest caveat: nominal < fmax
(3.5 GHz); EPP=performance additionally biases the *boost* decision, so if the tail
requires instant 3.5 GHz (not just ≥nominal) this floor recovers only part of the win.
That is measurable on-device: `busy_floor` is runtime-togglable, so the same interleaved
A/B protocol (n≥6) applies directly, and the floor's residency can be confirmed via
`turbostat`/`amd_pstate_epp_perf` tracepoint (min field of CPPC_REQ).

## Power cost reasoning

- Zero cost while disabled (one branch per 10 ms per CPU; the hook itself is a pointer
  call + one u64 compare per scheduler util update).
- When enabled: 2 rdmsr + rdtsc per 10 ms per CPU (~100 ns), one wrmsr per 10 ms per
  *busy* CPU. Negligible.
- Energy cost is confined to CPUs that were ≥70% busy in the last 100 ms: they run
  low-intensity post-sleep stretches at nominal V/f instead of a lower point. Unlike
  EPP=performance this never touches idle cores, never engages during light workloads,
  and expires within 100 ms of the workload stopping — it is the surgical subset of the
  sledgehammer that the A/B proved effective.

## Tunables (compile-time constants)

| Constant | Value | Rationale |
|---|---|---|
| `AMD_PSTATE_BUSY_FLOOR_SAMPLE_NS` | 10 ms | intel_pstate sampling interval; ≥ several ticks at HZ=1000; keeps MSR/rdmsr traffic trivial |
| `AMD_PSTATE_BUSY_FLOOR_BUSY_PCT` | 70% | comfortably below the 98%-pegged render thread, comfortably above bursty desktop/video C0 residency |
| `AMD_PSTATE_BUSY_FLOOR_DECAY_NS` | 100 ms | ≫ worst frame period (50 ms @ 20 fps) so intra-frame sleeps never drop the floor; short enough that a stopped game decays in ~0.1 s |
| floor level | `nominal_perf` | driver's own PERFORMANCE-policy floor; sustainable by definition, no thermal-budget theft |
| `busy_floor` param | default off | upstream-safe default; SteamOS enables via `amd_pstate.busy_floor=1`; 0644 allows runtime A/B |

## Risks / unknowns (honest)

1. **KEY OPEN RISK — does raising min_perf bind on Van Gogh's SMU during these sags?**
   CPPC min_perf is a *hint* the platform "should" honor absent thermal/power limits.
   The A/B evidence proves the EPP field changes SMU behavior; it does not prove the
   min field does. If Van Gogh's firmware weights min_perf weakly (or the 2.4 GHz
   plateau is an infrastructure/fabric DPM state rather than a core-CPPC decision), the
   floor could deliver less than EPP=performance did. Must be verified on-device:
   toggle `busy_floor`, read the effective frequency histogram of the pegged core.
2. **Trigger delivery**: update_util hooks fire only when the scheduler runs on that
   CPU. A fully idle CPU gets no callbacks — handled (stale floor is harmless while
   clock-gated and is corrected on the first sample after wake), but a CPU running a
   pure-idle-injection or nohz_full setup would sample rarely. Not the Deck profile.
3. **Clobber windows**: EPP sysfs stores, `epp_update_limit`, suspend and offline write
   the MSR from `cppc_req_cached` and momentarily drop the floor (≤10 ms until
   re-assert). The suspend path sets `cpudata->suspended` *after* its MSR reset, so one
   hook tick in that window could briefly re-raise min; `epp_resume`/set_policy rewrite
   the MSR unconditionally, so it cannot persist. Accepted, documented in-code.
4. **First-sample garbage**: the first window after registration spans boot (prev == 0),
   so busy% equals boot-average C0 — worst case one spurious 100 ms floor at init.
   Harmless; not worth extra code.
5. **shmem CPPC systems are excluded** (`X86_FEATURE_CPPC` gate): the PCC-mailbox write
   path can sleep and cannot run from scheduler context. If Van Gogh turned out to be
   shmem-based, this patch would be inert there — evidence (CAP1 highest_perf=255 and
   `cppc` cpuinfo flag on Zen2 client) says it is MSR-based; verify once on-device
   (`grep cppc /proc/cpuinfo` and absence of "shared memory" in the amd-pstate dmesg
   debug line).
6. **Upstream positioning**: default-off, active-mode-only, one module param, mirrors
   the accepted hwp_boost design. The predictable review asks: "why not EPP autonomy?"
   (answer: measured futex-sleep signal collapse, iowait boost can't see futexes) and
   "make the threshold/decay tunables sysfs" (defer until asked).
