# Winner validation: subsystem regression benchmarks

When a grind cycle produces a **high-confidence winner** (Welch-significant
improvement on a primary game metric, no significant regression), the patch
gets a second validation pass with the *standard upstream benchmarks for the
subsystem it touches* before an evidence package is considered
submission-ready. Game numbers alone don't survive LKML review; the first
question is always "what does this do to <canonical benchmark>?".

Run these on the same guest environment as the game measurement (identical
kernel config, 8 vCPU VM) unless noted, base vs patched, ≥3 runs per side
(10+ for sub-second microbenches), Welch's t-test on the results. Include
raw numbers in the winner's `EVIDENCE.md`, regressions included — an honest
"pipe ping-pong loses 2%" with an argument is stronger upstream than
silence.

## Mapping: subsystem → benchmarks

| Subsystem touched | Benchmarks (minimum set) | What they catch |
|---|---|---|
| Scheduler wakeup/placement (`fair.c` wake path, `select_idle_sibling`, wake_affine) | `perf bench sched pipe` (sync ping-pong — the adversarial case for placement changes), `perf bench sched messaging -g 20` (hackbench), schbench (wakeup-latency percentiles), timed kernel compile `make -j$(nproc)`, `stress-ng --switch` | throughput loss from broken stacking, latency-tail shifts, load-balance interactions |
| Scheduler slice/preemption (EEVDF slice, preemption thresholds, buddy logic) | same as above, plus `schbench -L` (no-locking variant) and a desktop-interactivity proxy (the game itself) | starvation, over-preemption throughput loss |
| ksoftirqd / softirq | `perf bench sched messaging`, netperf/iperf3 UDP_RR + TCP_RR if network-adjacent, timed kernel compile | softirq starvation vs userspace throughput balance |
| RCU | rcutorture (correctness, CONFIG_RCU_TORTURE_TEST), then scheduler set above | grace-period stalls, callback latency |
| mm: readahead/reclaim/compaction/THP | `stress-ng --vm`, will-it-scale (page_fault/mmap), kernbench (cold-cache), fio buffered seq+rand read | readahead regressions, reclaim latency, fault throughput |
| Block/blk-mq | fio (4k randread/randwrite, seq, iodepth sweep, both null_blk and real virtio), timed kernel compile (I/O mix) | IOPS/latency regressions across queue depths |
| DRM/amdgpu/GPU sched | the game benchmark suite itself (all three Civ 6 modes), vkmark, glmark2 | present-path/submission regressions across API styles |
| futex/locking | `perf bench futex` (hash/wake/requeue), will-it-scale lock ones | contention-path regressions |
| cpuidle/cpufreq | schbench (idle-exit latency shows in tails), `stress-ng --switch`, plus power if measurable | wakeup-latency vs residency tradeoffs |

Reference: Mel Gorman's mmtests is the umbrella suite upstream reviewers
reach for; the table above picks the members runnable in the Crucible guest.

## Protocol

1. Winner detected (accept verdict) → archive diff + trace + agent JSONs
   into `~/.crucible/civ6-winners/<cycle>-<name>-package/`.
2. Boot the **bench rootfs** (`~/.crucible/rootfs`, has perf/stress-ng/
   schbench/toolchain; no GPU needed for non-DRM patches) via
   `~/.cache/virtme-ng/schedbench.sh` (or the subsystem's equivalent) —
   once on the base kernel, once with the patch applied. Kernel selection =
   tree state at `vng` boot time; **revert + rebuild base afterwards**.
3. Welch base-vs-patched per benchmark; append a "Subsystem regression
   suite" section to `EVIDENCE.md` with raw numbers and analysis.
4. Only then is the package submission-ready.

## Measurement calibration (accidental A/A tests)

When the optimizer declines to emit a patch, the cycle still measures
baseline vs comparison on the IDENTICAL kernel — a free A/A test of the
measurement system. Treat these as calibration gold; do not discard.

Observed 2026-07-04 (cycle 13, aibenchmark, full isolation, n=4/side):
fps_avg passed A/A (+0.3%, neutral) but fps_p1 (-3.6%) and
frame_time_p99 (+3.8%) both flagged "significant" regressions with no
kernel change — a single weak-tail run in one phase drives both, since
the two metrics are the same order statistic inverted.

Consequences for the winner bar:
- fps_avg significance at n=4 is trustworthy as measured.
- Tail-metric (fps_p1 / frame_time_p99) significance at n=4 and p<0.05
  is NOT sufficient on its own: require either p<0.01, n>=6, or
  replication in an independent cycle before counting a tail-only win.
- A tail win accompanied by a same-direction fps_avg shift is stronger
  than tail alone.
