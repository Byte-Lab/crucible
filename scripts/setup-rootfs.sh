#!/usr/bin/env bash
# Build a minimal Debian rootfs suitable for `vng --root`.
#
# The image carries just enough to run the Crucible guest agent and a
# synthetic stress-ng benchmark. Steam, Wine, Mesa, and MangoHud are
# intentionally absent — those land in a separate, later milestone.
#
# Runs mmdebstrap as actual root so the resulting files are owned by
# uid 0 on disk. vng's 9p root mount reports those uids straight
# through to the guest; without uid 0 the guest's init can't mount
# /run and panics. The earlier unshare-mode rootfs (files owned by a
# subuid) tripped exactly that panic in our e2e attempts.
#
# Usage:
#   scripts/setup-rootfs.sh [--target <dir>] [--suite <debian-suite>] [--force]
#
# The script auto-elevates via sudo if not already root.

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [--target DIR] [--suite SUITE] [--force]

Options:
  --target DIR   Output rootfs directory (default: \$CRUCIBLE_ROOTFS or ~/.crucible/rootfs)
  --suite SUITE  Debian suite to bootstrap (default: bookworm)
  --force        Rebuild even if the stamp file is present
  -h, --help     Show this message
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Default target picks the invoking user's HOME, even when re-invoked
# under sudo (where $HOME resets to /root and the rootfs would land in
# a path the regular user can't traverse).
if [[ -n "${CRUCIBLE_ROOTFS:-}" ]]; then
    TARGET="$CRUCIBLE_ROOTFS"
elif [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    TARGET="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.crucible/rootfs"
else
    TARGET="$HOME/.crucible/rootfs"
fi
SUITE="bookworm"
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            TARGET="$2"; shift 2;;
        --suite)
            SUITE="$2"; shift 2;;
        --force)
            FORCE=1; shift;;
        -h|--help)
            usage; exit 0;;
        *)
            echo "unknown argument: $1" >&2
            usage
            exit 2;;
    esac
done

# Re-exec under sudo if not already root. mmdebstrap --mode=root needs
# real uid 0 to chroot and to set file ownership in the rootfs.
if [[ $EUID -ne 0 ]]; then
    echo "[setup-rootfs] needs root; re-exec under sudo"
    exec sudo --preserve-env=CRUCIBLE_ROOTFS,CRUCIBLE_ROOTFS_INSECURE \
        TARGET="$TARGET" SUITE="$SUITE" FORCE="$FORCE" \
        bash "$0" \
        --target "$TARGET" \
        --suite "$SUITE" \
        $([[ $FORCE -eq 1 ]] && echo --force)
fi

STAMP="$TARGET/.crucible-built"

if [[ -f "$STAMP" && $FORCE -eq 0 ]]; then
    echo "[setup-rootfs] $STAMP exists; nothing to do (use --force to rebuild)"
    exit 0
fi

if ! command -v mmdebstrap >/dev/null 2>&1; then
    cat >&2 <<EOF
[setup-rootfs] mmdebstrap is required but not installed.
  Arch: paru -S mmdebstrap   (AUR)
  Debian/Ubuntu: sudo apt install mmdebstrap
  Fedora: sudo dnf install mmdebstrap
EOF
    exit 1
fi

PACKAGES=(
    systemd-sysv
    udev
    python3
    stress-ng
    linux-perf
    dbus
    kmod
    util-linux
    ca-certificates
    procps
    iproute2
)
PKG_LIST="$(IFS=,; echo "${PACKAGES[*]}")"

echo "[setup-rootfs] target  : $TARGET"
echo "[setup-rootfs] suite   : $SUITE"
echo "[setup-rootfs] packages: ${PACKAGES[*]}"

# Wipe any prior rootfs (we're root, so subuid-owned remnants from an
# earlier mode=unshare run are fair game too).
rm -rf "$TARGET"
mkdir -p "$TARGET"

# Insecure apt opts when the host lacks debian-archive-keyring (common
# on non-Debian hosts). This rootfs is for ephemeral dev VMs.
APT_OPTS=()
if [[ "${CRUCIBLE_ROOTFS_INSECURE:-0}" == "1" ]] \
    || ! ls /usr/share/keyrings/debian-archive*.gpg >/dev/null 2>&1; then
    echo "[setup-rootfs] debian-archive-keyring not found; skipping signature verification"
    APT_OPTS+=(
        --aptopt='APT::Get::AllowUnauthenticated "true"'
        --aptopt='Acquire::AllowInsecureRepositories "true"'
        --aptopt='Acquire::AllowDowngradeToInsecureRepositories "true"'
    )
fi

mmdebstrap \
    --mode=root \
    --variant=minbase \
    --include="$PKG_LIST" \
    "${APT_OPTS[@]}" \
    "$SUITE" \
    "$TARGET"

# Overlay the Crucible guest payload. We're running as real root inside
# this script, so direct writes work without any namespace dance.
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

date -u +"%Y-%m-%dT%H:%M:%SZ" >"$STAMP"
echo "[setup-rootfs] done. rootfs at $TARGET"
