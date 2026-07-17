#!/bin/bash
# Unattended EEVDF kernel-patch grind on the Steam Deck via the v3 one-shot
# mechanism. For each patch: build neptune kernel -> deploy as crucible-test
# -> one-shot boot -> verify -> Civ6 gfx+AI benchmark arms -> reboot to stock.
# A patch whose kernel fails to boot is recorded and skipped (Deck returns to
# stock by itself; nothing here requires hands).
set -u

KSRC=~/upstream/crucible_kernel_2
DECK="deck@192.168.86.80"
SSH="ssh -i $HOME/.ssh/crucible_deck_ed25519 -o BatchMode=yes -o ConnectTimeout=8"
PATCH_DIR=~/upstream/crucible/deck/patches
OUT=~/.crucible/eevdf-grind
mkdir -p "$OUT"

declare -A PATCHES=(
  [sisfloor]="$PATCH_DIR/sched-fair-sis-util-idle-floor-neptune.diff"
  [wakeaffine]="$HOME/.crucible/civ6-winners/cycle21-wake-affine-package/wake-affine-sync-respect-idle-prev-cpu.diff"
  [cachenice]="$HOME/.crucible/civ6-winners/cycle17-llc-cache-nice-tries-package/llc-cache-nice-tries-grace.diff"
)
ORDER=(cachenice wakeaffine sisfloor)   # cachenice first: strongest prior Civ6-AI evidence

log() { echo "[eevdf-grind $(date +%H:%M)] $*"; }

deck_up() {
    for _ in $(seq 1 "${1:-40}"); do
        $SSH "$DECK" true 2>/dev/null && return 0
        sleep 20
    done
    return 1
}

deck_uname() { $SSH "$DECK" uname -r 2>/dev/null; }

for name in "${ORDER[@]}"; do
    patch="${PATCHES[$name]}"
    rel="6.16.12-valve24.4-1-neptune-616-$name"
    log "=== $name: build"
    cd "$KSRC"
    git checkout -- . || { log "$name: tree reset failed"; continue; }
    git apply "$patch" || { log "$name: PATCH APPLY FAILED"; continue; }
    sed -i "s/^CONFIG_LOCALVERSION=.*/CONFIG_LOCALVERSION=\"-1-neptune-616-$name\"/" .config
    sed -i 's/-Werror//g' tools/lib/bpf/Makefile
    taskset -c 0-7,16-23 make LOCALVERSION= -j16 olddefconfig >/dev/null 2>&1
    if ! taskset -c 0-7,16-23 make LOCALVERSION= -j16 >"$OUT/$name.build.log" 2>&1; then
        log "$name: BUILD FAILED (see $OUT/$name.build.log)"; git checkout -- .; continue
    fi
    [ "$(cat include/config/kernel.release)" = "$rel" ] || { log "$name: release mismatch"; git checkout -- .; continue; }
    rm -rf "/tmp/claude-1000/mods-$name"
    taskset -c 0-7,16-23 make LOCALVERSION= INSTALL_MOD_STRIP=1 \
        INSTALL_MOD_PATH="/tmp/claude-1000/mods-$name" modules_install >>"$OUT/$name.build.log" 2>&1

    log "$name: deploy to Deck"
    deck_up 40 || { log "$name: deck unreachable pre-deploy"; git checkout -- .; continue; }
    scp -q -i ~/.ssh/crucible_deck_ed25519 arch/x86/boot/bzImage "$DECK:/home/deck/bzImage-$name"
    rsync -a -e "ssh -i $HOME/.ssh/crucible_deck_ed25519" \
        "/tmp/claude-1000/mods-$name/lib/modules/$rel" "$DECK:/home/deck/mods-staging/"
    $SSH "$DECK" "set -e
        sudo steamos-readonly disable 2>/dev/null || true
        # drop any previous test release modules (space) except stock
        for d in /usr/lib/modules/*; do case \$d in *gfe145653a794) ;; *-\$(uname -r)) ;; *neptune-616) ;; *) case \$(basename \$d) in *sisfloor|*wakeaffine|*cachenice|*drmsched) sudo rm -rf \$d ;; esac ;; esac; done
        sudo rsync -a --delete /home/deck/mods-staging/$rel /usr/lib/modules/
        sudo depmod $rel
        sudo cp /home/deck/bzImage-$name /boot/vmlinuz-crucible-test
        sudo mkinitcpio -k $rel -g /boot/initramfs-crucible-test.img >/dev/null 2>&1
        sudo steamos-readonly enable 2>/dev/null || true
        sudo grub-editenv /efi/EFI/steamos/crucible-oneshot.env set crucible_boot_test=1
        sudo systemd-run --on-active=3 systemctl reboot >/dev/null 2>&1
        echo deployed-rebooting" || { log "$name: deploy failed"; git checkout -- .; continue; }
    git checkout -- .

    log "$name: waiting for boot"
    sleep 45
    deck_up 45 || { log "$name: DECK DID NOT RETURN in 15min — check later"; continue; }
    got="$(deck_uname)"
    if [ "$got" != "$rel" ]; then
        log "$name: BOOT FAILED (running $got) — kernel rejected, skipping benchmarks"
        echo "$name BOOT-FAILED $got" >>"$OUT/verdicts.txt"
        continue
    fi
    log "$name: test kernel live, benchmarking"
    $SSH "$DECK" "/home/deck/civ6-bench.sh gfx-$name none 4 graphicsbenchmark" >"$OUT/$name.gfx.log" 2>&1
    $SSH "$DECK" "/home/deck/civ6-bench.sh ai-$name none 4 aibenchmark" >"$OUT/$name.ai.log" 2>&1
    mkdir -p "/tmp/claude-1000/civ6-results"
    scp -q -r -i ~/.ssh/crucible_deck_ed25519 "$DECK:/home/deck/bench-results/gfx-$name" /tmp/claude-1000/civ6-results/ 2>/dev/null
    scp -q -r -i ~/.ssh/crucible_deck_ed25519 "$DECK:/home/deck/bench-results/ai-$name" /tmp/claude-1000/civ6-results/ 2>/dev/null

    log "$name: back to stock"
    $SSH "$DECK" "sudo systemd-run --on-active=3 systemctl reboot >/dev/null 2>&1"
    sleep 45; deck_up 45
    log "$name: done (now on $(deck_uname))"
    echo "$name MEASURED" >>"$OUT/verdicts.txt"
done
log "grind complete: $(cat "$OUT/verdicts.txt" 2>/dev/null | tr '\n' '; ')"
