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
# Three host inputs are seeded into the image (all optional but required
# for autonomous operation):
#   - A cached Steam session (created once interactively with
#     `steamcmd +login <user> +quit` on the host) so steamcmd in the guest
#     can talk to Steam without a Steam Guard prompt. Source:
#     CRUCIBLE_STEAM_SESSION (default: the invoking user's
#     ~/.local/share/Steam).
#   - A pre-downloaded Steam library (steamapps) so the guest never
#     downloads tens of GB per boot. Source: CRUCIBLE_STEAM_LIBRARY
#     (default: none; steamcmd inside the guest can download on demand).
#     The library MUST be current: the client refuses -applaunch on an
#     update-required app and starts downloading instead, which dies with
#     "Disk write failure" in vng's small tmpfs overlay (and would be
#     ephemeral anyway). Refresh in place with host steamcmd before any
#     benchmark session after Valve ships a game update:
#       D="<lib>/common/dota 2 beta"
#       mkdir -p "$D/steamapps/common"
#       cp <lib>/appmanifest_{570,1628350}.acf "$D/steamapps/"
#       ln -sfn <lib>/common/SteamLinuxRuntime_sniper "$D/steamapps/common/"
#       steamcmd +force_install_dir "$D" +login <user> +app_update 570 +quit
#       cp "$D/steamapps/appmanifest_570.acf" <lib>/
#     (the nested manifests give the session visibility of the runtime
#     dependency, or steamcmd suspends the 570 job to fetch it and +quit
#     kills both).
#   - Full-client login credentials. The steamcmd session above is NOT
#     enough: the client keeps its JWT in local.vdf + loginusers.vdf, a
#     store steamcmd never writes, and without it the client loops on
#     "WaitingForCredentials → logon failure" (observed 2026-06-16).
#     Source: CRUCIBLE_STEAM_CLIENT_CREDS, a Steam dir from a real client
#     login (default: the invoking user's snap Steam,
#     ~/snap/steam/common/.local/share/Steam). Verified 2026-07-01: the
#     copied JWT logs on inside the VM ('OK', no Steam Guard prompt).
#
# The Steam client itself is extracted from Valve's bootstrap tarball
# (version + sha256 taken from the packaged /usr/games/steam wrapper)
# directly into ~/.local/share/Steam. The Debian wrapper is never run:
# headless it blocks forever on a zenity first-run dialog, and it targets
# ~/.steam/debian-installation rather than the seeded client.
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
  CRUCIBLE_STEAM_SESSION       Host Steam dir with a cached steamcmd login
                               (default: invoking user's ~/.local/share/Steam)
  CRUCIBLE_STEAM_LIBRARY       Host steamapps dir to seed into the guest
                               library (default: unset — no game files seeded)
  CRUCIBLE_STEAM_CLIENT_CREDS  Steam dir from a real full-client login; its
                               local.vdf + config/loginusers.vdf are seeded so
                               the guest client can log on (default: invoking
                               user's ~/snap/steam/common/.local/share/Steam)
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
seed_default_client_creds() {
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        echo "$(getent passwd "$SUDO_USER" | cut -d: -f6)/snap/steam/common/.local/share/Steam"
    else
        echo "$HOME/snap/steam/common/.local/share/Steam"
    fi
}
STEAM_SESSION="${CRUCIBLE_STEAM_SESSION:-$(seed_default_session)}"
STEAM_LIBRARY="${CRUCIBLE_STEAM_LIBRARY:-}"
STEAM_CLIENT_CREDS="${CRUCIBLE_STEAM_CLIENT_CREDS:-$(seed_default_client_creds)}"

if [[ $EUID -ne 0 ]]; then
    echo "[$SCRIPT_TAG] needs root; re-exec under sudo"
    exec sudo --preserve-env=CRUCIBLE_STEAM_ROOTFS,CRUCIBLE_ROOTFS_INSECURE \
        CRUCIBLE_STEAM_SESSION="$STEAM_SESSION" \
        CRUCIBLE_STEAM_LIBRARY="$STEAM_LIBRARY" \
        CRUCIBLE_STEAM_CLIENT_CREDS="$STEAM_CLIENT_CREDS" \
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
    # dbus-launch: without it steam-runtime-launcher-service crash-loops
    # at client boot until Steam disables it.
    dbus-x11
    # Slirp gives the guest a NIC but no lease; the guest agent runs
    # dhclient before launching Steam (CM logon needs a route out).
    isc-dhcp-client
    # Perfetto (traced/traced_probes) for kernel-scheduler tracing during
    # the comparison-phase game run; the analyzer reasons over the trace.
    # The test kernel already has FTRACE/FTRACE_SYSCALLS/TRACEPOINTS.
    perfetto
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
# weston's EGL init opens /dev/dri/* as the steam user; without these
# groups it dies with Permission denied.
chroot "$TARGET" usermod -aG video,render crucible

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
    # The session seed above already created a steamapps/ dir (the host
    # Steam carries one); copy the library's *contents* into it rather
    # than the dir itself, or the game lands in steamapps/steamapps/.
    install -d "$TARGET/home/crucible/.local/share/Steam/steamapps"
    cp -a "$STEAM_LIBRARY/." "$TARGET/home/crucible/.local/share/Steam/steamapps/"
fi

# --- Steam client bootstrap (extracted, never wrapper-run) ------------------
# Version + sha256 come from the packaged wrapper so this stays in sync
# with the steam-installer .deb.
GUEST_STEAM_DIR="$TARGET/home/crucible/.local/share/Steam"
WRAPPER="$TARGET/usr/games/steam"
BOOTSTRAP_VERSION="$(sed -n 's/^version="\(.*\)"$/\1/p' "$WRAPPER" | head -1)"
BOOTSTRAP_SHA256="$(sed -n 's/^sha256="\(.*\)"$/\1/p' "$WRAPPER" | head -1)"
if [[ -z "$BOOTSTRAP_VERSION" || -z "$BOOTSTRAP_SHA256" ]]; then
    echo "[$SCRIPT_TAG] ERROR: cannot parse bootstrap version/sha from $WRAPPER" >&2
    exit 1
fi
if [[ -x "$GUEST_STEAM_DIR/steam.sh" ]]; then
    echo "[$SCRIPT_TAG] Steam client already extracted; skipping bootstrap"
else
    echo "[$SCRIPT_TAG] extracting Steam client bootstrap $BOOTSTRAP_VERSION"
    BOOTSTRAP_TMP="$(mktemp -d)"
    trap 'rm -rf "$BOOTSTRAP_TMP"' EXIT
    curl -fsSL -o "$BOOTSTRAP_TMP/steam.tar.gz" \
        "https://repo.steampowered.com/steam/archive/beta/steam_${BOOTSTRAP_VERSION}.tar.gz"
    echo "$BOOTSTRAP_SHA256 *$BOOTSTRAP_TMP/steam.tar.gz" | sha256sum -c - >/dev/null
    tar -C "$BOOTSTRAP_TMP" -zxf "$BOOTSTRAP_TMP/steam.tar.gz" \
        steam-launcher/bootstraplinux_ubuntu12_32.tar.xz
    install -d "$GUEST_STEAM_DIR"
    tar -C "$GUEST_STEAM_DIR" \
        -xf "$BOOTSTRAP_TMP/steam-launcher/bootstraplinux_ubuntu12_32.tar.xz"
fi

# steam.sh resolves everything through ~/.steam/{steam,root}; the guest
# agent invokes it directly (the wrapper would re-point these at
# ~/.steam/debian-installation).
install -d "$TARGET/home/crucible/.steam"
ln -sfn /home/crucible/.local/share/Steam "$TARGET/home/crucible/.steam/steam"
ln -sfn /home/crucible/.local/share/Steam "$TARGET/home/crucible/.steam/root"

# --- Full-client login credentials ------------------------------------------
if [[ -d "$STEAM_CLIENT_CREDS" && -f "$STEAM_CLIENT_CREDS/local.vdf" ]]; then
    echo "[$SCRIPT_TAG] seeding client credentials from $STEAM_CLIENT_CREDS"
    install -d "$GUEST_STEAM_DIR/config"
    cp "$STEAM_CLIENT_CREDS/local.vdf" "$GUEST_STEAM_DIR/local.vdf"
    cp "$STEAM_CLIENT_CREDS/config/loginusers.vdf" \
        "$GUEST_STEAM_DIR/config/loginusers.vdf"
    AUTOLOGIN_USER="$(sed -n 's/.*"AccountName"[[:space:]]*"\(.*\)"/\1/p' \
        "$GUEST_STEAM_DIR/config/loginusers.vdf" | head -1)"
    cat > "$TARGET/home/crucible/.steam/registry.vdf" <<EOF
"Registry"
{
	"HKCU"
	{
		"Software"
		{
			"Valve"
			{
				"Steam"
				{
					"AutoLoginUser"		"$AUTOLOGIN_USER"
					"RememberPassword"		"1"
				}
			}
		}
	}
}
EOF
else
    echo "[$SCRIPT_TAG] WARN: no full-client credentials at $STEAM_CLIENT_CREDS —"
    echo "             the guest Steam client will fail CM logon"
    echo "             (WaitingForCredentials); log in once with the snap/host"
    echo "             Steam client or point CRUCIBLE_STEAM_CLIENT_CREDS at a"
    echo "             logged-in Steam dir"
fi

chroot "$TARGET" chown -R crucible:crucible /home/crucible

rootfs_write_stamp
