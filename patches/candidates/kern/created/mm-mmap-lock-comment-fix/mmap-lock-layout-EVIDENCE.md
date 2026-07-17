# mmap_lock layout investigation - SCRAPPED with negative result (2026-07-16)

## Hypothesis (from static pahole scan)
Commit 8cea569ca785 ("sched/mmcid: Use proper data structures", tglx, Nov 2025)
inserted the 192B cacheline-aligned mm_cid before mmap_lock; sched_cache stats
added another aligned 64B. This broke the documented invariant from
2e3025434a6b ("mm: relocate 'write_protect_seq' in struct mm_struct", Feng
Tang 2021, citing Linus): mmap_lock at offset 56 so rwsem 'count' and 'owner'
sit in different cachelines. Breaking it was measured in 2021 as -9.2% on
will-it-scale mmap1. Current mainline: mmap_lock at offset 16, count+owner
same line, sharing with pgtables_bytes/map_count/page_table_lock; dead
56-byte hole after mm_users. Expectation: restoring the split recovers
throughput.

## Method
- Tree: mainline 7.2.0-rc3+ (37e2f878a7a6), x86-64, SCHED_MM_CID=y,
  SCHED_CACHE=y, PER_VMA_LOCK=y.
- Interleaved A/B, alternating boots (stock/patched per pair), 8 pairs per
  round, 3 reps per boot, will-it-scale -t 32 in vng (32 vCPU, host 7950X,
  host idle). Welch t-test. Kernel identity verified per boot via
  /proc/version md5. Stock CV 2-3.4%.

## Results (mmap1_threads ops/s, patched vs stock; page_fault1 neutral in all rounds)
| Variant | Layout | delta | p |
|---|---|---|---|
| A full restore | offset 56 + fault fields to CL2 hole + vm counters before lock | -3.04% | 0.001 |
| B split only | offset 56, count-line co-tenancy identical to stock | -1.66% | 0.03 |
| C dedicated line | mmap_lock ____cacheline_aligned, count+owner together, only quiet fields in line | -0.05% | 0.94 |

n=24/side each round. Diffs: mm-mmap-lock-cacheline-restore.diff (A),
variantC.diff (C); B was A minus co-tenancy changes (reconstructable: insert
mmlist+hiwater_rss+hiwater_vm+locked_vm before pgtables_bytes).
Raw: /tmp/wis-ab-varA.log, /tmp/wis-ab-varB.log, /tmp/wis-ab-results.log (C).

## Interpretation
On Zen4 + modern rwsem, the count/owner cacheline split is a PESSIMIZATION
(~1.7%): OSQ serializes optimistic spinning to a single owner-poller, so the
2021 many-spinners-poll-owner rationale no longer applies, and splitting makes
every acquire touch two lines instead of one. Co-tenant fields written only by
the lock holder (map_count, pgtables_bytes, total_vm...) are FREE - the holder
already owns the line exclusively. C == stock confirms co-tenancy costs
nothing; B < stock isolates the split cost.

## Honest upstream options (not yet submitted)
1. Comment fix: the mm_types.h "offset 56 is very optimal" comment is stale
   and actively misleading (following it = -1.7% on this HW). A patch
   removing/correcting it, with this data, prevents future bad "restorations".
   Ideally cross-check on Intel before sending (2021 data was Intel 0day).
2. Variant D (optional, unmeasured): pack the 56-byte hole with quiet fields
   WITHOUT touching anything at/after mmap_lock -> guaranteed perf-identical,
   mm_struct -64B. Trivial cleanup value only.

## Lessons
- Never trust a documented layout invariant without re-measuring on current
  hardware; comments encode dead microarchitectures.
- The adversarial-review + interleaved-measurement discipline worked exactly
  as designed: reviewer approved the mechanics, measurement killed the premise.

## Follow-up deliverable (2026-07-16 17:15): mm-mmap-lock-comment-fix
Comment-only patch replacing the stale offset-56 lore with durable placement
guidance (lock-free-hot fields off the line; same-lock-writer fields measured
neutral; re-measure before re-tuning). Review trail: r1 REVISE (6 fixes:
holder-owns-line falsehood, OSQ narrative, Cc lineage, checkpatch citation
format, caveat into body); r2 fresh-fable verified every number/commit/
mechanism claim independently (incl. git ancestry: 617f3ef95177 NOT ancestor
of bisected 57efa1fe5957) - REVISE on one checkable-false rwsem sentence
(pre-617f spin was also OSQ-gated), "fix that one sentence and ready to post".
Fixed as directed: mechanism now framed as every-acquire-writes-count+owner
(two lines per acquire) with 617f as example-not-cause + ancestry fact.
STATUS: APPROVED (conditional fix applied). Files:
mm-mmap-lock-comment-fix.{diff,commitmsg}.
