from __future__ import annotations

import os
import subprocess
from typing import Any

from agents.common.tool_registry import ToolRegistry


def make_optimizer_tools(registry: ToolRegistry, kernel_src: str) -> None:
    """Register optimizer tools into the given registry."""

    @registry.tool(
        description="Read a source file relative to kernel_src or as an absolute path."
    )
    def read_source_file(path: str, start_line: int = 0, max_lines: int = 200) -> dict:
        if os.path.isabs(path):
            full_path = path
        else:
            full_path = os.path.join(kernel_src, path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            end_line = start_line + max_lines
            selected = lines[start_line:end_line]
            return {
                "content": "".join(selected),
                "total_lines": len(lines),
                "start_line": start_line,
                "lines_returned": len(selected),
            }
        except OSError as exc:
            return {"error": str(exc)}

    @registry.tool(description="Search kernel source files matching a pattern using grep.")
    def search_kernel_source(
        pattern: str, file_glob: str = "*.c", subdirectory: str = ""
    ) -> dict:
        search_dir = os.path.join(kernel_src, subdirectory) if subdirectory else kernel_src
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include", file_glob, pattern, search_dir],
                capture_output=True,
                text=True,
                timeout=30,
            )
            matches = (
                result.stdout.strip().split("\n") if result.stdout.strip() else []
            )
            return {"matches": matches, "count": len(matches)}
        except FileNotFoundError:
            return {"error": "grep not found"}
        except subprocess.TimeoutExpired:
            return {"error": "search timed out"}

    @registry.tool(
        description="Save a unified diff to the .crucible_patches/ directory under kernel_src."
    )
    def write_patch(filename: str, content: str) -> dict:
        patch_dir = os.path.join(kernel_src, ".crucible_patches")
        os.makedirs(patch_dir, exist_ok=True)
        patch_path = os.path.join(patch_dir, filename)
        try:
            with open(patch_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return {"status": "ok", "path": patch_path}
        except OSError as exc:
            return {"status": "error", "error": str(exc)}

    @registry.tool(
        description="Read the current sysctl value and apply a new one."
    )
    def apply_sysctl(key: str, value: str) -> dict:
        try:
            read_result = subprocess.run(
                ["sysctl", "-n", key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            old_value = read_result.stdout.strip()
            write_result = subprocess.run(
                ["sysctl", "-w", f"{key}={value}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if write_result.returncode != 0:
                return {
                    "error": write_result.stderr.strip(),
                    "old_value": old_value,
                }
            return {"old_value": old_value, "new_value": value, "status": "ok"}
        except FileNotFoundError:
            return {"error": "sysctl not found"}
        except subprocess.TimeoutExpired:
            return {"error": "sysctl timed out"}

    @registry.tool(description="List files in a directory under kernel_src.")
    def list_source_files(path: str = "", file_glob: str = "*.c") -> dict:
        search_dir = os.path.join(kernel_src, path) if path else kernel_src
        if not os.path.isdir(search_dir):
            return {"error": f"directory not found: {search_dir}", "files": []}
        files: list[str] = []
        for entry in sorted(os.listdir(search_dir)):
            full = os.path.join(search_dir, entry)
            if os.path.isdir(full):
                files.append(entry + "/")
            elif _matches_glob(entry, file_glob):
                files.append(entry)
        return {"files": files, "directory": search_dir}


def _matches_glob(filename: str, pattern: str) -> bool:
    """Simple glob matching for *.ext patterns."""
    if pattern.startswith("*."):
        return filename.endswith(pattern[1:])
    if pattern == "*":
        return True
    return filename == pattern
