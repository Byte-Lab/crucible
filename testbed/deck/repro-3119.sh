#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

# Attempt to reproduce scx#3119: runnable tasks not scheduled for ~20ms
# despite idle CPUs. Reporter: Intel Cooperlake, ~90% util,
# --performance --slice-min-us 5000 --slice-max-us 20000 --pinned-slice-us 5000.
# Detection: schbench wake-latency p99.9/max outliers >= ~20ms while total
# CPU util is kept ~90% by stress-ng.
set -u
BIN="${1:-$HOME/upstream/scx/target/release/scx_lavd}"
LABEL="${2:-repro3119}"
OUT=~/.crucible/repro-3119/$LABEL
LAVD_PID=""
NCPU=$(nproc)
HOGS=$(( NCPU * ${UTIL_PCT:-90} / 100 ))
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
trap 'stop_sched; pkill -f "stress-ng --cpu" 2>/dev/null' EXIT

sudo "$BIN" --performance --slice-min-us 5000 --slice-max-us 20000 \
    --pinned-slice-us 5000 >"$OUT/sched.log" 2>&1 & LAVD_PID=$!
for _ in $(seq 1 50); do
    [ "$(cat /sys/kernel/sched_ext/state)" = "enabled" ] && break
    sudo kill -0 "$LAVD_PID" 2>/dev/null || { echo "load failed"; tail -5 "$OUT/sched.log"; exit 1; }
    sleep 0.2
done
sleep 1

for r in 1 2 3; do
    stress-ng --cpu "$HOGS" --cpu-method matrixprod >/dev/null 2>&1 &
    SPID=$!
    sleep 2
    ~/upstream/schbench/schbench -m 4 -t 8 -r 30 >"$OUT/schbench-$r.txt" 2>&1
    kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null
    sleep 1
done
stop_sched

echo "[$LABEL] wake p99 / p99.9 / max per rep:"
grep -E '99.0th|99.9th|max=' "$OUT"/schbench-*.txt | head -12
