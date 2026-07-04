from __future__ import annotations

import os
import subprocess

from agents.common.tool_registry import ToolRegistry

# The Agent SDK's CLI transport rejects any single JSON message over 1 MiB;
# one oversized tool result (an uncapped grep over the kernel tree) kills
# the agent mid-cycle. Mirror of the caps in agents/analyzer/tools.py.
MAX_TOOL_RESULT_BYTES = 200_000
MAX_SEARCH_MATCHES = 500


def make_kernel_read_tools(registry: ToolRegistry, kernel_src: str) -> None:
    """Register the read-only kernel navigation tools.

    Shared between the optimizer (which additionally gets edit/finalize)
    and the patch reviewer (which must NOT be able to edit — it audits).
    """

    @registry.tool(
        description="Read a source file relative to kernel_src or as an absolute path."
    )
    def read_source_file(path: str, start_line: int = 0, max_lines: int = 200) -> dict:
        full_path = path if os.path.isabs(path) else os.path.join(kernel_src, path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            end_line = start_line + max_lines
            selected = lines[start_line:end_line]
            content = "".join(selected)
            truncated = False
            if len(content) > MAX_TOOL_RESULT_BYTES:
                content = content[:MAX_TOOL_RESULT_BYTES]
                truncated = True
            return {
                "content": content,
                "total_lines": len(lines),
                "start_line": start_line,
                "lines_returned": len(selected),
                "truncated": truncated,
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
            truncated = len(matches) > MAX_SEARCH_MATCHES
            matches = [m[:500] for m in matches[:MAX_SEARCH_MATCHES]]
            size = 0
            capped: list[str] = []
            for m in matches:
                if size + len(m) > MAX_TOOL_RESULT_BYTES:
                    truncated = True
                    break
                capped.append(m)
                size += len(m)
            return {"matches": capped, "count": len(capped), "truncated": truncated}
        except FileNotFoundError:
            return {"error": "grep not found"}
        except subprocess.TimeoutExpired:
            return {"error": "search timed out"}


def make_optimizer_tools(registry: ToolRegistry, kernel_src: str) -> None:
    """Register optimizer tools into the given registry.

    The optimizer workflow is read → edit → finalize:

    1. `read_source_file` / `search_kernel_source` / `list_source_files`
       to navigate the tree.
    2. `edit_file` to replace exact text spans. Each call mutates the
       host tree in place.
    3. `finalize_patch` exactly once at the end: captures the full
       `git diff` of all edits into `.crucible_patches/<filename>` and
       reverts the working tree so the orchestrator can re-apply the
       canonical diff via `git apply`.

    Diffs come from git itself, so hunk headers are always well-formed —
    no more "corrupt patch" rejections from `git apply`.
    """
    make_kernel_read_tools(registry, kernel_src)

    @registry.tool(
        description=(
            "Replace one exact occurrence of old_text with new_text in a file under "
            "kernel_src. Fails if old_text is not present or appears more than once "
            "(use a longer, more specific old_text in that case). Edits stack across "
            "calls; capture them with finalize_patch when done."
        )
    )
    def edit_file(path: str, old_text: str, new_text: str) -> dict:
        full_path = path if os.path.isabs(path) else os.path.join(kernel_src, path)
        try:
            with open(full_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return {"status": "error", "error": f"read failed: {exc}"}
        occurrences = content.count(old_text)
        if occurrences == 0:
            return {
                "status": "error",
                "error": "old_text not found in file (check whitespace and exact characters)",
            }
        if occurrences > 1:
            return {
                "status": "error",
                "error": f"old_text matches {occurrences} places; extend it until unique",
            }
        new_content = content.replace(old_text, new_text, 1)
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(new_content)
        except OSError as exc:
            return {"status": "error", "error": f"write failed: {exc}"}
        return {"status": "ok", "path": full_path}

    @registry.tool(
        description=(
            "Capture all uncommitted edits in kernel_src as a unified diff in "
            ".crucible_patches/<filename>, then revert the working tree so the "
            "orchestrator can re-apply the diff cleanly. Call exactly once after "
            "all edit_file calls. Returns {status, path} or {status: error, error}."
        )
    )
    def finalize_patch(filename: str) -> dict:
        try:
            diff_result = subprocess.run(
                ["git", "-C", kernel_src, "diff"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            return {"status": "error", "error": "git not found"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "git diff timed out"}
        if diff_result.returncode != 0:
            return {
                "status": "error",
                "error": f"git diff failed: {diff_result.stderr.strip()}",
            }
        diff_text = diff_result.stdout
        if not diff_text.strip():
            return {
                "status": "error",
                "error": "no edits to capture; call edit_file first",
            }
        patch_dir = os.path.join(kernel_src, ".crucible_patches")
        try:
            os.makedirs(patch_dir, exist_ok=True)
        except OSError as exc:
            return {"status": "error", "error": f"mkdir failed: {exc}"}
        patch_path = os.path.join(patch_dir, filename)
        try:
            with open(patch_path, "w", encoding="utf-8") as fh:
                fh.write(diff_text)
        except OSError as exc:
            return {"status": "error", "error": f"write failed: {exc}"}
        try:
            revert = subprocess.run(
                ["git", "-C", kernel_src, "checkout", "--", "."],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "git checkout timed out"}
        if revert.returncode != 0:
            return {
                "status": "error",
                "error": f"git checkout failed: {revert.stderr.strip()}",
                "path": patch_path,
            }
        return {"status": "ok", "path": patch_path}

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
