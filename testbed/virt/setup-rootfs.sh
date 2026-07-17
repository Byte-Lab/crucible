#!/usr/bin/env bash
# Build a minimal Debian rootfs suitable for `vng --root`.
#
# The image carries just enough to run the Crucible guest agent and a
# synthetic stress-ng benchmark. Steam, Wine, Mesa, and MangoHud are
# intentionally absent — for the GPU-native benchmark rootfs see
# testbed/virt/setup-game-rootfs.sh.
#
# Runs mmdebstrap as actual root so the resulting files are owned by
# uid 0 on disk. vng's 9p root mount reports those uids straight
# through to the guest; without uid 0 the guest's init can't mount
# /run and panics. The earlier unshare-mode rootfs (files owned by a
# subuid) tripped exactly that panic in our e2e attempts.
#
# Usage:
#   testbed/virt/setup-rootfs.sh [--target <dir>] [--suite <debian-suite>] [--force]
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_TAG="setup-rootfs"
# shellcheck source=lib/rootfs-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/rootfs-common.sh"

TARGET="$(rootfs_default_target "${CRUCIBLE_ROOTFS:-}" rootfs)"
SUITE="bookworm"
FORCE=0
STAMP_NAME=".crucible-built"

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

rootfs_reexec_sudo "$0"
rootfs_check_stamp
rootfs_require_mmdebstrap

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

echo "[$SCRIPT_TAG] target  : $TARGET"
echo "[$SCRIPT_TAG] suite   : $SUITE"
echo "[$SCRIPT_TAG] packages: ${PACKAGES[*]}"

rootfs_apt_opts
rootfs_bootstrap "$PKG_LIST"
rootfs_install_guest
rootfs_write_stamp
