from __future__ import annotations

import os
import re
from typing import Any

from agents.common.tool_registry import ToolRegistry

BENCHMARK_GAMES: dict[int, dict[str, Any]] = {
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

    @registry.tool(description="List installed Steam games by scanning the Steam library for appmanifest ACF files.")
    def list_steam_games(library_path: str = "~/.steam/steam/steamapps") -> dict:
        expanded = os.path.expanduser(library_path)
        games: list[dict[str, Any]] = []
        if not os.path.isdir(expanded):
            return {"games": [], "error": f"directory not found: {expanded}"}
        for entry in sorted(os.listdir(expanded)):
            if entry.startswith("appmanifest_") and entry.endswith(".acf"):
                full_path = os.path.join(expanded, entry)
                app_id = _parse_acf_appid(full_path)
                name = _parse_acf_name(full_path)
                if app_id is not None:
                    games.append({"app_id": app_id, "name": name or "unknown"})
        return {"games": games}

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
