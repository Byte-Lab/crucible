# PR_SET_THP_ALWAYS — per-process opt-in to aggressive THP

Target tree: `/home/void/upstream/crucible_kernel_2` (6.16.12-valve24.4 "neptune", Steam Deck / Van Gogh, x86_64).
Patch: `/home/void/upstream/crucible/deck/patches/mm-thp-always-prctl.diff` (5 files, compile-verified).

## Motivating evidence (measured, Factorio 2.0 headless on the Deck)

The update loop is memory-latency bound: IPC 0.38, dTLB-load-misses 73.4%, with only
18.5% of its 154 MB anon heap hugepage-backed (28.6 MB AnonHugePages) under the stock
`enabled=madvise` policy. Global `defrag=always` drops dTLB-load-misses to 22.8%
(52x fewer absolute misses) and makes the benchmark ~10% faster — but it is global and
imposes synchronous-compaction latency on every process, which is exactly why the
kernel default is `defrag=madvise`. The game needs to opt only its own address space in.

## Research result: no existing mechanism (patch is warranted)

- `include/uapi/linux/prctl.h:180-181` — only `PR_SET_THP_DISABLE`/`PR_GET_THP_DISABLE`
  exist, as plain booleans. The `PR_THP_DISABLE_EXCEPT_ADVISED` flags rework is post-6.16
  mainline work and is **not** in this tree.
- `include/linux/mm_types.h:1758` — the only per-mm THP flag is `MMF_DISABLE_THP`.
  There is no "force/always" counterpart.
- `MADV_COLLAPSE` (incl. via `process_madvise`) is one-shot: it collapses what exists
  now but does nothing for future faults of a growing heap.
- `MADV_HUGEPAGE` can't be applied externally to *future* mappings; a shim in the
  process could wrap every allocator call, but that's fragile and per-allocation.

## The key mechanism insight (why one flag is enough)

`mm/huge_memory.c:1280 vma_thp_gfp_mask()`:

- `defrag=always`   → `GFP_TRANSHUGE | (vma_madvised ? 0 : __GFP_NORETRY)`
- `defrag=madvise`  → `GFP_TRANSHUGE_LIGHT | (vma_madvised ? __GFP_DIRECT_RECLAIM : 0)`

and `GFP_TRANSHUGE == GFP_TRANSHUGE_LIGHT | __GFP_DIRECT_RECLAIM` (include/linux/gfp_types.h).
So a **madvised VMA under the default `defrag=madvise` already gets bit-for-bit the
`defrag=always` allocation mask** (synchronous compaction, and without `__GFP_NORETRY`).
MADV_HUGEPAGE likewise flips the enabled-policy gate and khugepaged eligibility.

Therefore the whole design collapses to: **one per-mm flag, `MMF_THP_ALWAYS`, meaning
"treat every THP-eligible anonymous VMA in this mm as if MADV_HUGEPAGE"**. No separate
defrag override is needed; requirement (a) hugepage-allowed-under-madvise and
(b) defrag=always-GFP both fall out of the two existing `vma_madvised` predicates.

## Exact hook points (file:line, pre-patch)

| Site | What changes |
|---|---|
| `include/uapi/linux/prctl.h:374` | `PR_SET_THP_ALWAYS 79`, `PR_GET_THP_ALWAYS 80` (78 = `PR_FUTEX_HASH` was the highest). |
| `include/linux/mm_types.h:1780` | `MMF_THP_ALWAYS 32` + `MMF_THP_ALWAYS_MASK (1UL << 32)`; added to `MMF_INIT_MASK`. |
| `include/linux/huge_mm.h:191` | new inline `vma_thp_always(vma)` → `vma->vm_mm && test_bit(MMF_THP_ALWAYS, &vma->vm_mm->flags)` (NULL-guard matches the vdso/gate-vma case that `__thp_vma_allowable_orders` handles at `mm/huge_memory.c:122`). |
| `include/linux/huge_mm.h:290-299` (`thp_vma_allowable_orders` sysfs fast path) | `hugepage_hinted = (vm_flags & VM_HUGEPAGE) \|\| vma_thp_always(vma)`; `hinted` now selects `huge_anon_orders_madvise` and, with `hugepage_global_enabled()` (madvise counts as enabled, `huge_mm.h:180`), `huge_anon_orders_inherit`. This is the **allowable-orders** change: PMD/mTHP faults become permitted under global `enabled=madvise`. |
| `mm/huge_memory.c:1280-1282` (`vma_thp_gfp_mask`) | `vma_madvised` now also true when `vma_thp_always(vma)`. This is the **GFP** change: `__GFP_DIRECT_RECLAIM` (synchronous compaction) under `defrag=madvise`/`defer+madvise`, and no `__GFP_NORETRY` under `defrag=always`. Reached from `mm/huge_memory.c:1161` (`do_huge_pmd_anonymous_page`), `mm/memory.c:4436` (`alloc_anon_folio`, mTHP), `mm/memory.c:4985`, and the shmem fault paths. |
| `kernel/sys.c:2661` | prctl get/set cases, exact mirror of `PR_SET_THP_DISABLE` (same `mmap_write_lock_killable` discipline, same strict-args `-EINVAL`). Plus `BUILD_BUG_ON(MMF_THP_ALWAYS >= BITS_PER_LONG)` so a 32-bit build fails loudly instead of corrupting the field next to `mm->flags`. |

