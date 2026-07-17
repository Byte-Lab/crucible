#!/bin/bash
# Reproduce + bisect scx#3303: hackbench 20s→32s between scx_lavd v1.0.19 → v1.0.20.
# Reporter: dual-core Skylake, --performance. We emulate the small machine by
# pinning the WORKLOAD to 2c/4t (0-1,16-17); the scheduler runs system-wide.
# Uses perf bench sched messaging (hackbench equivalent, same -g/-l semantics).
#
# Usage: bisect-3303.sh <sha...>   (each sha = ~/upstream/scx-bisect/<sha>)
set -u

CPUS="0-1,16-17"
REPS="${REPS:-3}"
OUT=~/.crucible/bisect-3303
LAVD_PID=""
mkdir -p "$OUT"

stop_sched() {
    [ -z "$LAVD_PID" ] && return 0
    sudo kill -INT "$LAVD_PID" 2>/dev/null
    for _ in $(seq 1 50); do
        [ "$(cat /sys/kernel/sched_ext/state)" = "disabled" ] && break
        sleep 0.2
    done
    wait "$LAVD_PID" 2>/dev/null
    LAVD_PID=""
}
trap stop_sched EXIT

for sha in "$@"; do
    bin=~/upstream/scx-bisect/$sha/target/release/scx_lavd
    [ -x "$bin" ] || { echo "[$sha] MISSING BINARY"; continue; }

    sudo "$bin" --performance >"$OUT/$sha.sched.log" 2>&1 &
    LAVD_PID=$!
    ok=0
    for _ in $(seq 1 50); do
        [ "$(cat /sys/kernel/sched_ext/state)" = "enabled" ] && { ok=1; break; }
        sudo kill -0 "$LAVD_PID" 2>/dev/null || break
        sleep 0.2
    done
    if [ "$ok" != 1 ]; then
        echo "[$sha] SCHED LOAD FAILED"; tail -5 "$OUT/$sha.sched.log"
        stop_sched; continue
    fi
    sleep 1

    : >"$OUT/$sha.times.txt"
    for r in $(seq 1 $REPS); do
        taskset -c "$CPUS" perf bench sched messaging -g 8 -l 10000 2>&1 |
            grep 'Total time' >>"$OUT/$sha.times.txt"
    done
    stop_sched
    echo "[$sha] $(tr '\n' ' ' <"$OUT/$sha.times.txt")"
done
