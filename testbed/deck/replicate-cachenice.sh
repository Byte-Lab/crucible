#!/bin/bash
# Interleaved, thermally-controlled replication for cachenice vs stock EEVDF.
# Alternating kernel blocks (stock, cache, stock, cache, stock, cache) so any
# session drift (thermal/cache) is shared symmetrically across arms. Focus:
# aibenchmark (the CPU-bound workload where the win appeared). Each block:
# 3x aibenchmark + 2x graphicsbenchmark, with an 8s cooldown between reps.
# Requires cachenice already deployed as crucible-test (modules+initramfs).
set -u
DECK="deck@192.168.86.80"
SSH="ssh -i $HOME/.ssh/crucible_deck_ed25519 -o BatchMode=yes -o ConnectTimeout=8"
OUT=~/.crucible/eevdf-grind
: > "$OUT/replicate.log"
log() { echo "[replicate $(date +%H:%M)] $*" | tee -a "$OUT/replicate.log"; }
deck_up() { for _ in $(seq 1 45); do $SSH "$DECK" true 2>/dev/null && return 0; sleep 20; done; return 1; }

boot() {   # $1 = stock|test ; echoes running release
    [ "$1" = test ] && $SSH "$DECK" "sudo grub-editenv /efi/EFI/steamos/crucible-oneshot.env set crucible_boot_test=1"
    $SSH "$DECK" "sudo systemd-run --on-active=3 systemctl reboot >/dev/null 2>&1"
    sleep 50; deck_up || return 1
    $SSH "$DECK" uname -r
}

block() {  # $1 = arm label
    $SSH "$DECK" "COOLDOWN=8 /home/deck/civ6-bench.sh ai-$1 none 3 aibenchmark; COOLDOWN=8 /home/deck/civ6-bench.sh gfx-$1 none 2 graphicsbenchmark" >>"$OUT/replicate.log" 2>&1
}

for round in 1 2 3; do
    log "round $round: stock block"
    rel=$(boot stock) || { log "deck lost"; exit 1; }
    case "$rel" in *gfe145653a794) ;; *) log "expected stock, got $rel"; exit 1 ;; esac
    block "rstock$round"
    log "round $round: cachenice block"
    rel=$(boot test) || { log "deck lost"; exit 1; }
    case "$rel" in *cachenice) ;; *) log "cachenice boot failed ($rel)"; exit 1 ;; esac
    block "rcache$round"
done

log "final reboot to stock"
$SSH "$DECK" "sudo systemd-run --on-active=3 systemctl reboot >/dev/null 2>&1"; sleep 50; deck_up
for r in 1 2 3; do for k in rstock rcache; do for b in ai gfx; do
    scp -q -r -i ~/.ssh/crucible_deck_ed25519 "$DECK:/home/deck/bench-results/$b-$k$r" /tmp/claude-1000/civ6-results/ 2>/dev/null
done; done; done
log "replication harvested on $($SSH "$DECK" uname -r)"
