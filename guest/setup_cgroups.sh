#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

set -euo pipefail
CGROUP_ROOT="/sys/fs/cgroup/crucible"
mkdir -p "$CGROUP_ROOT/game"
mkdir -p "$CGROUP_ROOT/compositor"
mkdir -p "$CGROUP_ROOT/wine"
mkdir -p "$CGROUP_ROOT/mesa"
mkdir -p "$CGROUP_ROOT/system"
for group in game compositor wine mesa system; do
    echo "+cpu +memory +io" > "$CGROUP_ROOT/$group/cgroup.subtree_control" 2>/dev/null || true
done
echo "Crucible cgroup hierarchy created at $CGROUP_ROOT"
