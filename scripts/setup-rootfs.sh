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
# All overlay work (guest agent install, systemd unit enable, modules-load
# files, stamp file) runs inside mmdebstrap's user namespace via
# `--customize-hook`. Doing it after mmdebstrap exits would fail because
# the rootfs would be owned by a subuid the host user can't write.
#
# Usage:
#   scripts/setup-rootfs.sh [--target <dir>] [--suite <debian-suite>] [--force]
#
# Environment:
#   CRUCIBLE_ROOTFS         Default target dir if --target not given.
#                           (default: ~/.crucible/rootfs)
#   CRUCIBLE_ROOTFS_INSECURE  Set to 1 to skip apt signature verification.
#                           Needed when the host lacks debian-archive-keyring
#                           (e.g. Ubuntu hosts without debian-archive-keyring
#                           installed).

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
#   python3-pydantic guest agent dependency
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
PKG_LIST="$(IFS=,; echo "${PACKAGES[*]}")"

echo "[setup-rootfs] target  : $TARGET"
echo "[setup-rootfs] suite   : $SUITE"
echo "[setup-rootfs] packages: ${PACKAGES[*]}"

# A previous failed run can leave a directory owned by mmdebstrap's
# subuid that the host user can't delete. Bail with a clear hint rather
# than confusing the user with permission errors deep inside mmdebstrap.
if [[ -e "$TARGET" ]]; then
    if ! rm -rf "$TARGET" 2>/dev/null; then
        echo "[setup-rootfs] $TARGET exists but cannot be removed by \$USER." >&2
        echo "[setup-rootfs] Likely owned by a subuid from a prior mmdebstrap run." >&2
        echo "[setup-rootfs] Pick a fresh path with --target, or sudo rm -rf $TARGET." >&2
        exit 1
    fi
fi

# Stage the overlay payload into a world-traversable location. mmdebstrap's
# customize hook runs in a user namespace mapped to a subuid that can't
# traverse $HOME (mode 0750). /tmp is mode 1777 + world-readable.
STAGE="$(mktemp -d /tmp/cruc-overlay.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
cp -a "$REPO_ROOT/guest" "$STAGE/guest"
chmod -R a+rX "$STAGE"
HOOK="$STAGE/hook.sh"
cat >"$HOOK" <<'HOOK_EOF'
set -euo pipefail
ROOTFS="$1"
STAGE="$2"

install -d "$ROOTFS/opt/crucible/guest"
cp -a "$STAGE/guest/." "$ROOTFS/opt/crucible/guest/"
chmod +x "$ROOTFS/opt/crucible/guest/setup_cgroups.sh"

install -m 0644 \
    "$STAGE/guest/crucible-guest-agent.service" \
    "$ROOTFS/etc/systemd/system/crucible-guest-agent.service"
install -d "$ROOTFS/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/crucible-guest-agent.service \
    "$ROOTFS/etc/systemd/system/multi-user.target.wants/crucible-guest-agent.service"

install -d "$ROOTFS/etc/modules-load.d"
printf 'vsock\nvmw_vsock_virtio_transport\n' \
    >"$ROOTFS/etc/modules-load.d/vsock.conf"

cat >"$ROOTFS/etc/systemd/system/crucible-cgroups.service" <<'UNIT'
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
    "$ROOTFS/etc/systemd/system/multi-user.target.wants/crucible-cgroups.service"

date -u +"%Y-%m-%dT%H:%M:%SZ" >"$ROOTFS/.crucible-built"
HOOK_EOF
chmod 0755 "$HOOK"

# Insecure apt opts when the host doesn't have debian-archive-keyring
# installed (common on non-Debian hosts). The rootfs is for ephemeral
# local dev VMs, not production.
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
    --variant=minbase \
    --include="$PKG_LIST" \
    "${APT_OPTS[@]}" \
    --customize-hook="bash $HOOK \"\$1\" $STAGE" \
    "$SUITE" \
    "$TARGET"

echo "[setup-rootfs] done. rootfs at $TARGET"
