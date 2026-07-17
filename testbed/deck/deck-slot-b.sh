#!/bin/bash
# deck-slot-b.sh — runs ON the Steam Deck (as root via sudo). Manages the
# slot-B test partition for the Crucible bare-metal lane. Slot A is the
# pristine recovery anchor and is NEVER touched here.
#
# Subcommands:
#   status                 show bootconf state (this/selected/confs)
#   clone                  one-time: rsync slot A -> slot B (rootfs+var),
#                          enable sshd, install passwordless sudoers, clear
#                          btrfs ro. Makes B a modern-userspace clone of A.
#   install-kernel         mount B rw, install kernel+modules from
#                          ~/deck-deploy, regen initramfs + grub.cfg with
#                          panic=10 oops=panic + systemd watchdog.
#   select-b               make B the next-boot image (verifies selected==B)
#   select-a               revert next boot to A (recovery)
#   mark-good              (run while booted on B) mark current slot booted-ok
#   verify-running <rel>   assert `uname -r` == <rel> (booted the test kernel)
#
# Artifacts expected in ~/deck-deploy (pushed by the workstation):
#   bzImage                the built kernel image
#   modules/<release>/...  `make modules_install` tree for that kernel
#   release               file containing the kernel release string
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/home/deck/deck-deploy}"
MNT=/mnt/crucible-b
KERNEL_NAME=linux-neptune-616          # grub/boot filename stem (matches efi grub.cfg)
ROOTFS_B=/dev/disk/by-partlabel/rootfs-B
VAR_B=/dev/disk/by-partlabel/var-B
EFI_B=/dev/disk/by-partlabel/efi-B
ESP=/dev/disk/by-partlabel/esp

log()  { echo ">>> $*" >&2; }
fail() { echo "!!! $*" >&2; exit 1; }

require_root() { [ "$(id -u)" -eq 0 ] || fail "must run as root"; }

# rsync wrapper: exit 23/24 are non-fatal partial-attr warnings (SteamOS's
# btrfs.compression pseudo-xattr can't be reapplied via setxattr). Real
# failures (space, IO) surface as other codes.
run_rsync() {
    local rc=0
    rsync "$@" || rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 23 ] && [ "$rc" -ne 24 ]; then
        fail "rsync failed (code $rc)"
    fi
    return 0
}

booted_slot() { steamos-bootconf this-image; }

guard_not_b() {
    # Refuse to mutate B if we are currently BOOTED on B (would corrupt live root).
    [ "$(booted_slot)" != "B" ] || fail "currently booted on B; refusing to mutate B from itself"
}

cleanup_mounts() {
    # Best-effort recursive unmount of the B mount tree.
    if mountpoint -q "$MNT" 2>/dev/null; then
        umount -R "$MNT" 2>/dev/null || { sleep 1; umount -R "$MNT" 2>/dev/null || true; }
    fi
}

mount_b_rw() {
    guard_not_b
    mkdir -p "$MNT"
    cleanup_mounts
    mount "$ROOTFS_B" "$MNT"
    # SteamOS marks the rootfs subvol read-only (btrfs property). Clear it so
    # we can write the kernel/modules; B re-applies readonly on its own boot.
    btrfs property set "$MNT" ro false 2>/dev/null || true
    mount -o remount,rw "$MNT" 2>/dev/null || true
    mkdir -p "$MNT/var" "$MNT/efi" "$MNT/esp"
    mount "$VAR_B" "$MNT/var"
    mount "$EFI_B" "$MNT/efi"
    mount "$ESP"   "$MNT/esp"
}

cmd_status() {
    echo "booted (this-image): $(steamos-bootconf this-image 2>&1)"
    echo "selected-image:      $(steamos-bootconf selected-image 2>&1)"
    echo "list-images:         $(steamos-bootconf list-images 2>&1)"
    echo "--- A.conf ---"; cat /esp/SteamOS/conf/A.conf 2>/dev/null | grep -E "boot-|image-invalid|title"
    echo "--- B.conf ---"; cat /esp/SteamOS/conf/B.conf 2>/dev/null | grep -E "boot-|image-invalid|title"
}

