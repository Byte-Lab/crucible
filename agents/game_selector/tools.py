from __future__ import annotations

import os
import re
from typing import Any

from agents.common.tool_registry import ToolRegistry

# Native OSS GPU benchmarks runnable without Steam (guest launch_benchmark
# RPC allow-lists these). Selected when [measurement] mode = "game" on a
# rootfs with no Steam library.
NATIVE_BENCHMARKS: dict[str, dict[str, Any]] = {
    "vkmark": {
        "benchmark_args": [],
        "workload_profile": "gpu_heavy",
        "api": "vulkan",
    },
    "glmark2": {
        "benchmark_args": [],
        "workload_profile": "gpu_heavy",
        "api": "opengl",
    },
}

BENCHMARK_GAMES: dict[int, dict[str, Any]] = {
    289070: {
        "name": "Sid Meier's Civilization VI",
        # Verified headless on RADV passthrough (2026-07-02). Three
        # self-terminating modes: graphicsbenchmark (GPU flythrough,
        # writes first-party per-frame CSV), xp2benchmark (heavier GS
        # scene), aibenchmark (CPU-bound late-game AI turns).
        "benchmark_args": ["-benchmark", "graphicsbenchmark"],
        "workload_profile": "gpu_heavy",
    },
    750920: {
        "name": "Shadow of the Tomb Raider",
        "benchmark_args": ["--benchmark"],
        "workload_profile": "gpu_heavy",
    },
    1091500: {
        "name": "Cyberpunk 2077",
        "benchmark_args": ["--benchmark"],
        "workload_profile": "gpu_heavy",
    },
    1174180: {
        "name": "Red Dead Redemption 2",
        "benchmark_args": ["-benchmark"],
        "workload_profile": "gpu_heavy",
    },
}


def _parse_acf_name(acf_path: str) -> str | None:
    """Extract the game name from a Steam ACF manifest file."""
    try:
        with open(acf_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        match = re.search(r'"name"\s+"([^"]+)"', content)
        if match:
            return match.group(1)
    except OSError:
        pass
    return None


def _parse_acf_appid(acf_path: str) -> int | None:
    """Extract the app ID from a Steam ACF manifest file."""
    basename = os.path.basename(acf_path)
    match = re.match(r"appmanifest_(\d+)\.acf", basename)
    if match:
        return int(match.group(1))
    return None


def make_game_selector_tools(registry: ToolRegistry) -> None:
    """Register game-selector tools into the given registry."""

    @registry.tool(description="List installed Steam games by scanning the Steam library for appmanifest ACF files. Searches common paths if no path given.")
    def list_steam_games(library_path: str = "") -> dict:
        search_paths = [
            # Seeded guest rootfs library first: in steam mode the guest
            # launches from this library, not the host's — scanning only
            # host paths made the selector claim the configured app "is
            # not installed" while the launch succeeded anyway.
            os.path.expanduser(
                "~/.crucible/steam-rootfs/home/crucible/.local/share/Steam/steamapps"
            ),
            os.path.expanduser("~/snap/steam/common/.local/share/Steam/steamapps"),
            os.path.expanduser("~/.local/share/Steam/steamapps"),
            os.path.expanduser("~/.steam/steam/steamapps"),
        ]
        if library_path:
            search_paths = [os.path.expanduser(library_path)]

        games: list[dict[str, Any]] = []
        searched: list[str] = []
        for expanded in search_paths:
            searched.append(expanded)
            if not os.path.isdir(expanded):
                continue
            for entry in sorted(os.listdir(expanded)):
                if entry.startswith("appmanifest_") and entry.endswith(".acf"):
                    full_path = os.path.join(expanded, entry)
                    app_id = _parse_acf_appid(full_path)
                    name = _parse_acf_name(full_path)
                    if app_id is not None:
                        games.append({"app_id": app_id, "name": name or "unknown"})
        if not games:
            return {"games": [], "searched_paths": searched, "error": "no games found"}
        return {"games": games, "count": len(games)}

    @registry.tool(description=(
        "List native OSS GPU benchmarks (vkmark, glmark2) runnable in the "
        "guest without Steam. Use these when no Steam library is available."
    ))
    def list_native_benchmarks() -> dict:
        return {
            "benchmarks": [
                {"name": name, **info} for name, info in NATIVE_BENCHMARKS.items()
            ]
        }

    @registry.tool(description="Check whether a game has a known built-in benchmark mode.")
    def check_benchmark_support(app_id: int) -> dict:
        info = BENCHMARK_GAMES.get(app_id)
        if info is not None:
            return {
                "has_benchmark": True,
                "benchmark_args": info["benchmark_args"],
                "workload_profile": info["workload_profile"],
            }
        return {"has_benchmark": False, "benchmark_args": None, "workload_profile": None}

    @registry.tool(description="Return metadata about a game given its Steam app ID.")
    def get_game_metadata(app_id: int) -> dict:
        info = BENCHMARK_GAMES.get(app_id)
        if info is not None:
            return {
                "app_id": app_id,
                "name": info["name"],
                "has_benchmark": True,
                "workload_profile": info["workload_profile"],
            }
        return {
            "app_id": app_id,
            "name": "unknown",
            "has_benchmark": False,
            "workload_profile": "unknown",
        }
