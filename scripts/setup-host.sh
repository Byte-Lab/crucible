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
#   scripts/setup-host.sh <pci-addr>            # precheck + print commands
#   scripts/setup-host.sh <pci-addr> --bind     # actually bind to vfio-pci
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

# --- Precheck 3: IOMMU group members --------------------------------------
# Every function in the GPU's IOMMU group (typically the GPU plus its HDMI
# audio function) must be bound to vfio-pci or unbound for passthrough.
GROUP_MEMBERS=()
if [[ -d "$DEV/iommu_group/devices" ]]; then
    group_id="$(basename "$(readlink "$DEV/iommu_group")")"
    echo "[setup-host] IOMMU group $group_id members:"
    for member in "$DEV/iommu_group/devices"/*; do
        m="$(basename "$member")"
        driver="(none)"
        [[ -L "$member/driver" ]] && driver="$(basename "$(readlink "$member/driver")")"
        echo "             $m  driver=$driver"
        GROUP_MEMBERS+=("$m")
    done
else
    echo "[setup-host] WARN: no IOMMU group for $ADDR (expected with IOMMU off)"
    GROUP_MEMBERS=("$ADDR")
fi

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
echo "[setup-host] done"