**Not touched (deliberately):** file-backed VMA gating in `__thp_vma_allowable_orders`
(`mm/huge_memory.c:179`), shmem allowable-orders (`shmem_allowable_huge_orders`), and
khugepaged internals. The evidence is anon-heap TLB pressure; keeping the flag
anon-only is the minimal defensible scope.

## Fork/exec inheritance

`kernel/fork.c:1057` (`mm_init`): `mm->flags = mmf_init_flags(current->mm->flags)` runs
for **both** `fork()` (dup_mm) and `execve()` (bprm mm_alloc while `current->mm` is
still the launcher's). Adding `MMF_THP_ALWAYS_MASK` to `MMF_INIT_MASK`
(`mm_types.h:1783`) therefore gives exactly the MMF_DISABLE_THP inheritance semantics:
a launcher (Steam, gamescope, or a 5-line wrapper) can `prctl(PR_SET_THP_ALWAYS, 1)`
and exec the game.

## khugepaged (existing heap, not just new faults)

No explicit registration is needed: `khugepaged_enter_vma` (`mm/khugepaged.c:472`) is
called on every `mmap_region` and on the huge-fault path, gates on `hugepage_pmd_enabled()`
(true under global `enabled=madvise`, `mm/khugepaged.c:416`) and then on
`thp_vma_allowable_order(..., TVA_ENFORCE_SYSFS, PMD_ORDER)` — which the patch extends.
So a flagged mm gets registered on its next mapping/fault and khugepaged will also
*collapse the already-faulted heap* (the launcher-then-exec case registers everything
from the first mmap). Corner case: setting the prctl on a long-idle already-running
process may not register the mm until its next mmap or PMD-aligned fault; a one-shot
`process_madvise(MADV_COLLAPSE)` covers that if it ever matters.

## Interaction with PR_SET_THP_DISABLE (disable wins, structurally)

`vma_thp_disabled()` (`include/linux/huge_mm.h:320`) checks `VM_NOHUGEPAGE` and
`MMF_DISABLE_THP` and is consulted at `mm/huge_memory.c:125` in
`__thp_vma_allowable_orders`, which returns **0 orders** before the madvise-mask logic
ever matters; if no THP is faulted, the GFP-mask change is unreachable. So:
- `PR_SET_THP_DISABLE` set ⇒ no THP, regardless of `MMF_THP_ALWAYS`. Disable wins.
- Per-VMA `MADV_NOHUGEPAGE` ⇒ that VMA stays THP-free. App-level opt-outs are honoured.
- The two prctls are orthogonal bits; no cross-validation in the handler (matches the
  precedent that DISABLE is evaluated, not enforced, at set time).

## No-op guarantee for unflagged processes

Every behavioural change is behind `test_bit(MMF_THP_ALWAYS, ...)`, which is false for
every existing mm (bit 32 was never set, `MMF_INIT_MASK` only propagates it once set).
Cost when unset: one `test_bit` on an mm-hot cacheline in two slow paths that already
do multiple `test_bit`s on `transparent_hugepage_flags`. Zero policy change.

## Demonstrating it on Factorio without modifying the game

`MMF_INIT_MASK` inheritance means either an exec-wrapper or an LD_PRELOAD constructor
works. The LD_PRELOAD shim (runs before the game's `main` and before its allocators
create the big heap arenas):

```c
/* thp_always_shim.c — LD_PRELOAD shim: opt this process into aggressive THP.
 *
 * Build:  gcc -shared -fPIC -O2 -o libthpalways.so thp_always_shim.c
 * Use:    LD_PRELOAD=/path/libthpalways.so ./factorio --benchmark saves/bench.zip ...
 *
 * Verify: grep AnonHugePages /proc/$(pgrep factorio)/smaps_rollup   (should approach heap size)
 *         awk '/THPeligible/' /proc/$(pgrep factorio)/smaps | sort | uniq -c
 *         perf stat -e dTLB-loads,dTLB-load-misses -p $(pgrep factorio) -- sleep 30
 *         cat /sys/kernel/mm/transparent_hugepage/enabled   (stays [madvise] — proof it's per-process)
 */
#define _GNU_SOURCE
#include <sys/prctl.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

#ifndef PR_SET_THP_ALWAYS
#define PR_SET_THP_ALWAYS 79
#endif
#ifndef PR_GET_THP_ALWAYS
#define PR_GET_THP_ALWAYS 80
#endif

__attribute__((constructor(101)))
static void thp_always_init(void)
{
	if (prctl(PR_SET_THP_ALWAYS, 1, 0, 0, 0) != 0) {
		fprintf(stderr, "[thp-shim] PR_SET_THP_ALWAYS failed: %s "
			"(kernel without the patch?)\n", strerror(errno));
		return;
	}
	fprintf(stderr, "[thp-shim] pid %d: aggressive THP enabled (PR_GET_THP_ALWAYS=%d)\n",
		getpid(), (int)prctl(PR_GET_THP_ALWAYS, 0, 0, 0, 0));
}
```

A copy ready to build sits at `/home/void/upstream/crucible/deck/patches/thp_always_shim.c`
(compile-verified on the host). Equivalent launcher wrapper, since the flag survives exec:
`prctl(PR_SET_THP_ALWAYS, 1, 0, 0, 0); execvp(argv[1], &argv[1]);`.

Measurement protocol (mirrors the original evidence): run the Factorio benchmark
(a) stock, (b) with the shim, (c) global `defrag=always` as the reference ceiling;
compare benchmark time, `dTLB-load-misses` %, and `AnonHugePages`. Expect (b) ≈ (c)
for the game while any concurrent process keeps stock behaviour — that concurrent-
process check (e.g. `sysbench` memory latency percentiles alongside) is what proves
the point of the patch vs the global knob.

## Biggest risk / most likely maintainer objection

**Maintainer objection (the one that will actually be raised): this adds a one-off
boolean UAPI knob in exactly the space where upstream mm is converging on a general
per-process THP *policy* interface.** Post-6.16 mainline turned `PR_SET_THP_DISABLE`
into a flags-based interface (`PR_THP_DISABLE_EXCEPT_ADVISED`), and there is active
work on per-process THP policy / BPF-driven THP decisions. Reviewers (Hildenbrand,
Rientjes et al.) will say a new `PR_SET_THP_ALWAYS` boolean hardcodes
"madvise-everything" into UAPI forever and should instead be a flag/mode of the
unified policy interface. The honest upstream pitch is to frame it as the symmetric
counterpart of THP_DISABLE ("`PR_THP_ADVISED_ALL`") and rebase onto the flags-based
prctl and the `mm_flags_t` bitmap (which also dissolves this backport's one real
wart: bit 32 requires 64-bit `mm->flags`, enforced here by a `BUILD_BUG_ON`).

**Biggest runtime risk: indiscriminate madvise-everything, inherited by everything.**
The flag treats *all* anon VMAs — thread stacks, sparse arenas, guard-adjacent
regions — as MADV_HUGEPAGE, so (1) hugepage faults on sparse mappings inflate RSS
(real concern on a 16 GB Deck; MADV_HUGEPAGE is deliberately targeted today), and
(2) because it survives fork+exec, a game that spawns helpers (wine services, shader
compilers, crash handlers) silently drags them into synchronous-compaction stalls.
Mitigation: set the flag as late as possible in the launch chain (the shim, not
Steam itself), and the child can always clear it (`prctl(PR_SET_THP_ALWAYS, 0)`) or
per-region `MADV_NOHUGEPAGE`, both of which are honoured by design.
