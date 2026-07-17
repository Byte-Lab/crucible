---
title: "mm: PR_SET_THP_ALWAYS prctl + khugepaged eager-collapse"
slug: mm-thp-always-prctl
class: kern
state: created
tier: TIER_1
created: 2026-07-12
target: linux-mm
suggested_cc: "David Hildenbrand, Usama Arif, Johannes Weiner, Barry Song, Andrew Morton"
base: "v7.2-rc3 (37e2f878a7a6), branch crucible/thp-always-prctl in ~/upstream/k1-thp; mm-new rebase planned for non-RFC posting"
review_rounds: 4
review_status: "fresh-fable APPROVE for RFC posting (round 4); numbers recomputed clean incl block-clustering (t=5.99), anon-gate shmem-leak fix verified, UAPI disjointness proven; pending: setuid-inheritance RFC-open-question + mm-new rebase + selftests for non-RFC"
---

## Summary
Per-mm MMF_THP_ALWAYS flag treating the mm's anon VMAs as
MADV_HUGEPAGE, PLUS khugepaged promptness (kick-on-opt-in, per-mm
scan-budget boost, sleep-skip-while-collapsing) so the flagged heap
collapses promptly - flag alone measured neutral because stock
khugepaged is too slow. Default-off, CONFIG_64BIT-gated, anon-only.
Use case: launcher opts in a third-party game binary that cannot
madvise itself (Factorio: update loop memory/TLB-bound, IPC 0.38,
dTLB-miss 73%, only 18.5% of 154MB anon heap hugepage-backed under
enabled=madvise).

After lore review the interface was reshaped from a new PR_SET_THP_ALWAYS
prctl number to a flag in the existing PR_SET_THP_DISABLE flags family:
commit 9dc21bbd62ed ("prctl: extend PR_SET_THP_DISABLE to optionally
exclude VM_HUGEPAGE") occupies this exact space, enumerates our use case
as its scenario (4), and states "we're not willing to add completely new
toggles" - so the series adds the enable-side flag PR_THP_ALWAYS_ADVISED
to that interface instead of colliding with it. Two-patch split: patch 1
= UAPI flag + eligibility/GFP + inheritance + docs; patch 2 = khugepaged
promptness.

## Key numbers (Deck, interleaved A/B; UPS gain = ratio of mean ms/tick)
- Factorio +2.53% UPS (ms/tick 1.4808 -> 1.4443; Welch t=5.79, p<0.0001,
  n=32/side interleaved) with stock global khugepaged - proves the patch
  drives collapse. Block-level zero regression: no interleaved ON block
  mean was worse than the overall OFF mean (worst ON block 1.4640 vs
  OFF mean 1.4808).
- Aggressive-khugepaged ceiling +5.76% UPS (ms/tick 1.4853 -> 1.4044,
  n=18/side, t=21.30); gap explained by documented bimodal
  first-collapse latency (no scan-list reprioritisation).
- Provenance: measured on 6.16.12-valve24.4 (Deck) carrying an
  equivalent backport with the prototype prctl spelling and pre-mTHP
  khugepaged; the mainline port is build-tested, not yet re-measured.
- Civ6 supporting measurement (2026-07-17, stock SteamOS 6.16 kernel,
  enabled=always, khugepaged promptness via sysfs only -- probes the
  constraint, not the patch mechanism): fps_avg +4.21% (Welch t=20.4),
  1%-low +4.22%, frame-time p99 -6.30%, same sign in all 3 interleaved
  blocks, prompt arm thermally disadvantaged. Raw: ab-data/civ6/.

## State of the work
- Commit messages written: full series in series-v1/ (cover + 2 patches,
  git format-patch --rfc output). Drafts commitmsg-{1,2,cover}-draft in
  this directory match the commits.
- Mainline port done: applies on v7.2-rc3 (base 37e2f878a7a6), commits
  f3245dfbba11 (prctl flag) + 08049c5d1f6b (khugepaged promptness).
  Ported to mm_flags_t bitmap API, explicit mm_init() inheritance, and
  mTHP-era khugepaged.
- Review round 2 = REVISE, applied in full: mm_types.h limitation
  comment split so patch 1 no longer references patch-2 code (only the
  exec/setuid inheritance note stays at the flag; sleep-skip, stale-pass
  and scan-list-order notes moved to mm/khugepaged.c in patch 2);
  disable path clears MMF_THP_ALWAYS before setting disable modes
  (GET states airtight-disjoint); all claims recomputed from the raw
  logs over all 32 samples/side and the per-run zero-regression claim
  replaced with the block-level statement; cover gained RSS/footprint
  paragraph, selftests commitment, accurate LWN citation
  (https://lwn.net/Articles/1072538/), precise remote-MADV_HUGEPAGE
  gap statement (future VMAs, not persistence), lkml Cc and base note.
- Round 3 fixes applied (this round): (1) vma_thp_always() gained the
  vma_is_anonymous() guard so the gfp half no longer leaks
  madvised-strength allocation effort to shmem/swap-in faults --
  anon-gated at the helper so both consumers (eligibility, gfp) are
  covered, with a commit-message sentence added; (2) the 64-bit-only
  rationale restated honestly everywhere (policy choice, no identified
  32-bit use case, untested there, -EINVAL relaxable later without
  breaking UAPI -- the "matches the original UAPI" phrasing dropped);
  (3) motivation "~10% faster" identified as an exploratory
  uncontrolled single-shot probe of a broader global config, vs the
  controlled interleaved +5.76% ceiling, consistently across cover,
  patch 1, and EVIDENCE.md; (4) Civ6 supporting measurement added to
  cover + EVIDENCE.md.
- Builds: x86_64 kernel/sys.o + mm/ clean at patch 1 and at HEAD
  (re-run after the round 3 anon-guard change; i386 PAE was built
  clean in round 1). checkpatch: 0 errors, 1 warning per patch
  (pre-existing From/SoB email mismatch mainfault.com vs manifault.com
  - resolve before sending).

## Why TIER_2 not TIER_1
New UAPI (prctl flag) = high bar, and the pipeline is not finished:
- Fresh adversarial APPROVE pass on the revised series still pending
  (round 3 fixes applied; discipline rule 1 requires a fresh APPROVE).
- mm-new rebase pending (RFC applies on v7.2-rc3).
- Selftests (tools/testing/selftests/mm/prctl_thp_disable.c extension)
  promised for non-RFC, not yet written.

## Review trajectory
Round 1 (Deck backport): REVISE (dead-kick sleep_expire, 32-bit break,
comment drift) -> fixed -> fresh-fable steady-state APPROVE.
Round 2 (mainline port + reshape): REVISE (comment placement, claim
conventions, cover gaps) -> applied.
Round 3: REVISE (shmem gfp leak through vma_thp_gfp_mask -- anon guard
added to vma_thp_always(); dishonest 64-bit rationale restated;
~10%-vs-+5.76% reconciled) -> applied this round, plus Civ6 supporting
evidence. Next: fresh skeptical reviewer on series-v1.
Trail in mm-thp-always-prctl.EVIDENCE.md; design doc THP-DESIGN.md
(this directory).

## Artifacts (this directory)
series-v1/ (RFC cover + 2 patches), mm-thp-always-prctl-mainline-7.2.diff
(pre-split port), mm-thp-always-prctl.diff (Deck backport), EVIDENCE.md,
thp_always_shim.c (LD_PRELOAD shim used for measurement), THP-DESIGN.md,
raw A/B: ab-data/ (factorio-*, thp-*).
