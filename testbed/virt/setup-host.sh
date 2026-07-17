#!/usr/bin/env bash
# Host-side VFIO passthrough preparation for Crucible game mode.
#
# Default mode is read-only: prechecks IOMMU, inspects the target GPU's
# IOMMU group, and PRINTS the bind commands without running them.
# Binding a GPU that the host is actively using (driving a display,
# loaded amdgpu) can kill the session — only `--bind` executes, and it
# asks for confirmation first.
#
# Usage:
#   testbed/virt/setup-host.sh <pci-addr>            # precheck + print commands
#   testbed/virt/setup-host.sh <pci-addr> --bind     # actually bind to vfio-pci
#
# <pci-addr> is the short form from config/crucible.toml [vm] vfio_device,
# e.g. "0a:00.0". The domain prefix ("0000:") is added automatically.

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 <pci-addr> [--bind]

  <pci-addr>  PCI address of the GPU function, e.g. 0a:00.0
  --bind      Execute the vfio-pci bind (default: print commands only)
EOF
}

ADDR=""
DO_BIND=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --bind) DO_BIND=1; shift;;
        -h|--help) usage; exit 0;;
        -*) echo "unknown argument: $1" >&2; usage; exit 2;;
        *) ADDR="$1"; shift;;
    esac
done

if [[ -z "$ADDR" ]]; then
    usage
    exit 2
fi

# Domain-qualify the short config form.
if [[ "$ADDR" != *:*:* ]]; then
    ADDR="0000:$ADDR"
fi

if [[ ! "$ADDR" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$ ]]; then
    echo "[setup-host] invalid PCI address: $ADDR (expected e.g. 0a:00.0)" >&2
    exit 2
fi

DEV="/sys/bus/pci/devices/$ADDR"

fail=0

# --- Precheck 1: device exists -------------------------------------------
if [[ ! -d "$DEV" ]]; then
    echo "[setup-host] FAIL: $DEV does not exist (check the PCI address with lspci)" >&2
    exit 1
