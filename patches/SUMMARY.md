# Crucible patch series — Steam Deck / kernel, 2026-07-11/12 (+ mainline 07-16)

> **Files reorganized 2026-07-17.** All upstream candidates now live under
> `patches/candidates/{kern,scx}/{created,sent,merged,rejected}/<slug>/` with one
> SCORECARD.md per patch; killed patches under `patches/negative-results/`. Use
> `patches/candidates/patchctl` to browse. Paths mentioned below predate the move —
> resolve any file via `candidates/patchctl path <slug>`.

Every patch below reached **adversarial-fable-reviewer APPROVE steady-state**
(iterated author↔skeptical-reviewer until a fresh reviewer signed off with no
required changes) AND was **measured with interleaved, thermally-controlled A/B**
(no stale baselines — a hard lesson from earlier thermal-drift false positives).

## LOCKED WINS (7)

### 6. fs-fdtable-fd-array-align.diff (patches/mainline/) — MAINLINE VFS PATCH, 2026-07-16
include/linux/fdtable.h. Found by systematic pahole layout scan of mainline
7.2-rc3: files_struct::fd_array slots 0-3 share the ____cacheline_aligned
"written part" line (file_lock/next_fd/fd bitmaps), violating the struct's own
2005 read/write-split comment — __fget_files_rcu() reads a slot there on every
fd-taking syscall pre-expansion, and every other thread's open()/close()
bounces it. Fix: ____cacheline_aligned_in_smp on fd_array; size 704 unchanged
on any config. **Targeted microbench (8 pread readers vs 8 dup2/close churn
threads): +35.1% reader ops (boot-level t=33.6, p<1e-4, n=8 boots/side);
24 readers: +8.3%; 6 will-it-scale fd-path gates + memcached-with-conn-churn
all neutral; host perf c2c: 37.5% of run's HITMs on files_struct+128 with
do_dup2/file_close_fd_locked as writers.** 3 review rounds, final fresh-fable
APPROVE. Full package in deck/patches/mainline/ (diff, commitmsg with SoB,
EVIDENCE.md, fdbench.c, raw logs, c2c report).

### 7. mm-mmap-lock-comment-fix.diff (patches/mainline/) — MAINLINE MM PATCH, 2026-07-16
include/linux/mm_types.h, comment-only. Replaces the stale "offset 56 is very
optimal" mmap_lock placement lore (Feng Tang 2021 / 61dc4358d37a wording) with
durable guidance, backed by the negative-result measurement campaign below:
restoring the documented split regresses mmap1_threads -1.7% (p=0.03), full
restore -3.0% (p=0.001), dedicated line null — i.e. following the comment
today would regress the very benchmark its lore came from. Commit message
carries the data + honest uarch caveat + git-ancestry fact (617f3ef95177 not
an ancestor of the 2021 bisect base). 2 review rounds; r2 fresh-fable verified
every number/commit/mechanism claim, one sentence fixed as directed. APPROVED.

## RIGOROUSLY KILLED (mainline lane, 2026-07-16)
- request_queue nr_active_requests_shared_tags move: dedicated-cacheline
  max-benefit variant NULL on all gates (null_blk shared_tag_bitmap +
  scsi_debug host_tagset, CV<1.1%, n=18/side); naive move beside requeue_lock
  REGRESSED SCSI randread -3.2% / fsync-write -2.5% (flush-path collision).
  Mechanism notes + lesson in patches/mainline/rq-shared-tags-EVIDENCE.md.
- mm_struct mmap_lock offset-56 "restoration": the documented (Feng Tang
  2e3025434a6b, Linus-quoted) count/owner cacheline split REGRESSES on Zen4 +
  modern rwsem — split-only -1.66% p=0.03, full repack -3.04% p=0.001,
  dedicated-line null; will-it-scale mmap1 -t 32, n=24/side per variant.
  The stale comment is itself a candidate for a data-backed correction patch.
  Evidence: patches/mainline/mmap-lock-layout-EVIDENCE.md.
- Deck tooling angle: perf c2c (IBS) + perf lock contention (BPF) both work
  on Van Gogh; Civ6 AI/gfx show diffuse HITMs and ~4ms/30s total lock wait —
  no kernel-scaling target on the 4-core APU. Angle exhausted.

## LOCKED WINS, Deck era (5)

### 1. lavd-queue-scaled-compete-window.diff — fixes upstream sched-ext/scx#3303
scx_lavd. The constant LAVD_DL_COMPETE_WINDOW head-start freezes the logical
clock and disables deadline aging under oversubscription. Bisected the #3303
hackbench +50% regression to a8a25fcb, isolated to this constant. Fix: scale the
window by nr_active/nr_queued. **hackbench 25.5→17.4s (n=10, EEVDF ref 16.9),
fork-storm protection preserved, interactive wake p99 -36%.** 5 review rounds +
fresh-fable APPROVE. commitmsg included.

### 2. lavd-preempt-kick-escalation.diff
scx_lavd. Trace-proven: the IPI-free soft-yield can't evict tight-loop CPU-bound
victims (<7% of delayed wakeups displaced a hog). Fix: escalate a won preemption
to SCX_KICK_PREEMPT, gated --no-preempt-kick. **real-8CPU fps_p1 +10.8%
(p=0.009), wake p99 -35%, no regression.** 2 rounds + fresh-fable APPROVE.

