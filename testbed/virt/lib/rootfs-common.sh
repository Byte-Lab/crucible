# testbed/virt/lib/rootfs-common.sh
#
# Shared helpers for the rootfs build scripts (setup-rootfs.sh and
# setup-game-rootfs.sh). Sourced, not executed. Callers set:
#   TARGET, SUITE, FORCE, STAMP_NAME, REPO_ROOT
# before calling the functions below.

# Resolve the default rootfs target under the *invoking* user's HOME,
# even when re-invoked under sudo (where $HOME resets to /root and the
# rootfs would land in a path the regular user can't traverse).
# $1: env var override name's value (may be empty), $2: dir basename.
rootfs_default_target() {
    local override="$1" basename="$2"
    if [[ -n "$override" ]]; then
        echo "$override"
    elif [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        echo "$(getent passwd "$SUDO_USER" | cut -d: -f6)/.crucible/$basename"
    else
        echo "$HOME/.crucible/$basename"
    fi
}

# Re-exec under sudo if not already root. mmdebstrap --mode=root needs
# real uid 0 to chroot and to set file ownership in the rootfs.
# $1: the script path to re-exec ($0 from the caller).
rootfs_reexec_sudo() {
    local script="$1"
    if [[ $EUID -ne 0 ]]; then
        echo "[${SCRIPT_TAG}] needs root; re-exec under sudo"
        exec sudo --preserve-env=CRUCIBLE_ROOTFS,CRUCIBLE_GAME_ROOTFS,CRUCIBLE_ROOTFS_INSECURE \
            TARGET="$TARGET" SUITE="$SUITE" FORCE="$FORCE" \
            bash "$script" \
            --target "$TARGET" \
            --suite "$SUITE" \
            $([[ $FORCE -eq 1 ]] && echo --force)
    fi
}

# Exit 0 early when the stamp exists and --force wasn't given.
rootfs_check_stamp() {
    STAMP="$TARGET/$STAMP_NAME"
    if [[ -f "$STAMP" && $FORCE -eq 0 ]]; then
        echo "[${SCRIPT_TAG}] $STAMP exists; nothing to do (use --force to rebuild)"
        exit 0
    fi
}

rootfs_require_mmdebstrap() {
    if ! command -v mmdebstrap >/dev/null 2>&1; then
        cat >&2 <<EOF
[${SCRIPT_TAG}] mmdebstrap is required but not installed.
  Arch: paru -S mmdebstrap   (AUR)
  Debian/Ubuntu: sudo apt install mmdebstrap
  Fedora: sudo dnf install mmdebstrap
EOF
        exit 1
    fi
}

# Populate APT_OPTS with insecure-repo options when the host lacks
# debian-archive-keyring (common on non-Debian hosts). These rootfs
# images are for ephemeral dev VMs.
rootfs_apt_opts() {
    APT_OPTS=()
    if [[ "${CRUCIBLE_ROOTFS_INSECURE:-0}" == "1" ]] \
        || ! ls /usr/share/keyrings/debian-archive*.gpg >/dev/null 2>&1; then
        echo "[${SCRIPT_TAG}] debian-archive-keyring not found; skipping signature verification"
        APT_OPTS+=(
            --aptopt='APT::Get::AllowUnauthenticated "true"'
            --aptopt='Acquire::AllowInsecureRepositories "true"'
            --aptopt='Acquire::AllowDowngradeToInsecureRepositories "true"'
        )
    fi
}

# Wipe any prior rootfs and bootstrap. We're root, so subuid-owned
# remnants from an earlier mode=unshare run are fair game too.
# $1: comma-separated package list, $2: optional --components value.
rootfs_bootstrap() {
    local pkg_list="$1" components="${2:-}"
    local extra=()
    [[ -n "$components" ]] && extra+=("--components=$components")

    rm -rf "$TARGET"
    mkdir -p "$TARGET"

    mmdebstrap \
        --mode=root \
        --variant=minbase \
        --include="$pkg_list" \
        "${extra[@]}" \
        "${APT_OPTS[@]}" \
        "$SUITE" \
        "$TARGET"
}

# Overlay the Crucible guest payload and systemd units. We're running
# as real root inside this script, so direct writes work without any
# namespace dance.
rootfs_install_guest() {
    install -d "$TARGET/opt/crucible/guest"
    cp -a "$REPO_ROOT/guest/." "$TARGET/opt/crucible/guest/"
    chmod +x "$TARGET/opt/crucible/guest/setup_cgroups.sh"

    install -m 0644 \
        "$REPO_ROOT/guest/crucible-guest-agent.service" \
        "$TARGET/etc/systemd/system/crucible-guest-agent.service"
    install -d "$TARGET/etc/systemd/system/multi-user.target.wants"
    ln -sf /etc/systemd/system/crucible-guest-agent.service \
        "$TARGET/etc/systemd/system/multi-user.target.wants/crucible-guest-agent.service"

    install -d "$TARGET/etc/modules-load.d"
    printf 'vsock\nvmw_vsock_virtio_transport\n' \
        >"$TARGET/etc/modules-load.d/vsock.conf"

    cat >"$TARGET/etc/systemd/system/crucible-cgroups.service" <<'UNIT'
[Unit]
Description=Crucible cgroup hierarchy setup
Before=crucible-guest-agent.service
DefaultDependencies=no
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/opt/crucible/guest/setup_cgroups.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
    ln -sf /etc/systemd/system/crucible-cgroups.service \
        "$TARGET/etc/systemd/system/multi-user.target.wants/crucible-cgroups.service"
}

rootfs_write_stamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ" >"$STAMP"
    echo "[${SCRIPT_TAG}] done. rootfs at $TARGET"
}
