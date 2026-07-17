#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

# Interleaved CPU-scheduler benchmark on the Deck: stock EEVDF vs a test
# kernel (deployed as crucible-test). Alternating blocks kill thermal drift.
# Metrics come from schbench (wakeup + request latency) and a timed
# `perf bench sched messaging`-equivalent via schbench throughput.
#
# schbench is CPU/scheduler-bound (no GPU), so an EEVDF wakeup/placement
# patch can actually show signal here — unlike GPU-bound Civ6.
#
# Usage: cpu-bench-interleaved.sh <test-release-suffix> <rounds>
#   e.g. cpu-bench-interleaved.sh sisfloor 4
set -u
SUFFIX="${1:?test release suffix, e.g. sisfloor}"
ROUNDS="${2:-4}"
DECK="deck@192.168.86.80"
SSH="ssh -i $HOME/.ssh/crucible_deck_ed25519 -o BatchMode=yes -o ConnectTimeout=8"
OUT=~/.crucible/cpu-bench/$SUFFIX
mkdir -p "$OUT"; : > "$OUT/run.log"
log() { echo "[cpubench $(date +%H:%M)] $*" | tee -a "$OUT/run.log"; }
deck_up() { for _ in $(seq 1 45); do $SSH "$DECK" true 2>/dev/null && return 0; sleep 20; done; return 1; }

boot() {
    [ "$1" = test ] && $SSH "$DECK" "sudo grub-editenv /efi/EFI/steamos/crucible-oneshot.env set crucible_boot_test=1"
    $SSH "$DECK" "sudo systemd-run --on-active=3 systemctl reboot >/dev/null 2>&1"
    sleep 50; deck_up || return 1
    $SSH "$DECK" uname -r
}

# One block = 3 schbench runs at moderate load (message threads m=2, workers
# t=4 => 8 threads on 8 CPUs, 20s each). Captures wakeup p50/p99/p99.9.
block() {
    local arm="$1" i
    for i in 1 2 3; do
        $SSH "$DECK" "/home/deck/schbench -m 2 -t 4 -r 20" >"$OUT/$arm-r$i.txt" 2>&1
        sleep 8   # cooldown
    done
}

for round in $(seq 1 "$ROUNDS"); do
    log "round $round: stock"
    rel=$(boot stock) || { log "deck lost"; exit 1; }
    case "$rel" in *gfe145653a794) ;; *) log "expected stock got $rel"; exit 1 ;; esac
    block "stock-r$round"
    log "round $round: test($SUFFIX)"
    rel=$(boot test) || { log "deck lost"; exit 1; }
    case "$rel" in *$SUFFIX) ;; *) log "test boot failed ($rel)"; exit 1 ;; esac
    block "test-r$round"
done

log "final reboot to stock"
$SSH "$DECK" "sudo systemd-run --on-active=3 systemctl reboot >/dev/null 2>&1"; sleep 50; deck_up
log "done on $($SSH "$DECK" uname -r)"
