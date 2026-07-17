#!/bin/bash
# Validate a8a25fcb's stated scenario: latency-critical task wake latency
# while a fork storm runs. Compares scheduler variants.
# Usage: forkstorm-test.sh <label> <scx_lavd-binary>
set -u
LABEL="${1:?}"; BIN="${2:?}"
CPUS="0-3,16-19"
OUT=~/.crucible/forkstorm/$LABEL
LAVD_PID=""
mkdir -p "$OUT"

stop_sched() {
    [ -z "$LAVD_PID" ] && return 0
    sudo kill -INT "$LAVD_PID" 2>/dev/null
    for _ in $(seq 1 50); do
        [ "$(cat /sys/kernel/sched_ext/state)" = "disabled" ] && break
        sleep 0.2
    done
    wait "$LAVD_PID" 2>/dev/null; LAVD_PID=""
}
trap 'stop_sched; pkill -f "stress-ng --fork" 2>/dev/null' EXIT

sudo "$BIN" >"$OUT/sched.log" 2>&1 & LAVD_PID=$!
for _ in $(seq 1 50); do
    [ "$(cat /sys/kernel/sched_ext/state)" = "enabled" ] && break
    sleep 0.2
done
[ "$(cat /sys/kernel/sched_ext/state)" = "enabled" ] || { echo "load failed"; exit 1; }
sleep 1

for r in $(seq 1 "${REPS:-3}"); do
    taskset -c "$CPUS" stress-ng --fork 8 >/dev/null 2>&1 &
    SPID=$!
    sleep 1
    taskset -c "$CPUS" ~/upstream/schbench/schbench -m 2 -t 4 -r 20 >"$OUT/schbench-$r.txt" 2>&1
    kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null
    pkill -f 'stress-ng --fork' 2>/dev/null
    sleep 1
    # plain schbench (no storm) as collateral check
    taskset -c "$CPUS" ~/upstream/schbench/schbench -m 2 -t 4 -r 15 >"$OUT/plain-$r.txt" 2>&1
done
stop_sched
grep -m1 -A0 '\* 99.0th' "$OUT"/schbench-*.txt
echo "[$LABEL] done"
