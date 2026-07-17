#!/bin/bash
# Real-8-CPU host experiment: offline CCD1 + siblings so the host becomes a
# genuine 4c/8t machine (kills the "proxy inflates slice_max benefit"
# objection). Re-enables CPUs on ANY exit.
set -u

OFFLINE="4-15,20-31"

reenable() {
    sudo chcpu -e "$OFFLINE" >/dev/null 2>&1
    echo "[off8] CPUs re-enabled: $(nproc) online"
}
trap reenable EXIT

sudo chcpu -d "$OFFLINE" >/dev/null 2>&1
echo "[off8] online now: $(cat /sys/devices/system/cpu/online) ($(nproc) CPUs)"
[ "$(nproc)" = 8 ] || { echo "[off8] offline failed"; exit 1; }

~/upstream/crucible/deck/lavd-ab.sh off8-cfs cfs 5
~/upstream/crucible/deck/lavd-ab.sh off8-lavd ~/upstream/scx/target/release/scx_lavd 5
~/upstream/crucible/deck/lavd-ab.sh off8-small ~/upstream/scx-smallmachine/target/release/scx_lavd 5
~/upstream/crucible/deck/lavd-ab.sh off8-deck ~/upstream/scx-deckbuild/target/release/scx_lavd 5

# auto-trigger proof for the review record
grep -h -iE 'profile|slice_max|preempt' ~/.crucible/lavd-ab/off8-small/lavd.log | head -5

# fork-storm under the pair (protection must hold on real 8 CPUs)
REPS=6 ~/upstream/crucible/deck/forkstorm-test.sh off8-deck ~/upstream/scx-deckbuild/target/release/scx_lavd
REPS=6 ~/upstream/crucible/deck/forkstorm-test.sh off8-lavd ~/upstream/scx/target/release/scx_lavd

echo "[off8] experiment done"