### 3. amd-pstate-epp-boost-clean.diff (dynamic_epp) — REAL KERNEL PATCH, full closed loop
cpufreq/amd-pstate. Found by perfetto-tracing a real Civ6 game on the Deck:
graphics benchmark is single-thread CPU-bound, the busy core downclocks to
2.43GHz (vs 3.5 fmax) because frequent short futex sleeps collapse amd-pstate's
util signal. Causally confirmed via EPP=performance A/B (p999 -40%). Patch:
per-core dynamic EPP=performance boost for recently-busy CPUs (C0-residency
trigger, edge-triggered, hwp_boost-style), default-off module param.
Iteration story: first tried a min_perf floor → regressed p999; refuted the
write-rate hypothesis (50x fewer writes still regressed); root-caused the LEVER
(min_perf floor pins ≥nominal even during micro-idle, perturbs SMU boost on the
shared-TDP APU); switched to the EPP hint → clean.
**Civ6 graphics 1%-low fps +31.8% (p=0.014), frametime p99 -4.1% (p=0.015),
p999 no regression, n=6 interleaved.** 2 review rounds + fresh-fable APPROVE +
production-clean version (scaffolding stripped) final-reviewed. commitmsg with
numbers included.

### 4. sched-fair-smt-full-core-pref.diff (SIS_PREFER_CORE) — REAL EEVDF KERNEL PATCH
kernel/sched/fair.c. Found by tracing the Civ6 AI benchmark: the coordinator
thread runs 25% SMT-degraded while a full idle core sits free (select_idle_sibling
takes the idle-target shortcut before checking for idle cores). Patch: prefer a
full idle core over an SMT-idle target, behind a default-off SCHED_FEAT
(SIS_PREFER_CORE), asym-suppressed, target-only, stale-flag-safe.
**Civ6 aibench fps_avg +4.9% (p=0.0004), no frame regression.** Cost: schbench
low-load wake p99 28→38µs (documented, why default-off). 2 rounds + fresh-fable
APPROVE for Deck default-off carry. EVIDENCE.md included. (Upstream needs the
full tbench/netperf/multi-LLC table — noted.)

### 5. mm-thp-always-prctl.diff (PR_SET_THP_ALWAYS + khugepaged eager-collapse) — REAL KERNEL MM PATCH, new workload class
mm/huge_memory.c + mm/khugepaged.c + kernel/sys.c + prctl. Found by profiling a NEW
workload — **Factorio 2.0** (user-selected, beyond Civ6): the update loop is
memory/TLB-bound (IPC 0.38, dTLB-load-misses 73.4%, only 18.5% of the 154MB anon heap
hugepage-backed under stock `enabled=madvise`). Patch: per-mm `MMF_THP_ALWAYS` flag
(prctl opt-in) treating the mm's anon VMAs as MADV_HUGEPAGE, PLUS a khugepaged
enhancement (per-mm scan-budget boost + sleep-skip-while-collapsing + kick-on-opt-in) so
the flagged heap collapses promptly — the flag alone was neutral because stock khugepaged
is too slow. Default-off, CONFIG_64BIT-gated, anon-only.
**Factorio +2.29% UPS (OFF 1.4799 vs ON 1.4460 ms/tick, n=28/side, Welch t=5.39,
p<0.0001, ZERO regression), stock global khugepaged — proves the patch drives collapse.**
Aggressive-khugepaged ceiling +5.4% (n=18); per-process result is below it due to
documented bimodal first-collapse latency. 1 review round (REVISE: functional dead-kick
`sleep_expire`, 32-bit break, comments — all fixed) + fresh-fable steady-state APPROVE
(i386+x86_64 built clean, all dimensions run down). EVIDENCE.md + THP-DESIGN.md + raw A/B.

## RIGOROUSLY KILLED CANDIDATES (measurement discipline working)
- drm-sched-wq-highpri: prior VM winner, but REGRESSED on Van Gogh (Civ6 gfx p99
  +4.8%, p=0.005) — CPU-starved APU, evidence didn't transfer. Deck-measured.
- small-machine profile (slice/preempt defaults): falsified — the proxy signal
  was a 32-CPU-host artifact; real-8CPU (chcpu) showed stock wake p99 = 44µs, not
  6526 (>100x inflation), and the profile REGRESSED it.
- cpuidle deep-idle over-selection: red herring — CC6 exit ~20-30µs on Van Gogh
  silicon (not the ACPI 350µs), off critical path, 0.2% of a frame.
- EEVDF busy-CPU runqueue latency: 0.30% of critical-thread time (below noise);
  dominated by RT pipewire-audio preemption (userspace fix) + true oversubscription.
- xp2benchmark: DUPLICATE of #3 (multi-threaded amd-pstate downclock; all 8 cores
  ~2GHz). dynamic_epp should cover it (validation A/B in progress).
- ported CFS corpus (cachenice/wakeaffine/sisfloor): all null under proper
  interleaving on the Deck (thermal-drift artifacts under single-baseline).
- ntsync lock-boost (scx_lavd): correct + reviewed, but unmeasurable — SteamOS
  Proton uses fsync/futex, not ntsync (0 ntsync fds even forced).

## Infrastructure built (durable)
- v3 autonomous Deck iteration: grub one-shot on slot A + boot canary + hardware
  watchdog + btrfs snapshot → any failed test kernel auto-recovers to stock, zero
  manual intervention. (docs in memory: deck-autonomous-iteration.)
- Interleaved, thermally-controlled measurement harnesses (fps + per-core freq +
  perfetto + debugfs counters), runtime-toggle A/B where the patch allows
  (module param / sched_feat — no reboot per block).
