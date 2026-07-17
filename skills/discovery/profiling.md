# Discovery: finding a bottleneck worth patching

Crucible is a general-purpose kernel optimizer -- optimizations are fair
game anywhere (mm, block, KVM, net, locking, sched, fs). Hunt for the
highest-value, cleanest, most-defensible upstream change, not the one
nearest the last patch.

## Toolkit (use the full set, not just perfetto)

Perfetto is scheduler/trace-centric; most subsystems need other
instruments:

- perf c2c -- cache-to-cache transfers / false sharing (HITM
  cachelines). THE tool for contended-cacheline layout patches.
  Needs a full PMU: run on bare metal, not in a VM (KVM does not
  virtualize IBS; on the host AMD IBS works).
- pahole -- struct layout: holes, padding, hot fields crossing
  cachelines. Reads /sys/kernel/btf/vmlinux.
- perf stat topdown -- frontend/backend/cache/TLB/branch bound.
- perf record/report + flamegraphs -- hotspots.
- perf lock / lock contention -b (BPF) -- lock contention.
- perf mem -- access patterns. perf sched -- runqueue latency.
- bpftrace/bcc -- targeted dynamic probes.
- perfetto -- scheduler timelines, wakeup chains, per-frame stalls
  (the VM loop's analyzer path).

## Prototype-first (standing rule; two dead patches taught it)

Before building any layout/perf patch, run a cheap targeted probe
proving the claimed cost exists NOW, on the target microarchitecture:
perf c2c on the workload, or a reader-vs-writer microbench on the
struct. Static-scan plausibility is not evidence. fd_array had a probe
and won; mmap_lock offset-56 and rq shared-tags did not and died after
full build+measure cycles.

Corollary: distrust documented perf lore. Layout/optimization comments
encode dead microarchitectures; re-measure before "restoring" any
documented optimum (the mmap_lock offset-56 "rule" REGRESSED on Zen4).

## Workflow for false-sharing/layout candidates

1. Highly-parallel workload on the host (32T surfaces contention)
   under `perf c2c record -a`.
2. `perf c2c report` -> HITM cachelines + offsets + symbols.
3. Map offsets to struct+field with pahole.
4. Verify the layout still holds in the mainline tree you would
   actually patch.
5. Prototype probe (see above), then patch, build, boot in a vng VM,
   re-measure the workload metric.
6. Adversarial review (validation/adversarial-review.md) before the
   patch counts.

## Where prior findings live

Search patches/evidence/ and patches/negative-results/ before
re-deriving a bottleneck -- killed ideas and no-patch findings are
recorded exactly so they are not re-litigated.
