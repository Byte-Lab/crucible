# dynamic_epp (amd-pstate per-core EPP boost) — EVIDENCE

Patch: `amd-pstate-epp-boost-clean.diff` (+ `.commitmsg`). Kernel:
`drivers/cpufreq/amd-pstate.c/.h`. Target: Steam Deck (Van Gogh APU, EEVDF,
kernel 6.16.12-valve24.4). Module param `amd_pstate.dynamic_epp` (default OFF).

## The bottleneck (why this is a win)
Full Crucible closed loop on a real game, not a synthetic:

1. **Trace** — `evidence/traces/baseline-gfx.pftrace` (perfetto, 45s, Civ VI
   graphics benchmark on the Deck). Analysis (`evidence/TRACE-FINDINGS.md`): the
   benchmark is **single-thread CPU-bound** — one render thread 98% busy, GPU
   only 3.2% busy, dma_fence waits 0.28%, EEVDF runnable-wait 0.7%. The only
   lever is that core's clock.
2. **Symptom** — per-core `scaling_cur_freq` sampling during the flythrough:
   the pegged core ran at **median 2.43 GHz, below 90% of fmax (3.5 GHz) 58% of
   the time** (bimodal 2.4/3.5), despite 98% utilization. Cause: the render
   thread's frequent short futex sleeps (waiting on GPU/other threads each
   frame) collapse amd-pstate active-mode's utilization signal, so post-wakeup
   bursts start at a low operating point and the frame-time tail inflates.
3. **Causal proof (the decisive experiment)** — EPP is a runtime sysfs knob, so
   I interleaved EPP=performance vs balance_performance blocks (n=6, no reboot,
   thermally controlled). Result: **frame-time p999 −40.7% (335→172ms, p=0.041),
   1%-low fps +16%.** This proves frequency is the causal lever for the jank
   tail (raw A/B in `evidence/ab-data/`). Average fps unchanged — the downclock
   hurts the *worst* frames (post-sleep wakeups on a slow core), not the mean.

## The patch
Per-core dynamic EPP=performance boost for recently-busy CPUs: a cpufreq
update-util hook (mirrors intel_pstate hwp_boost) samples C0 residency every
10ms; when a core is ≥50% busy it holds a 300ms decay window over which its
EPP byte in MSR_AMD_CPPC_REQ is set to AMD_CPPC_EPP_PERFORMANCE, restored to
policy EPP on decay. Edge-triggered, local-CPU-guarded, MSR-systems only,
default-off. This applies EPP=performance's clean-tail benefit *only* to busy
cores, not globally.

## Iteration story (why the final lever is EPP, not min_perf)
- v1 raised **min_perf** to nominal (2.8GHz) → reached 3.5GHz median but
  **REGRESSED frame-time p999 +21%**. (`evidence/P999-ROOTCAUSE.md`.)
- Hypothesis: MSR-write storm from the sched hook → refuted: a low-write
  variant cut writes ~50× and p999 *still* regressed +13%.
- Root cause = the **lever**: pinning min_perf ≥nominal keeps the core high even
  during micro-idle/vsync waits, which on Van Gogh's shared CPU/GPU TDP perturbs
  the SMU boost management and costs the extreme tail. EPP=performance (the
  proven-clean global lever) doesn't pin min. Switched to the EPP byte → clean.

## Measured result (final patch, interleaved n=6, Welch, thermally controlled)
| workload | metric | OFF | ON | Δ | p |
|---|---|---|---|---|---|
| Civ6 graphics | 1%-low fps | 14.20 | 18.72 | **+31.8%** | 0.014 |
| Civ6 graphics | frametime p99 | 44.25 | 42.44 | −4.1% | 0.015 |
| Civ6 graphics | frametime p999 | 75.6 | 67.1 | −11.3% | 0.29 (ns, no regression) |
| Civ6 AI       | 1%-low fps | 14.15 | 19.14 | **+35.3%** | 0.001 |
| Civ6 AI       | fps_avg | 40.16 | 40.73 | +1.4% | 0.016 |
Busy-core freq 2.43→3.50 GHz both workloads. No metric regresses on either.
Raw per-rep CSVs: `evidence/ab-data/eppb-*` (graphics), `eppm-ai-*` (AI).

## Review record (steady-state APPROVE)
2 adversarial fable rounds on the mechanism (round 1 caught a CRITICAL cross-CPU
MSR-corruption bug — update-util hooks fire remotely on shared-cache CPUs; fixed
with the smp_processor_id guard). Fresh-eyes review of the EPP variant: APPROVE.
Production-clean version (scaffolding stripped, renamed dynamic_epp): final
review APPROVE after 2 comment nits fixed. No outstanding required changes.

## Scope / honesty
- p999 is bimodal (reactive per-core trigger — inherent, not a defect; a
  post-idle-gap or scene transition can land the worst frame in an un-boosted
  window). The two significant wins (1%-low, p99) are robust.
- Default-off: stock behavior unless `amd_pstate.dynamic_epp=1`.
- Upstream: strip debugfs/counters (done in -clean), and note AMD has floated a
  separate "dynamic EPP" concept (Limonciello) — the param name may collide.
