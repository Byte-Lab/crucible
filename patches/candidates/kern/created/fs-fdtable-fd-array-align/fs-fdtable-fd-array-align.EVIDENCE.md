# fs-fdtable-fd-array-align - evidence package (2026-07-16)

## Patch
fs-fdtable-fd-array-align.diff: ____cacheline_aligned_in_smp on
files_struct::fd_array. Size 704 unchanged (alignment replaces tail padding;
holds on any config since sizeof(fd_array) is a multiple of any plausible
SMP_CACHE_BYTES). pahole verified: fd_array 160 -> 192, own cachelines.

## Mechanism (reviewer-verified, fs/file.c line refs from 7.2-rc3+)
- Read: __fget_files_rcu (fs/file.c:1017) reads fdt->fd[fd]; pre-expansion
  fdt->fd == fd_array (dup_fd fs/file.c:405, init_files :535). Slots 0-3
  (64-bit) sat on the written line (file_lock@128).
- Write: alloc_fd (:569) + file_close_fd_locked (:714) write file_lock,
  next_fd, open_fds_init/full_fds_bits_init per open/close.
- Expansion trigger = fd NUMBER >= max_fds, not file count; post-expansion
  processes unaffected (separate kvmalloc array).

## Measurements (interleaved A/B, alternating boots, vng 32 vCPU, 7950X host idle,
## Welch; kernel identity per boot via /proc/version md5)
fdbench (scratchpad/fdbench.c; readers pread fd3, churners dup2(fd,60)/close):
- r8c8:  stock 9.45M -> patched 12.77M reader ops/s: +35.1% t=28.2 p<1e-4 (n=24/side)
- r24c8: 19.51M -> 21.13M: +8.3% t=9.4 p<1e-4
- r8c0 control: -2.65% p=0.27 ns (stockCV 8.4%)
- churn cost on stock: r8c8 vs r8c0 = -55% reader throughput
Regression gates (all ns): open1_threads -0.87% p=0.17 (n=24/side),
open1_processes -0.37% p=0.47, dup1_threads +0.50% p=0.23,
dup1_processes -0.32% p=0.31, poll2_threads +0.53% p=0.43,
pread1_threads +0.61% p=0.16 (n=18/side).
Boot-level (correct clustering unit) recheck by reviewer r2: r8c8 t=33.6
(n=8 boots/side), r24c8 t=8.3 - both still p<1e-4.
Raw logs: /tmp/fd-ab-results.log, /tmp/fd-ab2-results.log.

## perf c2c (host bare metal, unpatched 7.0 kernel, AMD IBS, fdbench 8r8c 15s)
Top shared line = 37.5% of all load HITMs (3325/8866), resolves to
files_struct+128: offset 0x0 file_lock (native_queued_spin_lock_slowpath),
0x8/0x10/0x18 bitmap words written from do_dup2 / file_close_fd_locked.
Report: /tmp/c2c-fdbench-report.txt, data /tmp/c2c-fdbench.data.
Note: reader-side loads (fd_array slot +0x38) are sample-starved vs spinning
churners in IBS attribution; reader benefit shown by the throughput A/B.
Patched-side c2c not possible in VM (no IBS in KVM guest, verified).

## Review trail
r1 REVISE (mechanism confirmed end-to-end; checkpatch * fix, comment
expansion-condition + 32-bit corrections, evidence demands) - all applied.
r2 REVISE-trivial (commit msg next_fd wording, fdbench comments, boot-level
stats - verified stronger: r8c8 t=33.6 n=8 boots/side) - all applied +
memcached datapoint added.
r3 fresh-fable FINAL: all numbers independently recomputed from raw logs and
verified; one vestigial fdbench comment fix required then APPROVE - applied.
STATUS: APPROVED steady state. Ready for user to post (rebase onto current
mm/vfs tree first; Cc Christian Brauner, Al Viro, Mateusz Guzik, linux-fsdevel).

## Real-workload datapoint (memcached, done 2026-07-16 14:44)
memcached -t 4 (13 fds, pre-expansion) + memtier 4x2 conns,
--reconnect-interval=50 (connection churn), 20s x 3 reps x 8 pairs
interleaved: +0.27% p=0.75 NEUTRAL (n=24/side, stockCV 3.4%).
Interpretation: per-request network+hash work dwarfs one line miss at this
churn rate; patch is a no-regression for this shape. Raw: /tmp/fd-ab3-results.log.

## Honest scope/caveats
- Only pre-expansion fdtables (< NR_OPEN_DEFAULT highest-fd). Big-fd-count
  servers unaffected in both directions.
- Single-machine (Zen4) numbers; microbench is adversarially constructed by
  design (demonstrator), gates + memcached are the safety story.
- Real-workload result is neutral, not a win: the sell is zero-cost +
  restores the documented read/write split + protects churn-heavy shapes.
