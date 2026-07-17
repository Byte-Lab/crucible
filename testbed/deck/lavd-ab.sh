#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

# Host-side A/B harness for scx_lavd variants (Steam Deck proxy).
#
# Proxy model: Deck = 4c/8t single-LLC Van Gogh. Host = 7950X; we pin the
# WORKLOAD to 4 cores + SMT siblings on CCD0 (0-3,16-19) so contention
# dynamics resemble the Deck's small-CPU regime. The scheduler under test
# (scx_lavd variant) runs system-wide — sched_ext replaces the scheduler for
# the whole box — but only pinned-workload metrics are recorded.
#
# Usage: lavd-ab.sh <label> <scx_lavd-binary> [reps] [lavd-extra-args...]
#   label   → results in ~/.crucible/lavd-ab/<label>/repN/
#   binary  → path to scx_lavd build to test ("cfs" = don't load any, baseline EEVDF)
#
# Workloads per rep:
#   1. vkmark headless under MangoHud + stress-ng CPU contention on the same
#      cpuset → frame-time distribution (the gaming metric).
#   2. schbench wakeup-latency percentiles under the same contention.
#   3. perf bench sched messaging (throughput sanity).
#
# Requires passwordless sudo (scheduler load + cleanup).
set -u

LABEL="${1:?label}"
LAVD_BIN="${2:?scx_lavd binary or 'cfs'}"
REPS="${3:-5}"
shift 3 2>/dev/null || shift $#
LAVD_ARGS=("$@")

PROXY_CPUS="0-3,16-19"          # 4c/8t Deck-proxy cpuset (CCD0 + SMT siblings)
VKMARK_DUR=30                    # seconds of vkmark scene
LOG_DUR=25                       # MangoHud log window — must end BEFORE vkmark exits or the CSV never flushes
OUT_ROOT="$HOME/.crucible/lavd-ab/$LABEL"
LAVD_PID=""

log() { echo "[lavd-ab] $*"; }

cleanup() {
    stop_sched
    pkill -f 'stress-ng --cpu 6 --cpu-method matrixprod' 2>/dev/null
    pkill -x vkmark 2>/dev/null
}
trap cleanup EXIT

start_sched() {
    [ "$LAVD_BIN" = "cfs" ] && return 0
    sudo "$LAVD_BIN" "${LAVD_ARGS[@]}" >"$OUT_ROOT/lavd.log" 2>&1 &
    LAVD_PID=$!
    for _ in $(seq 1 50); do
        [ "$(cat /sys/kernel/sched_ext/state 2>/dev/null)" = "enabled" ] && return 0
        # bail early if the loader already died (verifier reject etc.)
        sudo kill -0 "$LAVD_PID" 2>/dev/null || { log "scheduler died on load"; tail -20 "$OUT_ROOT/lavd.log"; return 1; }
        sleep 0.2
    done
    log "scheduler never reached enabled state"; return 1
}

stop_sched() {
    [ -z "$LAVD_PID" ] && return 0
    sudo kill -INT "$LAVD_PID" 2>/dev/null
    for _ in $(seq 1 50); do
        [ "$(cat /sys/kernel/sched_ext/state 2>/dev/null)" = "disabled" ] && break
        sleep 0.2
    done
    wait "$LAVD_PID" 2>/dev/null
    LAVD_PID=""
}

# MangoHud drops two files per log stop: a big per-frame CSV (row 3 header
# starts with "fps,frametime") and a small summary CSV. Keep the per-frame
# one as frames-<tag>.csv, summary as summary-<tag>.csv, so the next run's
# `ls -t` can't confuse them.
move_frame_csv() {
    local out="$1" tag="$2" f
    for f in "$out"/vkmark_*.csv; do
        [ -e "$f" ] || continue
        if [ "$(stat -c%s "$f")" -gt 100000 ]; then
            mv "$f" "$out/frames-$tag.csv"
        else
            mv "$f" "$out/summary-$tag.csv"
        fi
    done
}

run_rep() {
    local rep="$1"
    local out="$OUT_ROOT/rep$rep"
    mkdir -p "$out"

    # background CPU contention on the proxy cpuset: 6 hogs on 8 hw threads
    # leaves headroom so the scheduler's latency-criticality routing (not raw
    # starvation) decides frame pacing.
    taskset -c "$PROXY_CPUS" stress-ng --cpu 6 --cpu-method matrixprod \
        >/dev/null 2>&1 &
    local STRESS_PID=$!
    sleep 1

    # 1a) vkmark capped at 90fps (Deck-like frame cadence: render thread sleeps
    #     between frames → periodic latency-sensitive wakeups; metric = jank)
    MANGOHUD_CONFIG="output_folder=$out,log_duration=$LOG_DUR,autostart_log=1,log_interval=0,fps_limit=90" \
        taskset -c "$PROXY_CPUS" mangohud vkmark --winsys headless \
        -b vertex:duration=$VKMARK_DUR \
        >"$out/vkmark-capped.txt" 2>&1
    move_frame_csv "$out" capped

    # 1b) vkmark uncapped (throughput + tail under contention)
    MANGOHUD_CONFIG="output_folder=$out,log_duration=$LOG_DUR,autostart_log=1,log_interval=0" \
        taskset -c "$PROXY_CPUS" mangohud vkmark --winsys headless \
        -b vertex:duration=$VKMARK_DUR \
        >"$out/vkmark-uncapped.txt" 2>&1
    move_frame_csv "$out" uncapped

    # 2) schbench wakeup latency (own contention profile: its workers + the hogs)
    taskset -c "$PROXY_CPUS" ~/upstream/schbench/schbench -m 2 -t 4 -r 20 \
        >"$out/schbench.txt" 2>&1

    # 3) sched messaging throughput
    taskset -c "$PROXY_CPUS" perf bench sched messaging -g 8 -l 400 \
        >"$out/messaging.txt" 2>&1

    kill "$STRESS_PID" 2>/dev/null; wait "$STRESS_PID" 2>/dev/null
}

mkdir -p "$OUT_ROOT"
log "label=$LABEL sched=$LAVD_BIN reps=$REPS args=${LAVD_ARGS[*]:-none}"
start_sched || exit 1
for r in $(seq 1 "$REPS"); do
    log "rep $r/$REPS"
    run_rep "$r"
done
stop_sched
log "done → $OUT_ROOT"