cmd_clone() {
    require_root
    guard_not_b
    trap cleanup_mounts EXIT
    mount_b_rw
    log "rsync slot A rootfs -> B (A root is readonly => consistent)"
    # -x (one-file-system) is load-bearing: it stops rsync crossing into /home,
    # /var, /efi, /esp, /proc, /sys, /dev, and crucially /mnt/crucible-b (B's
    # own mount) — without it rsync recurses into B and fills the partition.
    # --delete-before purges extraneous files (incl. any prior partial clone)
    # up front so a near-full B has room for the fresh tree.
    run_rsync -aHAXx --numeric-ids --delete-before --info=stats1 \
        --exclude='/proc/*' --exclude='/sys/*' --exclude='/dev/*' \
        --exclude='/run/*' --exclude='/tmp/*' --exclude='/mnt/*' \
        --exclude='/media/*' --exclude='/home/*' --exclude='/var/*' \
        --exclude='/efi/*' --exclude='/esp/*' --exclude='/lost+found' \
        --exclude='/swapfile' --exclude='/boot/*' \
        --exclude='/usr/lib/modules/*' --exclude='/lib/modules/*' \
        / "$MNT/"
    # /boot and modules are excluded above: B boots the crucible test kernel
    # (installed by install-kernel), and the chainloader's fallback is slot A,
    # not an in-B kernel — so B needs neither of A's ~2G module tree nor A's
    # kernel (cloning both overflows B's 5G partition). NOTE: modules live at
    # the REAL path /usr/lib/modules (/lib -> /usr/lib symlink); excluding only
    # /lib/modules silently clones them and fills B.
    log "rsync slot A /var -> B/var (skip coredumps/cache/logs — var-B is small)"
    # DO NOT exclude /var/lib/overlays: SteamOS mounts /etc as an overlay whose
    # upperdir is /var/lib/overlays/etc/upper — that's where runtime /etc state
    # (WiFi/NetworkManager profiles, service enablement) actually lives. Excluding
    # it leaves B with no WiFi credentials => B boots but never joins the network
    # => unreachable with no auto-fallback. (Learned the hard way 2026-07-08.)
    run_rsync -aHAXx --numeric-ids --delete --info=stats1 \
        --exclude='/lib/systemd/coredump/*' \
        --exclude='/tmp/*' --exclude='/cache/*' --exclude='/log/*' \
        /var/ "$MNT/var/"
    log "enable sshd + install passwordless sudoers on B"
    ln -sf /usr/lib/systemd/system/sshd.service \
        "$MNT/etc/systemd/system/multi-user.target.wants/sshd.service"
    cat > "$MNT/etc/sudoers.d/zz-crucible" <<'SUDO'
deck ALL=(ALL) NOPASSWD: ALL
SUDO
    chmod 0440 "$MNT/etc/sudoers.d/zz-crucible"
    log "clone done"
    cleanup_mounts
    trap - EXIT
}

install_kernel_in_chroot() {
    local release="$1"
    # Kernel image + modules were copied into $MNT already. Regenerate the
    # initramfs and grub.cfg from *inside* B so paths/UUIDs are B's own.
    # panic=10 oops=panic => a panicking test kernel reboots and the
    # chainloader can advance boot-attempts toward fallback. Watchdog
    # (sp5100_tco) recovers hangs once systemd is up.
    cat > "$MNT/etc/systemd/system.conf.d/10-crucible-watchdog.conf" <<'WD'
[Manager]
RuntimeWatchdogSec=60
RebootWatchdogSec=2min
WD
    mkdir -p "$MNT/etc/default"
    if [ -f "$MNT/etc/default/grub" ]; then
        if grep -q "crucible-safety" "$MNT/etc/default/grub"; then :; else
            printf '\n# crucible-safety\nGRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX:-} panic=10 oops=panic"\n' \
                >> "$MNT/etc/default/grub"
        fi
    fi
    arch-chroot "$MNT" mkinitcpio -k "$release" -g "/boot/initramfs-${KERNEL_NAME}.img" \
        || fail "mkinitcpio failed"
    arch-chroot "$MNT" grub-mkconfig -o /efi/EFI/steamos/grub.cfg \
        || fail "grub-mkconfig failed"
}

