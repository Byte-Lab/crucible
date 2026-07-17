#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

# Build the GPU-native benchmark rootfs for `vng --root`.
#
# Extends the minimal synthetic rootfs (testbed/virt/setup-rootfs.sh) with
# Mesa/Vulkan drivers, MangoHud, and the OSS benchmarks the guest
# agent's launch_benchmark RPC allow-lists (vkmark, glmark2). Based on
# Debian trixie: bookworm's Mesa 22.x predates usable RDNA3 (gfx1100 /
# 7900 XT) support, which landed in Mesa 23.1+. firmware-amd-graphics
# comes from the non-free-firmware component.
#
# Steam/Wine/Proton/gamescope are still absent — that is a later
# milestone; this rootfs proves real GPU frame timing through MangoHud
# without a compositor (vkmark/glmark2 render via DRM/KMS directly).
#
# Usage:
#   testbed/virt/setup-game-rootfs.sh [--target <dir>] [--suite <debian-suite>] [--force]
#
# The script auto-elevates via sudo if not already root.

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [--target DIR] [--suite SUITE] [--force]

Options:
  --target DIR   Output rootfs directory (default: \$CRUCIBLE_GAME_ROOTFS or ~/.crucible/game-rootfs)
  --suite SUITE  Debian suite to bootstrap (default: trixie)
  --force        Rebuild even if the stamp file is present
  -h, --help     Show this message
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_TAG="setup-game-rootfs"
# shellcheck source=lib/rootfs-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/rootfs-common.sh"

TARGET="$(rootfs_default_target "${CRUCIBLE_GAME_ROOTFS:-}" game-rootfs)"
SUITE="trixie"
FORCE=0
STAMP_NAME=".crucible-game-built"

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
    # Base (mirrors the synthetic rootfs; stress-ng kept so synthetic
    # mode also works against this image)
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
    pciutils
    # GPU stack
    mesa-vulkan-drivers
    libvulkan1
    vulkan-tools
    libdrm2
    firmware-amd-graphics
    # Benchmarks + frame timing
    vkmark
    glmark2
    glmark2-drm
    mangohud
)
PKG_LIST="$(IFS=,; echo "${PACKAGES[*]}")"

echo "[$SCRIPT_TAG] target  : $TARGET"
echo "[$SCRIPT_TAG] suite   : $SUITE"
echo "[$SCRIPT_TAG] packages: ${PACKAGES[*]}"

rootfs_apt_opts
rootfs_bootstrap "$PKG_LIST" "main non-free-firmware"
rootfs_install_guest
rootfs_write_stamp
