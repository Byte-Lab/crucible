---
title: "fs: put files_struct::fd_array on its own cacheline"
slug: fs-fdtable-fd-array-align
class: kern
state: created
tier: TIER_1
created: 2026-07-16
target: linux-fsdevel
suggested_cc: "Christian Brauner, Al Viro, Mateusz Guzik, Linus Torvalds (optional)"
base: "mainline 7.2-rc3+ (37e2f878a7a6); rebase onto vfs.git before sending"
review_rounds: 3
review_status: "fresh-fable APPROVE steady state"
---

## Summary
fd_array slots 0-3 share the ____cacheline_aligned_in_smp "written part"
line of files_struct (file_lock, next_fd, fd bitmaps), violating the
struct's own 2005 read/write-split comment. Until the fdtable is first
expanded, fdt->fd points at fd_array, so __fget_files_rcu() reads a slot
there on every fd-taking syscall; any sibling thread's open()/close()
bounces the line. Fix: one alignment attribute; size 704 unchanged on
every config (512 % any SMP_CACHE_BYTES == 0).

## Key numbers (interleaved A/B, alternating boots, vng 32 vCPU on 7950X)
- Contended microbench (8 pread readers vs 8 dup2/close churn threads):
  +35.1% reader ops/s, boot-level Welch t=33.6, p<1e-4, n=8 boots/side
- 24 readers / 8 churners: +8.3%, t=8.3
- Uncontended control: ns. 6 will-it-scale fd-path gates: all ns (p>0.15)
- memcached + memtier connection churn (real-workload sanity): +0.27%
  p=0.75 neutral - honestly reported, the sell is zero-cost + big
  targeted win + restored documented invariant
- Host perf c2c (unpatched): hottest shared line of the run = 37.5% of
  all load HITMs = files_struct+128, writers do_dup2/file_close_fd_locked

## Review trajectory
r1 adversarial: REVISE - mechanism CONFIRMED end-to-end (fs/file.c line
refs in EVIDENCE); required checkpatch fix, expansion-condition comment
correction, 32-bit scope fix, evidence demands. r2 fresh reviewer:
REVISE-trivial - recomputed all stats from raw logs incl. boot-level
clustering (stronger), caught stale next_fd wording. r3 fresh reviewer:
verified every number/citation independently; one vestigial bench
comment fix, then APPROVE. Full trail: fs-fdtable-fd-array-align.EVIDENCE.md
(Review trail section). Raw agent transcripts were session-ephemeral;
EVIDENCE carries the round-by-round summaries.

## Prep before sending
- Rebase onto current vfs.git / mainline; rerun checkpatch
- Optional: lore.kernel.org search "fd_array cacheline" for prior art
  (r1 reviewer found none, re-check at send time)
- Include fdbench.c as reproducer in cover letter

## Artifacts (this directory)
diff, commitmsg (SoB included), EVIDENCE.md, fdbench.c (reproducer),
fd-ab-microbench.log / fd-ab-gates.log / fd-ab-memcached.log (raw A/B),
fd-c2c-host-report.txt (perf c2c). Raw perf data: /tmp/c2c-fdbench.data
(host, volatile).