fi
echo "[setup-host] device: $ADDR ($(lspci -s "${ADDR#0000:}" 2>/dev/null | head -1 || echo 'lspci unavailable'))"

# --- Precheck 2: IOMMU enabled -------------------------------------------
if compgen -G "/sys/kernel/iommu_groups/*" >/dev/null; then
    echo "[setup-host] OK: IOMMU groups present"
else
    echo "[setup-host] FAIL: no IOMMU groups. Enable IOMMU in BIOS and add" >&2
    echo "             amd_iommu=on (or intel_iommu=on) to the kernel cmdline:" >&2
    echo "               $(cat /proc/cmdline)" >&2
    fail=1
fi

# --- Precheck 3: slot siblings + IOMMU group members ----------------------
# A modern GPU is a multifunction device (e.g. a 7900 XT is VGA + HDMI
# audio + USB + UCSI, each in its own IOMMU group). QEMU's bus reset at
# attach affects every function, so ALL of them must move to vfio-pci —
# passing only the VGA function fails with "depends on group N which is
# not owned". Collect every function of the slot, then every member of
# each function's IOMMU group.
SLOT="${ADDR%.*}"
GROUP_MEMBERS=()
for sibling in "/sys/bus/pci/devices/$SLOT".*; do
    s="$(basename "$sibling")"
    if [[ -d "$sibling/iommu_group/devices" ]]; then
        group_id="$(basename "$(readlink "$sibling/iommu_group")")"
        echo "[setup-host] $s — IOMMU group $group_id members:"
        for member in "$sibling/iommu_group/devices"/*; do
            m="$(basename "$member")"
            driver="(none)"
            [[ -L "$member/driver" ]] && driver="$(basename "$(readlink "$member/driver")")"
            echo "             $m  driver=$driver"
            GROUP_MEMBERS+=("$m")
        done
    else
        echo "[setup-host] WARN: no IOMMU group for $s (expected with IOMMU off)"
        GROUP_MEMBERS+=("$s")
    fi
done
# De-duplicate (a group can contain a sibling we already walked).
mapfile -t GROUP_MEMBERS < <(printf '%s\n' "${GROUP_MEMBERS[@]}" | sort -u)

# --- Current driver --------------------------------------------------------
current="(none)"
[[ -L "$DEV/driver" ]] && current="$(basename "$(readlink "$DEV/driver")")"
if [[ "$current" == "vfio-pci" ]]; then
    echo "[setup-host] OK: $ADDR already bound to vfio-pci; nothing to do"
    exit 0
fi
echo "[setup-host] current driver for $ADDR: $current"

if [[ "$current" == "amdgpu" || "$current" == "nouveau" || "$current" == "i915" ]]; then
    echo "[setup-host] WARN: $ADDR is driven by $current. If it is rendering the"
    echo "             host display, binding it to vfio-pci WILL kill your session."
fi

if [[ $fail -ne 0 ]]; then
    echo "[setup-host] prechecks failed; fix the above before binding" >&2
    exit 1
fi

# --- Bind commands ---------------------------------------------------------
print_bind_commands() {
    local m
    echo "  sudo modprobe vfio-pci"
    for m in "${GROUP_MEMBERS[@]}"; do
        echo "  echo vfio-pci | sudo tee /sys/bus/pci/devices/$m/driver_override"
        echo "  [ -L /sys/bus/pci/devices/$m/driver ] && echo $m | sudo tee /sys/bus/pci/devices/$m/driver/unbind"
        echo "  echo $m | sudo tee /sys/bus/pci/drivers_probe"
    done
}

if [[ $DO_BIND -eq 0 ]]; then
    echo "[setup-host] dry run. To bind, run these commands (or re-run with --bind):"
    print_bind_commands
    exit 0
fi

echo "[setup-host] about to bind ${GROUP_MEMBERS[*]} to vfio-pci."
echo "             This detaches the device(s) from their current driver."
read -r -p "[setup-host] type 'yes' to continue: " answer
if [[ "$answer" != "yes" ]]; then
    echo "[setup-host] aborted"
    exit 1
fi

sudo modprobe vfio-pci
for m in "${GROUP_MEMBERS[@]}"; do
    echo vfio-pci | sudo tee "/sys/bus/pci/devices/$m/driver_override" >/dev/null
    if [[ -L "/sys/bus/pci/devices/$m/driver" ]]; then
        echo "$m" | sudo tee "/sys/bus/pci/devices/$m/driver/unbind" >/dev/null
    fi
    echo "$m" | sudo tee /sys/bus/pci/drivers_probe >/dev/null
    bound="(none)"
    [[ -L "/sys/bus/pci/devices/$m/driver" ]] \
        && bound="$(basename "$(readlink "/sys/bus/pci/devices/$m/driver")")"
    echo "[setup-host] $m now bound to: $bound"
done

# The orchestrator runs QEMU unprivileged: it needs to open each affected
# /dev/vfio/<group> node, and enough RLIMIT_MEMLOCK to pin all guest RAM
# for the IOMMU mappings.
run_user="${SUDO_USER:-$(id -un)}"
if [[ "$run_user" == "root" ]]; then
    echo "[setup-host] WARN: invoked as root without sudo — /dev/vfio nodes will be"
    echo "             owned by root, and an unprivileged orchestrator cannot open"
    echo "             them. Re-run via sudo from the orchestrator's user, or chown"
    echo "             the nodes manually."
fi
for m in "${GROUP_MEMBERS[@]}"; do
    g="$(basename "$(readlink "/sys/bus/pci/devices/$m/iommu_group" 2>/dev/null)")" || continue
    [[ -n "$g" && -e "/dev/vfio/$g" ]] || continue
    sudo chown "$run_user" "/dev/vfio/$g"
    echo "[setup-host] /dev/vfio/$g now owned by $run_user"
done
echo "[setup-host] done"
echo "[setup-host] NOTE: unprivileged QEMU also needs a memlock limit >= guest"
echo "             RAM. Check with 'ulimit -l'; raise via /etc/security/limits.d/"
echo "             (e.g. '$run_user hard memlock unlimited' + relogin) or"
echo "             'sudo prlimit --pid <orchestrator-pid> --memlock=unlimited'."
