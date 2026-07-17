# Misplaced-wake bailout attribution (baseline-ai.pftrace, 2026-07-17)

Question (review round-1, dim 5): the 6.16 trace analysis counted misplaced
wakes (wakee lands on a busy core's idle sibling while a fully idle core
exists) but did not attribute them to a select_idle_sibling() bailout;
the patch gates only the @target bailout.

Method: for every Civ6-thread wake (off-cpu gap > 0.2ms) in
traces/baseline-ai.pftrace whose landing CPU had a busy SMT sibling while
some fully idle core existed (n=3682 across all Civ6 threads; the original
finding's n=140 was burst-start worker wakes only), classify the landing
CPU against (a) the wakee's previous CPU (last sched slice) and (b) the
waker's CPU at wake time (thread_state.waker_utid -> waker's running slice).

Result:
  landed == wakee prev cpu       3523  (96%)
  landed == neither              158   (4%)   (scan / recent_used)
  landed == waker cpu == prev       1  (0%)

Interpretation: the waker is on-CPU at the moment of the wake, so its own
CPU is busy and wake_affine() resolves to the wakee's idle prev as @target
for non-sync wakes (futex wakes are non-sync). select_idle_sibling() then
returns it via the *target* fast bailout -- the one the feature gates.
Caveat: a WF_SYNC wake with the waker alone on its rq yields
target=this_cpu, after which a landed-on-prev wake goes via the *prev*
bailout and would be miscounted here; sync wakes are rare in this
workload, and the target-only Deck A/B (+4.9% fps, p=0.0004) is the
empirical confirmation that target-gating captures the mechanism. The 96% is therefore covered by target-only gating;
the empirical Deck A/B (+4.9% fps p=0.0004 with the target-only build)
confirms it. Residual 4% arrives via scan/recent_used and is out of scope.

Analysis script: session scratchpad (bisect-based interval lookup over
sched + thread_state dumps via trace_processor_shell).
