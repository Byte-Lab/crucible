# SIS_PREFER_CORE — validation benchmark plan

Patch: `deck/patches/sched-fair-smt-full-core-pref.diff`
Feature: `SCHED_FEAT(SIS_PREFER_CORE, false)` — **default OFF**.
Toggle at runtime (no reboot):

```
echo SIS_PREFER_CORE   > /sys/kernel/debug/sched/features   # on
echo NO_SIS_PREFER_CORE > /sys/kernel/debug/sched/features   # off
```

Because the feature is a runtime static-branch toggle, **every test below is
an A/B on the same kernel image, same boot** — flip the flag between runs.
This removes kernel-build/boot variance from the comparison entirely.

Target: Steam Deck, Van Gogh, 4c/8t, single LLC, symmetric capacity,
kernel 6.16.12-neptune. `SCHED_SMT=y`, `SCHED_MC=y`, `SCHED_CLUSTER=y`.

## Ground rules

- Two states only: `NO_SIS_PREFER_CORE` (= stock, the control) and
  `SIS_PREFER_CORE` (treatment). Confirm the flag is set before each run
  (`grep PREFER_CORE /sys/kernel/debug/sched/features`).
- ≥10 repetitions per state per workload; randomize/interleave A/B order to
  spread thermal drift. Report median + IQR (or Welch t + Cohen's d, matching
  the crucible evaluator).
- Pin nothing the scheduler is being tested on. Keep the same power/TDP mode
  and, if possible, a fixed GPU/CPU clock cap to cut variance on the Deck.
- Watch for thermal throttle: log `sensors` / frequencies; discard runs that
  throttled asymmetrically between A and B.

## 1. REGRESSION guard (MUST run — this is the gate to APPROVE)

The whole risk is that forcing a full-core placement adds a cross-core
migration that costs cache warmth on latency-critical, lightly-loaded
wakeup ping-pong. These four must show **no regression** with the flag ON:

| Bench | Command (representative) | Why | Watch |
|-------|--------------------------|-----|-------|
| **tbench** | `tbench_srv & tbench -t 30 <1..8> localhost` | classic SIS wakeup-latency/locality canary; the exact class Rik van Riel's `c722f35b513f` regressed | throughput at low client counts (1,2,4) — where migration cost bites hardest |
| **netperf TCP_RR** | `netperf -t TCP_RR -l 30 -- -r 1,1` (1 and few pairs) | pure sync ping-pong, sub-µs wakeups, warmth-sensitive | transactions/sec at 1–4 pairs |
| **schbench** | `schbench -m 2 -t 4 -r 30` (also a full-LLC-load run) | wakeup latency percentiles (p50/p99/p99.9) directly | p99/p99.9 latency must not rise |
| **hackbench** | `hackbench -g 4 -l 10000` (pipe + socket) | many-wakeup stress; sensitive to migration churn and scan cost | total time; run both `-pipe` and default |

Decision rule: if any of these regresses beyond noise (esp. tbench low-count
throughput or schbench p99), the patch stays OFF and target-only is
insufficient — do NOT ship it enabled.

## 2. BENEFIT confirmation (the reason the patch exists)

| Bench | Command | Why |
|-------|---------|-----|
| **Civ6 AI benchmark** | `-benchmark aibenchmark` (the CPU-bound late-game AI workload from the trace) headless on the Deck, MangoHud CSV; also `graphicsbenchmark` for a GPU-bound contrast | this is the traced workload (6 threads on 4c/8t, coordinator SMT-degraded 25.3%). Primary signal: fps_avg / fps_p1 / frame_time_p99, plus a re-capture of the perfetto trace to confirm the coordinator's SMT-degraded runtime % drops and burst-start wakeups stop landing on the busy sibling |

Bounded expectation from the trace analysis: ~5% wall on Civ6 AI. Confirm with
≥10 runs A/B; a real win should move both fps and the trace-level
SMT-degraded-runtime metric in the same direction.

## Target-only caveat to verify

The patch gates **only the `target` shortcut**; `prev` and `recent_used`
remain stock. The trace did not isolate which shortcut placed the 15/140
mis-placed wakeups. Civ6's coordinator→worker wakeups are sync/exec-path
(WF_SYNC), which route through `target` (the waker's CPU), so target-only
*should* capture the mechanism. **Verification:** if the re-captured Civ6
trace shows the coordinator's SMT-degraded % essentially unchanged with the
flag ON, the mis-placements were arriving via `prev`/`recent_used` instead —
in that case extend the guard to `prev` (accepting the higher regression risk
and re-running section 1 in full) before concluding the patch is ineffective.

## Suggested order

1. tbench + netperf TCP_RR low-count (fastest way to catch a regression).
2. schbench p99 + hackbench.
3. Civ6 aibenchmark A/B + trace re-capture.
4. Only if 1–2 are clean AND 3 shows benefit: recommend shipping with the
   feature exposed (still default-off) + a Deck-specific opt-in.
