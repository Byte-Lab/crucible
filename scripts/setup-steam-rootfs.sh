#!/usr/bin/env bash
# Build the Steam game-measurement rootfs for `vng --root` (milestone G3).
#
# Extends the GPU benchmark rootfs (scripts/setup-game-rootfs.sh) with the
# Steam client, steamcmd, and a weston-headless + Xwayland display stack so
# real Steam titles can run in the guest and be measured through MangoHud.
# gamescope is deliberately absent: it is not packaged in trixie (sid
# only); weston's headless backend with the GL renderer is the in-suite
# compositor. i386 multiarch is enabled for the 32-bit Steam bootstrap.
#
# Two host inputs are seeded into the image (both optional but required
# for autonomous operation):
#   - A cached Steam session (created once interactively with
#     `steamcmd +login <user> +quit` on the host) so the guest can talk to
#     Steam without a Steam Guard prompt. Source: CRUCIBLE_STEAM_SESSION
#     (default: the invoking user's ~/.local/share/Steam).
#   - A pre-downloaded Steam library (steamapps) so the guest never
#     downloads tens of GB per boot. Source: CRUCIBLE_STEAM_LIBRARY
#     (default: none; steamcmd inside the guest can download on demand).
#
# Usage:
#   scripts/setup-steam-rootfs.sh [--target <dir>] [--suite <debian-suite>] [--force]
#
# The script auto-elevates via sudo if not already root.

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [--target DIR] [--suite SUITE] [--force]

Options:
  --target DIR   Output rootfs directory (default: \$CRUCIBLE_STEAM_ROOTFS or ~/.crucible/steam-rootfs)
  --suite SUITE  Debian suite to bootstrap (default: trixie)
  --force        Rebuild even if the stamp file is present

Environment:
  CRUCIBLE_STEAM_SESSION  Host Steam dir with a cached login to seed
                          (default: invoking user's ~/.local/share/Steam)
  CRUCIBLE_STEAM_LIBRARY  Host steamapps dir to seed into the guest library
                          (default: unset — no game files seeded)
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_TAG="setup-steam-rootfs"
# shellcheck source=lib/rootfs-common.sh
source "$REPO_ROOT/scripts/lib/rootfs-common.sh"

TARGET="$(rootfs_default_target "${CRUCIBLE_STEAM_ROOTFS:-}" steam-rootfs)"
SUITE="trixie"
FORCE=0
STAMP_NAME=".crucible-steam-built"

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

# Resolve seed paths while we still know the invoking user.
seed_default_session() {
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        echo "$(getent passwd "$SUDO_USER" | cut -d: -f6)/.local/share/Steam"
    else
        echo "$HOME/.local/share/Steam"
    fi
}
STEAM_SESSION="${CRUCIBLE_STEAM_SESSION:-$(seed_default_session)}"
STEAM_LIBRARY="${CRUCIBLE_STEAM_LIBRARY:-}"

if [[ $EUID -ne 0 ]]; then
    echo "[$SCRIPT_TAG] needs root; re-exec under sudo"
    exec sudo --preserve-env=CRUCIBLE_STEAM_ROOTFS,CRUCIBLE_ROOTFS_INSECURE \
        CRUCIBLE_STEAM_SESSION="$STEAM_SESSION" \
        CRUCIBLE_STEAM_LIBRARY="$STEAM_LIBRARY" \
        bash "$0" \
        --target "$TARGET" \
        --suite "$SUITE" \
        $([[ $FORCE -eq 1 ]] && echo --force)
fi

rootfs_check_stamp
rootfs_require_mmdebstrap

PACKAGES=(
    # Base (mirrors the game rootfs; synthetic mode still works)
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
    locales-all
    # GPU stack (amd64 + i386 for the 32-bit Steam bootstrap)
    mesa-vulkan-drivers
    mesa-vulkan-drivers:i386
    libvulkan1
    libvulkan1:i386
    libgl1-mesa-dri
    libgl1-mesa-dri:i386
    vulkan-tools
    libdrm2
    firmware-amd-graphics
    # Benchmarks + frame timing (native checks stay available)
    vkmark
    glmark2
    glmark2-drm
    mangohud
    # Steam + display stack
    steam-installer
    steamcmd
    weston
    xwayland
    xauth
)
PKG_LIST="$(IFS=,; echo "${PACKAGES[*]}")"

echo "[$SCRIPT_TAG] target  : $TARGET"
echo "[$SCRIPT_TAG] suite   : $SUITE"
echo "[$SCRIPT_TAG] session : $STEAM_SESSION"
echo "[$SCRIPT_TAG] library : ${STEAM_LIBRARY:-<none>}"

rootfs_apt_opts

# The steam/steamcmd postinst asks a debconf license question that aborts
# a noninteractive install; preseed before the package set unpacks.
PRESEED='echo "steam steam/question select I AGREE" | chroot "$1" debconf-set-selections;
echo "steam steam/license note" | chroot "$1" debconf-set-selections;
echo "steamcmd steam/question select I AGREE" | chroot "$1" debconf-set-selections;
echo "steamcmd steam/license note" | chroot "$1" debconf-set-selections'

rm -rf "$TARGET"
mkdir -p "$TARGET"

mmdebstrap \
    --mode=root \
    --variant=minbase \
    --architectures=amd64,i386 \
    --include="$PKG_LIST" \
    --components="main contrib non-free non-free-firmware" \
    --essential-hook="$PRESEED" \
    "${APT_OPTS[@]}" \
    "$SUITE" \
    "$TARGET"

rootfs_install_guest

# Guest user for Steam (steamcmd refuses to run usefully as root; uid 1000
# also matches the host-seeded file ownership).
chroot "$TARGET" useradd --uid 1000 --create-home --shell /bin/bash crucible \
    || echo "[$SCRIPT_TAG] user crucible already present"

if [[ -d "$STEAM_SESSION" ]]; then
    echo "[$SCRIPT_TAG] seeding Steam session from $STEAM_SESSION"
    install -d "$TARGET/home/crucible/.local/share"
    cp -a "$STEAM_SESSION" "$TARGET/home/crucible/.local/share/Steam"
else
    echo "[$SCRIPT_TAG] WARN: no Steam session at $STEAM_SESSION — the guest"
    echo "             will prompt for Steam Guard on first login"
fi

if [[ -n "$STEAM_LIBRARY" && -d "$STEAM_LIBRARY" ]]; then
    echo "[$SCRIPT_TAG] seeding Steam library from $STEAM_LIBRARY (may take a while)"
    install -d "$TARGET/home/crucible/.local/share/Steam"
    cp -a "$STEAM_LIBRARY" "$TARGET/home/crucible/.local/share/Steam/steamapps"
fi

chroot "$TARGET" chown -R crucible:crucible /home/crucible

rootfs_write_stamp