cmd_install_kernel() {
    require_root
    guard_not_b
    [ -f "$DEPLOY_DIR/bzImage" ] || fail "no bzImage in $DEPLOY_DIR"
    [ -f "$DEPLOY_DIR/release" ] || fail "no release file in $DEPLOY_DIR"
    local release; release="$(cat "$DEPLOY_DIR/release")"
    [ -d "$DEPLOY_DIR/modules/$release" ] || fail "no modules/$release in $DEPLOY_DIR"
    trap cleanup_mounts EXIT
    mount_b_rw
    # B runs ONLY the crucible kernel (fallback is slot A), so purge any stale
    # kernels/modules first — otherwise leftover trees (e.g. from a clone that
    # accidentally copied A's modules) permanently occupy B's small partition.
    log "purge stale kernels/modules on B"
    rm -rf "$MNT"/usr/lib/modules/* "$MNT"/lib/modules/* 2>/dev/null || true
    rm -f  "$MNT"/boot/vmlinuz-* "$MNT"/boot/initramfs-* 2>/dev/null || true
    sync
    log "install kernel image -> B /boot/vmlinuz-${KERNEL_NAME}"
    cp -f "$DEPLOY_DIR/bzImage" "$MNT/boot/vmlinuz-${KERNEL_NAME}"
    log "install modules -> B /lib/modules/$release"
    mkdir -p "$MNT/lib/modules"
    cp -a "$DEPLOY_DIR/modules/$release" "$MNT/lib/modules/$release"
    install_kernel_in_chroot "$release"
    sync
    log "install-kernel done (release $release)"
    cleanup_mounts
    trap - EXIT
}

cmd_select_b() {
    require_root
    steamos-bootconf --image B set-mode reboot
    local sel; sel="$(steamos-bootconf selected-image)"
    [ "$sel" = "B" ] || fail "selected-image is '$sel', expected B — NOT rebooting"
    log "slot B selected for next boot"
}

cmd_select_a() {
    require_root
    steamos-bootconf --image A set-mode reboot
    log "slot A selected for next boot (selected=$(steamos-bootconf selected-image))"
}

cmd_mark_good() {
    require_root
    local slot; slot="$(booted_slot)"
    steamos-bootconf set-mode booted
    log "marked current slot ($slot) booted-ok"
}

cmd_start_agent() {
    require_root
    local agent_dir="${AGENT_DIR:-/home/deck/crucible-agent}"
    local port="${AGENT_PORT:-5000}"
    systemctl stop crucible-guest-agent 2>/dev/null || true
    systemctl reset-failed crucible-guest-agent 2>/dev/null || true
    # Transient unit so it survives the launching SSH session (plain nohup
    # dies to logind session-scope cleanup). PATH/LD_LIBRARY_PATH point at
    # the shipped perfetto trio; TCP transport for the bare-metal lane.
    systemd-run --unit=crucible-guest-agent \
        --setenv=CRUCIBLE_AGENT_TRANSPORT=tcp \
        --setenv=CRUCIBLE_AGENT_TCP_PORT="$port" \
        --setenv=PYTHONPATH="$agent_dir" \
        --setenv=PATH="$agent_dir/perfetto:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        --setenv=LD_LIBRARY_PATH="$agent_dir/perfetto/lib" \
        --working-directory="$agent_dir" \
        /usr/bin/python3 -m guest.crucible_guest_agent
    log "guest agent started (tcp :$port)"
}

cmd_verify_running() {
    local want="$1"; local got; got="$(uname -r)"
    [ "$got" = "$want" ] || fail "running kernel '$got' != expected '$want'"
    log "verified running kernel: $got"
}

case "${1:-}" in
    status)          cmd_status ;;
    clone)           cmd_clone ;;
    install-kernel)  cmd_install_kernel ;;
    select-b)        cmd_select_b ;;
    select-a)        cmd_select_a ;;
    mark-good)       cmd_mark_good ;;
    start-agent)     cmd_start_agent ;;
    verify-running)  shift; cmd_verify_running "${1:?release}" ;;
    *) echo "usage: $0 {status|clone|install-kernel|select-b|select-a|mark-good|start-agent|verify-running <rel>}" >&2; exit 2 ;;
esac
