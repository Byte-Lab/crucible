#!/usr/bin/env bash
# Build a minimal Debian rootfs suitable for `vng --root`.
#
# The image carries just enough to run the Crucible guest agent and a
# synthetic stress-ng benchmark. Steam, Wine, Mesa, and MangoHud are
# intentionally absent — those land in a separate, later milestone.
#
# Rootless by default via `mmdebstrap`. If `mmdebstrap` isn't installed
# we fail fast with an install hint rather than silently switching to
# `debootstrap` (which needs root).
#
# Usage:
#   scripts/setup-rootfs.sh [--target <dir>] [--suite <debian-suite>] [--force]
#
# Environment:
#   CRUCIBLE_ROOTFS   Default target dir if --target not given.
#                     (default: ~/.crucible/rootfs)

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
TARGET="${CRUCIBLE_ROOTFS:-$HOME/.crucible/rootfs}"
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

# Packages the guest needs:
#   systemd-sysv  systemd + init scripts
#   python3       runs the guest agent module
#   stress-ng     synthetic workload driver
#   linux-perf    "perf" binary (optional, useful for future bench types)
#   dbus          systemd dependency surface
#   kmod          modprobe for vsock module load on boot
#   util-linux    coreutils-ish baseline
#   ca-certificates  TLS roots for any future apt/curl calls
#   procps        ps/kill basics
#   iproute2      `ip` for ad-hoc debugging
PACKAGES=(
    systemd-sysv
    python3
    python3-pydantic
    stress-ng
    linux-perf
    dbus
    kmod
    util-linux
    ca-certificates
    procps
    iproute2
)

echo "[setup-rootfs] target  : $TARGET"
echo "[setup-rootfs] suite   : $SUITE"
echo "[setup-rootfs] packages: ${PACKAGES[*]}"

mkdir -p "$TARGET"

# Bootstrap the base system.
PKG_LIST="$(IFS=,; echo "${PACKAGES[*]}")"
mmdebstrap \
    --variant=minbase \
    --include="$PKG_LIST" \
    "$SUITE" \
    "$TARGET"

# Install the guest agent payload.
install -d "$TARGET/opt/crucible"
rsync -a --delete "$REPO_ROOT/guest/" "$TARGET/opt/crucible/guest/"

# Drop the systemd unit and enable it at boot.
install -m 0644 \
    "$REPO_ROOT/guest/crucible-guest-agent.service" \
    "$TARGET/etc/systemd/system/crucible-guest-agent.service"
install -d "$TARGET/etc/systemd/system/multi-user.target.wants"
ln -sf \
    /etc/systemd/system/crucible-guest-agent.service \
    "$TARGET/etc/systemd/system/multi-user.target.wants/crucible-guest-agent.service"

# Load vsock virtio transport at boot so the agent can bind port 5000.
install -d "$TARGET/etc/modules-load.d"
cat >"$TARGET/etc/modules-load.d/vsock.conf" <<EOF
vsock
vmw_vsock_virtio_transport
EOF

# Run the cgroup setup script as a oneshot before the guest agent starts.
cat >"$TARGET/etc/systemd/system/crucible-cgroups.service" <<'EOF'
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
EOF
ln -sf \
    /etc/systemd/system/crucible-cgroups.service \
    "$TARGET/etc/systemd/system/multi-user.target.wants/crucible-cgroups.service"

# Stamp the rootfs so re-runs short-circuit.
date -u +"%Y-%m-%dT%H:%M:%SZ" >"$STAMP"
echo "[setup-rootfs] done. rootfs at $TARGET"
