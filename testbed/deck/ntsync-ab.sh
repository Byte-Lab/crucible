#!/bin/bash
# ntsync boost A/B: stock scx_lavd vs ntsync-patched build.
# Verifier/load test happens implicitly: patched scheduler must reach
# "enabled" with the ntsync fexit hooks attached (module BTF present).
# Usage: ntsync-ab.sh <label> <scx_lavd-binary>
set -u
LABEL="${1:?}"; BIN="${2:?}"
CPUS="0-3,16-19"
BENCH_CPUS="0,1,2,3,16,17,18,19"
OUT=~/.crucible/ntsync-ab/$LABEL
LAVD_PID=""
mkdir -p "$OUT"
sudo modprobe ntsync || { echo "no ntsync module"; exit 1; }

stop_sched() {
    [ -z "$LAVD_PID" ] && return 0
    sudo kill -INT "$LAVD_PID" 2>/dev/null
    for _ in $(seq 1 50); do
        [ "$(cat /sys/kernel/sched_ext/state)" = "disabled" ] && break
        sleep 0.2
    done
    wait "$LAVD_PID" 2>/dev/null; LAVD_PID=""
}
trap 'stop_sched; pkill -f "stress-ng --cpu" 2>/dev/null' EXIT

sudo "$BIN" >"$OUT/sched.log" 2>&1 & LAVD_PID=$!
ok=0
for _ in $(seq 1 50); do
    [ "$(cat /sys/kernel/sched_ext/state)" = "enabled" ] && { ok=1; break; }
    sudo kill -0 "$LAVD_PID" 2>/dev/null || break
    sleep 0.2
done
[ "$ok" = 1 ] || { echo "[$LABEL] SCHED LOAD FAILED"; tail -20 "$OUT/sched.log"; exit 1; }
echo "[$LABEL] scheduler loaded OK"
sleep 1

for r in 1 2 3; do
    taskset -c "$CPUS" stress-ng --cpu 7 --cpu-method matrixprod >/dev/null 2>&1 &
    SPID=$!
    sleep 1
    # 1ms critical sections: holder preemption mid-CS now dominates convoy
    # behavior, which is exactly what the lock-holder boost protects against.
    /tmp/claude-1000/bench-ntsync -t 8 -d 15 -s 1000 -c "$BENCH_CPUS" >"$OUT/bench-$r.txt" 2>&1
    kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null
done
stop_sched
grep -H 'handoff_p99_us\|acquisitions_per_sec' "$OUT"/bench-*.txt
echo "[$LABEL] done"
