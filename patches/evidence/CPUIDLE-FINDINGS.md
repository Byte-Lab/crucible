# CPUIDLE on the Steam Deck (Van Gogh): patch investigation — VERDICT: NOT WORTH A PATCH

**Question:** is "cpuidle over-selects the deepest CC6 state (~155k deep-idle entries, ~41,500 waking
within 250µs)" a real, kernel-addressable, critical-path bottleneck for Civ6 on the Deck?

**Answer: No. The over-selection is real (≈25% of deepest-state entries wake before target residency),
but it is measurably NOT on the critical path.** Actual measured wake-from-deepest-idle latency for the
game's threads is avg ~29µs / max ~109µs — two orders of magnitude below the frame budget, and *smaller
than the ACPI-advertised exit latency itself*. Recommend: do not patch the cpuidle governor.

---

## 1. Ground truth (read live from the Deck via SSH, 192.168.86.80, kernel 6.16.12-valve24.4-1-neptune-616-combo)

- **Driver: `acpi_idle`** (not amd/intel native). **Governor: `menu`** (available: ladder, menu, teo;
  no `cpuidle.governor=` on cmdline, so menu wins by rating 20 vs teo's 19).
- Config has all three governors built (`CONFIG_CPU_IDLE_GOV_LADDER/MENU/TEO=y`); `CONFIG_HZ_1000`,
  `NO_HZ_FULL`, full `PREEMPT`.
- Idle state table (sysfs, cpu0 — 8 CPUs, 4c/8t Zen2):

| idx | name | desc | exit latency | target residency |
|-----|------|------|--------------|------------------|
| 0 | POLL | polling | 0 | 0 |
| 1 | C1 | ACPI FFH MWAIT 0x0 | 1 µs | 2 µs |
| 2 | C2 | ACPI IOPORT 0x414 | 18 µs | 36 µs |
| 3 | C3 | ACPI IOPORT 0x415 | **350 µs** | **700 µs** | ← the "CC6" deep state

- Kernel's own since-boot misprediction counters on cpu0: C3 `above` (too deep) = 221,742 of
  768,921 usages = **28.8%** — consistent with what the traces show below. So yes, menu chronically
  over-selects C3 by its own accounting.

## 2. Idle-state distribution (traces, trace_processor v56.1, `cpuidle` ftrace counter per CPU)

Graphics benchmark trace (`baseline-gfx.pftrace`, 44.9 s, 8 CPUs = 359 CPU-s; system ~75% idle):

| state | entries | total residency | avg residency |
|-------|---------|-----------------|---------------|
| POLL | 390 | 0.014 s | 34 µs |
| C1 | 6,917 | 0.75 s | 109 µs |
| C2 | 67,255 | 29.0 s | 430 µs |
| C3 | 87,623 | 239.1 s | **2.73 ms** |

AI benchmark trace is nearly identical (C3: 88,493 entries / 237.4 s).

- C3 takes 54% of idle *entries* and **89% of idle time**. Average C3 sleep is 2.7 ms = 3.9× target
  residency, i.e. the bulk of C3 selection is *correct*.
- The original "~155k deep-idle entries" ≈ C2+C3 entries combined (154,878). The "~41,500 waking
  within 250µs" reproduces exactly as C2<250µs (25,758) + C3<250µs (15,753) = **41,511 — it counted C2
  (18µs exit latency) as "deep idle"**. C2 waking at <250µs is not a misprediction at all (C2's target
  residency is 36µs); the alarming number was mostly benign C2 behavior.

## 3. Over-selection quantified (the part of the claim that IS true)

C3 entries whose actual residency fell below thresholds (gfx trace):

| threshold | count | % of C3 entries |
|-----------|-------|-----------------|
| < 700 µs (target residency → "too deep" by definition) | 21,694 | 24.8% |
| < 350 µs (below even the stated exit latency) | 17,164 | 19.6% |
| < 250 µs | 15,753 | 18.0% |
| < 100 µs | 10,805 | 12.3% |

(AI trace: 22,702 short C3 of 88,493 = 25.7%. C2, for contrast, is well-selected: only 4.7% of C2
entries wake below its 36µs target.)

Worst-case "wasted exit latency" if the ACPI 350µs figure were real: 21,694 × 350µs ≈ 7.6 s over
359 CPU-s = 2.1% of machine time — **but spread across idle CPUs, and the 350µs figure is fiction
(see §4)**. Using the *measured* exit cost (~20-30µs) it is ~0.5 s = 0.15%, and it is an energy
cost, not a latency cost.

## 4. Critical-path test (the crux) — deep-idle exits do NOT delay the game

Method: every real wakeup (sleep→Runnable→Running, via `thread_state`) of the 7 Civ6 critical threads
(main render thread + 5 workers + coordinator), joined to the idle state of the CPU the thread was
placed on at waking time. The Runnable duration spans sched_waking → sched_switch, so it *includes*
IPI delivery + idle-state exit + scheduling.

Graphics benchmark (10,521 wakeups over 44.9 s):

| CPU state at wake | wakeups | avg latency | max latency | >250 µs |
|-------------------|---------|-------------|-------------|---------|
| **C3 (deep)** | **3,648** | **29.0 µs** | **106 µs** | **0** |
| C2 | 1,897 | 19.1 µs | 83 µs | 0 |
| C1 | 213 | 8.2 µs | 22 µs | 0 |
| busy (no idle) | 760 | 29.3 µs | 1,275 µs | 6 |

AI benchmark (7,164 wakeups): same shape — C3 wakes avg 28.1 µs / max 109 µs / **zero** >250µs; the
only 16 wakeups exceeding 250µs in the whole trace were on **busy** CPUs (runqueue delay, max 2.37 ms).

Of the C3-interrupting game wakeups, 1,661 (gfx) / 1,337 (AI) terminated a "mispredicted" short-C3
sleep — and even those paid avg 25.9 µs, max **79 µs**.

Conclusions this forces:

1. **The real CC6 exit cost on Van Gogh is ~20-30 µs, not 350 µs.** The ACPI \_CST table is (typically)
   conservative; the measured wake-from-C3 latency (29µs avg incl. IPI + schedule) is ~21µs above the
   C1 baseline (8µs) and never exceeded 109µs across ~7,500 C3 wakes in both traces. The premise "each
   mispredicted C3 entry pays 350µs" is empirically false on this silicon.
2. **Per-frame cost is noise.** ~81 game-thread C3 wakes/s ≈ 1.5-2 per frame × ~21µs extra ≈ ~40µs
   ≈ 0.2% of a 25 ms (40 fps) frame — and much of even that overlaps GPU work rather than extending
   the frame.
3. The C3 sleeps the game *does* interrupt average 2.84 ms residency — mostly correctly-chosen deep
   idle, not mispredictions.
4. Actual wakeup tail latency lives elsewhere: waking onto **busy** CPUs (scheduler placement /
   runqueue contention), max 1.3-2.4 ms. If anything is worth attention it's task placement (EEVDF /
   select_idle_sibling territory), not cpuidle.

