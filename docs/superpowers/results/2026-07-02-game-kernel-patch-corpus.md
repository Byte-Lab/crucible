# Crucible — Game-mode kernel-patch corpus (2026-07-02)

Raw material for the **gaming-on-Linux microconference (Oct 2026)** talk.

## What ran

10 full end-to-end optimization loops, fully autonomous, on the passthrough
AMD Radeon RX 7900 XT (RADV/NAVI31):

1. Benchmark (vkmark, Vulkan, 6 samples) for a baseline.
2. Re-run under a Perfetto kernel-scheduler trace.
3. Analyzer mines the trace (PerfettoSQL) → names a kernel bottleneck + source area.
4. Optimizer writes a **real kernel source patch** to that area.
5. `KernelBuilder` applies + rebuilds the kernel (auto-reverts on build failure).
6. VM **reboots** with the patched kernel (GPU passthrough intact).
7. Re-measure (6 samples) → Welch t-test + Cohen's d per metric → verdict.

Config: `~/.crucible/game-grind.toml` (mode=game, vkmark, `allowed_layers=["kernel"]`,
`runs_per_phase=6`). Diffs: `linux/.crucible_patches/`.

**Infra milestone:** 0 vfio-busy across all 10 reboots — the shutdown-drain fix
(`VmManager::wait_for_process_group_exit`) held. This was the last blocker to
running the kernel-patch loop on a real GPU.

## Corpus (10 patches, 8 subsystems)

`*` = statistically significant (Welch p<0.05). Metrics: fps_avg / fps_p1 (1% lows) /
frame_time_p99 (lower is better). Single measurement run per phase — see caveats.

| # | Patch | Subsystem | fps_avg | fps_p1 | ft_p99 |
|---|-------|-----------|--------:|-------:|-------:|
| 6 | **sched-fair-sis-util-idle-floor** | CFS `select_idle_cpu` | **+6.5%*** | **+19%*** | **-15%*** |
| 2 | **drm-sched-wq-highpri** | DRM scheduler wq | +2.2 | **+58%*** | -21 |
| 12 | cpuidle-menu-typical-interval-fallback | cpuidle menu governor | +2.1 | +6.5 | -6.6 |
| 9 | amdgpu-ih-skip-empty-wake-up-all | amdgpu interrupt handler | +1.9 | -2.4 | -11 |
| 8 | wq-select-unbound-cpu-avoid-hardirq-local | workqueue | +0.3 | +1.7 | -2.6 |
| 3 | sched-fair-immediate-resched-kthread-wakeup | CFS | -0.6 | -1.8 | -4.4 |
| 11 | softirq-ksoftirqd-short-slice | softirq | +1.3 | -4.7 | +0.1 |
| 7 | rcu-read-unlock-special-hardirq-fastpath | RCU | -4.6 | -12.5 | +9 |
| 10 | rcu-core-defer-reraise-need-resched | RCU | -6.9 | -6.7 | +0.5 |
| 1 | drm-atomic-helper-fence-fastpath | DRM atomic commit | -2.0 | -21 | +94 |

**2 significant wins**; some patches regressed — the loop proposes, the data
rejects. That mixed outcome is itself part of the story (autonomous, measured,
self-correcting).

## The two winners (candidate slide patches)

### Cycle 6 — `sched-fair-sis-util-idle-floor` (strongest: all three metrics)
`kernel/sched/fair.c`, `select_idle_cpu`: when `nr_idle_scan` reads the LLC as
overloaded, mainline bails out of the idle-CPU search entirely (`return -1`) and
stacks the waking task on a busy CPU. The patch instead probes up to
`min(4, span_weight)` CPUs for a genuinely idle sibling. Rationale (from the
trace): `nr_idle_scan` is a laggy average refreshed only on periodic load
balance; a bursty GPU-submission workload keeps a few CPUs pinned by
IRQ/softirq/kworker while siblings idle, so the stale "overloaded" reading makes
every wakeup skip the scan → render/submit threads pile onto busy CPUs → wakeup
latency → late frames. Result: fps_avg +6.5%, fps_p1 +19%, frame_time_p99 -15%.

### Cycle 2 — `drm-sched-wq-highpri` (frame consistency)
`drivers/gpu/drm/scheduler/sched_main.c`: add `WQ_HIGHPRI` to the DRM scheduler's
ordered workqueue (job submit/free is on the per-frame critical path). Result:
fps_p1 (1% lows) +58%.

## Caveats — READ before putting numbers on slides

- **Single measurement run per phase.** Each verdict compares one baseline
  distribution (6 samples) to one comparison distribution (6 samples), both from
  a single boot. vkmark's fps_p1 was noisy (CV up to ~43% on some baselines), so
  the *magnitudes* (especially the +58%) are inflated by baseline variance /
  regression-to-mean. The *significance* is real; the *effect size* is not yet
  trustworthy.
- **Workload is vkmark, not a shipping game.** Real games stress these paths
  differently. See the Aspyr/real-games plan.
- **Not vetted for upstream.** These are plausible, profile-grounded candidates,
  not reviewed patches. e.g. cycle 6 re-introduces scan cost the `return -1`
  deliberately avoids under genuine overload — needs broad benchmarks + a
  maintainer discussion.

## TODO — nail effect size for the slides

- [ ] **Confirmation runs** for cycles 2 & 6: apply each patch, run the
      baseline-vs-comparison measurement **≥5 independent times** (fresh
      reboots), report mean delta ± CI. Kills the regression-to-mean doubt.
- [ ] **More samples / longer runs per phase** to shrink vkmark CV: bump
      `runs_per_phase` to ~15 and `benchmark_duration_secs` to ~30; discard 2
      warmups.
- [ ] **A/B/A** ordering (baseline → patched → baseline) to detect drift/thermal.
- [ ] Re-measure the two winners on a **real game** once Aspyr/RADV is fixed —
      the number that matters for the talk is a game FPS delta.
- [ ] Optional: hand-review + minimize the two winners into clean, submittable
      diffs; check against latest mainline `select_idle_cpu` / drm-sched.
- [ ] Capture Perfetto before/after screenshots for the two winners (the trace
      that motivated the patch → the trace showing the stall gone).

## Reproduce
```bash
# GPU bound to vfio-pci first: scripts/setup-host.sh 03:00.0 --bind ; memlock unlimited
PATH="$PWD/.venv/bin:$PATH" ./target/release/crucible-orchestrator \
  --config ~/.crucible/game-grind.toml --max-cycles 10
# diffs land in linux/.crucible_patches/ ; results in ~/.crucible/gamegrind.db
```
