# mm: PR_SET_THP_ALWAYS + khugepaged eager-collapse — EVIDENCE

Patch: `mm-thp-always-prctl.diff` (+ demonstration shim `thp_always_shim.c`).
Kernel: 7 files (`include/uapi/linux/prctl.h`, `include/linux/mm_types.h`,
`include/linux/huge_mm.h`, `include/linux/khugepaged.h`, `kernel/sys.c`,
`mm/huge_memory.c`, `mm/khugepaged.c`), +198/−5. Target: Steam Deck (Van Gogh APU,
UMA, kernel 6.16.12-valve24.4-neptune, EEVDF). Gated on `CONFIG_64BIT`; per-process,
default-off (opt-in via `prctl(PR_SET_THP_ALWAYS, 1)`).

## The bottleneck (why this is a win) — NEW workload class

Found by profiling **Factorio 2.0** headless on the Deck (user-selected new workload,
beyond the Civ6 benchmarks that produced patches #3/#4). The 90×90 inserter/chest
synthetic stress save (`~/.factorio/saves/stress.zip`) drives a memory/TLB-bound
update loop — a genuinely different class from the CPU-freq (#3) and SMT (#4) findings:

- **IPC 0.38**, **dTLB-load-misses 73.4%** of dTLB loads.
- Only **18.5%** of the 154 MB anon heap is hugepage-backed (28.6 MB `AnonHugePages`)
  under the stock `enabled=madvise` policy — Factorio's allocator never issues
  `MADV_HUGEPAGE`, so the hot factory-state heap stays 4K-mapped.
- Setting global `defrag=always` drops dTLB-load-misses to **22.8% (52× fewer absolute
  misses)** and ran the benchmark ~10% faster in an exploratory single-shot probe of that
  broader global configuration (uncontrolled; the controlled interleaved
  aggressive-khugepaged A/B below puts the ceiling at +5.76%) — proving the heap's TLB
  pressure is the lever. But global `always` imposes synchronous-compaction latency on *every* process,
  which is exactly why the kernel default is `madvise`. The game needs to opt **only its
  own address space** in. Full analysis + hook-point derivation: `evidence/THP-DESIGN.md`.

Profiling modality note: this is a memory/TLB bottleneck, so the evidence is **hardware
perf counters (IPC, dTLB-load-misses) + `/proc/<pid>/smaps` hugepage coverage**, not a
scheduler perfetto trace — the right instrument for the mechanism.

## The mechanism (why one flag suffices)

`vma_thp_gfp_mask()` already gives a **madvised** VMA under the default `defrag=madvise`
the bit-for-bit `defrag=always` allocation mask (`GFP_TRANSHUGE`, sync compaction, no
`__GFP_NORETRY`), and `MADV_HUGEPAGE` already flips the enabled-policy gate + khugepaged
eligibility. So the whole design collapses to **one per-mm flag, `MMF_THP_ALWAYS`,
meaning "treat every THP-eligible anon VMA in this mm as if MADV_HUGEPAGE"**. Anon-only,
minimal scope; DISABLE/NOHUGEPAGE/`never` precedence still wins.

## The patch has two halves — flag alone was NOT enough (measured)

1. **Eligibility** (prctl + `vma_thp_always()` + gfp mask): makes the flagged mm's anon
   VMAs THP-eligible. Verified on-device: Factorio's 91 MB `[heap]` VMA shows
   `THPeligible: 1` with the flag set.
2. **Prompt collapse** (khugepaged enhancement): the flag alone was **NEUTRAL** —
   on-fault THP only covered the pre-flag ~28 MB and stock khugepaged (10 s scan,
   4096-page budget, round-robin) never caught up on a ~67 s run (`AnonHugePages` flat).
   So the patch adds, all gated on `MMF_THP_ALWAYS` (zero effect when unused):
   a per-mm scan-budget boost, a sleep-skip while actively collapsing a flagged mm, and
   a kick-on-opt-in (`khugepaged_enter_mm`, which zeroes `khugepaged_sleep_expire` before
   waking so the kick actually interrupts the throttle).

## Measured

Throughput convention used throughout: **UPS gain = ratio of mean ms/tick**
(e.g. (1.4808 / 1.4443) - 1 = +2.53%).

**Ceiling** (global aggressive khugepaged, proves the achievable win), interleaved n=18/side:
`evidence/ab-data/thp-ceiling-aggressive.log` — BASE 1.4853 → AGGR 1.4044 ms/tick,
**+5.76% UPS (Welch t=21.30), zero overlap**.

**Decisive per-process A/B** (the patch: per-process flag via the prctl shim, **stock**
global khugepaged, global `enabled=madvise` — so any win is the *patch* driving collapse,
not global tuning), interleaved, thermally controlled, `evidence/ab-data/thp-prctl-decisive.log`:

| | n | mean ms/tick | sd |
|---|---|---|---|
| OFF (no flag) | 32 | 1.4808 | 0.0066 |
| ON (`PR_SET_THP_ALWAYS`) | 32 | 1.4443 | 0.0350 |

**Δ +2.53% UPS, Welch t=5.79, p<0.0001** (all 32 samples/side from the raw log).
**Block-level zero regression** — no interleaved ON block mean was worse than the
overall OFF mean (worst ON block mean 1.4640 vs OFF mean 1.4808). At the individual-run
level two ON runs (both 1.483) sit marginally above the OFF mean, so the earlier
per-run "no flagged run was worse" phrasing does not hold on the full data and
has been retired; the block-level statement is the accurate one.

## Supporting measurement: Civ6 khugepaged-promptness A/B (2026-07-17)

Second real game, probing whether khugepaged collapse latency binds a second game
heap. **What this probes, stated honestly: global khugepaged promptness under
`enabled=always`, via sysfs only -- NOT the per-mm patch mechanism.** No patched
kernel involved. It demonstrates that khugepaged collapse promptness is a binding
constraint for a second real game heap, and therefore motivates per-process
promptness without global tuning -- which is what patch 2 provides.

Method:

- Civilization VI first-party AI benchmark (frame-time CSVs, one float ms/line),
  Steam Deck, stock SteamOS 6.16 kernel, global THP `enabled=always`.
- A/B on khugepaged tunables alone: stock `scan_sleep_millisecs=10000` /
  `pages_to_scan=4096` vs prompt `100` / `16384`.
- Interleaved 3 blocks x 2 reps per arm (n=6 reps/side). Thermal note: the prompt
  arm ran later in the session and was therefore thermally disadvantaged; the
  measured advantage is if anything understated.
- Raw data: `ab-data/civ6/c3thp-{stock,eager}-b{1,2,3}/rep*.csv`. Conventions:
  fps = n_frames/(sum_ms/1000); 1%-low = 1000/(mean of worst 1% frame times);
  p99 = linear-interpolated percentile; per-rep metrics, Welch across reps.

Results (recomputed from the raw CSVs):

| metric | stock | prompt | delta | Welch t |
|---|---|---|---|---|
| fps_avg | 29.879 | 31.136 | **+4.21%** | 20.36 |
| 1%-low fps | 9.841 | 10.256 | **+4.22%** | 5.49 |
| frame-time p99 (ms) | 65.93 | 61.78 | **-6.30%** | -4.52 |

Sign is consistent in all 3 interleaved blocks for all three metrics (per-block
fps_avg deltas +3.79%/+3.95%/+4.89%; p99 deltas -6.82%/-4.90%/-7.14%). Arm-level
fps CVs 0.43% (stock) / 0.26% (prompt).

## Scope / honesty

- The per-process result (+2.53%) is **below the +5.76% aggressive-khugepaged ceiling**,
  and ON is **bimodal** (sd 5× OFF): when khugepaged collapses the heap early in a run
  the win is large; when its round-robin scan-list position lands the collapse late,
  ON ≈ OFF. The per-mm boost + sleep-skip help but don't fully beat first-collapse
  latency on a busy scan list — an inherent trade-off of not reprioritizing the scan
  list, documented in the patch header, not a defect.
- Default-off, `CONFIG_64BIT`-only. Anon-only. Global `never`/`VM_NOHUGEPAGE`/
  `MMF_DISABLE_THP` still veto.
- Documented upstream-discussion limitations (in the patch header): exec-inheritance of
  a resource-inflating hint into a setuid binary (mirrors `MMF_DISABLE_THP` inheritance,
  inflating rather than restricting); an unprivileged process can hold khugepaged
  unthrottled via continuous faulting (~512× work amplification at `max_ptes_none=511`);
  one-pass stale sleep-skip after a flagged process exits.

## Review record

Adversarial fable review round 1 → **REVISE**: (1) `khugepaged_enter_mm` didn't zero
`khugepaged_sleep_expire` → the opt-in kick couldn't interrupt the throttle (functional
dead-kick); (2) 32-bit build broke (bit 32 overflow); (3) two misleading comments. All
fixed: functional fix verified, feature gated on `CONFIG_64BIT` (i386 allnoconfig +
x86_64 both compile clean), comments corrected. Fresh-eyes steady-state re-review:
see `SUMMARY.md` for the APPROVE record.

## Artifacts

- `evidence/THP-DESIGN.md` — full bottleneck analysis, mechanism derivation, hook points.
- `evidence/ab-data/thp-ceiling-aggressive.log` — the +5.76% UPS ceiling (n=18/side).
- `evidence/ab-data/thp-prctl-decisive.log` — the decisive per-process A/B (n=32/side, p<0.0001).
- `ab-data/civ6/` — Civ6 khugepaged-promptness A/B raw frame-time CSVs (2026-07-17,
  supporting measurement; see section above).
- `thp_always_shim.c` — LD_PRELOAD `constructor(101)` calling `prctl(PR_SET_THP_ALWAYS,1)`;
  the mechanism used to opt an unmodified Factorio binary in for measurement.