## 5. Governor logic (menu vs teo) — why the over-selection happens, and why fixing it still wouldn't pay

- **menu** (`drivers/cpuidle/governors/menu.c`): predicts idle length as min(next-timer × per-bucket
  correction factor, `get_typical_interval()` over the last 8 idle intervals). On a mostly-idle CPU the
  next timer is far away and the recent-interval detector rejects high-variance samples, so
  `predicted_ns` stays large → picks C3. Interrupt-driven (non-timer) wakeups — exactly what a
  vsync/IPC-paced game generates — are what menu's timer-anchored prediction misses. That is the
  documented weakness teo was written for.
- **teo** (`drivers/cpuidle/governors/teo.c`): bins wakeups by idle state whose residency range the
  *measured* idle duration fell into ("intercepts" vs timer "hits", decayed). With ~25% of C3 sleeps
  intercepted short, teo would likely demote a fraction of those entries to C2. It is available on the
  Deck today: `echo teo > /sys/devices/system/cpu/cpuidle/current_governor` — a zero-patch A/B test.
- But the *benefit ceiling* is fixed by §4: demoting a short C3 to C2 saves (29−19)≈10 µs on the few
  wakeups per frame that land on such a CPU, i.e. tens of µs per 16-25 ms frame. Unmeasurable in fps.
  The honest framing of the teo/menu question here is **battery/thermals** (mispredicted deep entries
  waste entry/exit energy; conversely C2-instead-of-C3 burns more idle power — cuts both ways), not
  performance. On a TDP-limited APU, *more* deep idle on idle cores can even help by leaving thermal
  headroom for the GPU — another reason not to blindly bias shallow.

## 6. Verdict and recommendation

**Red herring for performance. Do NOT write a cpuidle governor kernel patch.**

- The "over-selects deepest state" observation is directionally true (≈25% of C3 entries are too
  short by target-residency accounting; the kernel's own sysfs `above` counters agree) but the
  headline numbers in the original finding conflated C2 with the deep state (41.5k → only ~15.7k are C3).
- The cost model behind the concern (350µs exit latency paid on the game's critical path) is
  empirically false: measured C3-exit wakeup latency for Civ6 threads is 29µs avg / ≤109µs max, with
  zero wakes >250µs in 45s on either benchmark. Total critical-path exposure ≈ 0.2% of frame time.
- The genuine tail-latency events (up to 2.4 ms) are wakeups onto **busy** CPUs — a scheduler
  placement question, which is where Deck-lane patch effort should stay (EEVDF/select_idle path).
- If anyone still wants signal here, the free experiment is a governor A/B
  (`current_governor` menu↔teo, sysfs, no kernel build) measuring **fps + battery draw**; expectation
  from this data: fps delta indistinguishable from noise.

### Provenance
- Traces: `/tmp/claude-1000/baseline-gfx.pftrace` (Civ6 graphicsbenchmark, 44.9s),
  `/tmp/claude-1000/baseline-ai.pftrace` (Civ6 aibenchmark, ~45s), analyzed with
  `trace_processor_shell` v56.1; queries kept at `/tmp/claude-1000/q_*.sql`.
- Deck sysfs/cmdline read live 2026-07-12 over SSH (slot A test kernel `vmlinuz-crucible-test`,
  `6.16.12-valve24.4-1-neptune-616-combo`).
- Governor source: `/home/void/upstream/crucible_kernel_2/drivers/cpuidle/governors/{menu,teo}.c`
  (neptune 6.16.12 tree; ratings menu=20 > teo=19 ⇒ menu default).
