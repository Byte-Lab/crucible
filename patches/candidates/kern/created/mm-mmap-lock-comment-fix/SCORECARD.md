---
title: "mm: replace stale mmap_lock cacheline-placement comment"
slug: mm-mmap-lock-comment-fix
class: kern
state: created
tier: TIER_1
created: 2026-07-16
target: linux-mm (akpm)
suggested_cc: "Feng Tang, Adrian Huang, Andrew Morton, Lorenzo Stoakes, David Hildenbrand, Liam Howlett, Suren Baghdasaryan, Vlastimil Babka, Waiman Long, Peter Zijlstra"
base: "mainline 7.2-rc3+ (37e2f878a7a6); rebase onto mm-unstable before sending"
review_rounds: 2
review_status: "APPROVED (r2 conditional fix applied as directed)"
---

## Summary
Comment-only patch. The mm_types.h comment above mmap_lock claims the
offset-56 count/owner cacheline split is "very optimal" and warns
against disturbing it. Measurement shows the opposite on current
kernels: following the comment regresses the exact benchmark its lore
came from. Replaces it with durable guidance (lock-free-hot fields off
the line; same-lock-writer fields measured free; re-measure before
re-tuning).

## Key numbers (will-it-scale mmap1_threads -t 32, vng 32 vCPU, 7950X,
## interleaved alternating boots, n=24/side per variant)
- Restore offset-56 split only: -1.66%, p=0.03 (REGRESSION)
- Full restoration (split + fault-field separation): -3.04%, p=0.001
- mmap_lock on fully dedicated cacheline: -0.05%, p=0.94 (null - proves
  current co-tenants cost nothing)
- page_fault1 neutral throughout (PER_VMA_LOCK bypasses mmap_lock)

## Review trajectory
Investigation first ATTEMPTED to restore the documented layout
(investigation/ subdir: restore diff + variants A/B/C + raw logs);
adversarial r1 approved mechanics, measurement killed the premise -
negative result documented in mmap-lock-layout-EVIDENCE.md. Comment-fix
patch then reviewed r1 (6 required fixes incl. a false holder-owns-line
claim and wrong OSQ narrative) and r2 fresh reviewer (independently
recomputed all stats; verified git ancestry 617f3ef95177 NOT ancestor
of 2021 bisect base 57efa1fe5957; one checkable-false rwsem sentence -
fixed as directed, "then ready to post").

## Prep before sending
- STRONGLY recommended: one Intel datapoint (rerun /tmp/wis-ab.sh-style
  A/B on an Intel box) - preempts the likely "still helps on Intel"
  reply. Body already hedges uarch-dependence honestly, so optional.
- Rebase onto mm-unstable; resolve Cc addresses from MAINTAINERS
- The commitmsg has submitter notes below the --- cutline; strip before git am

## Artifacts (this directory)
diff, commitmsg, mmap-lock-layout-EVIDENCE.md (the negative-result data
foundation), investigation/ (scrapped restore patch + variant diffs +
raw wis-ab logs for all three variants).
