import os
import tempfile
from agents.optimizer.tools import make_optimizer_tools
from agents.common.tool_registry import ToolRegistry


def test_optimizer_tools_registered():
    registry = ToolRegistry()
    make_optimizer_tools(registry, kernel_src="/tmp")
    names = [t["name"] for t in registry.tools]
    assert "read_source_file" in names
    assert "write_patch" in names
    assert "apply_sysctl" in names
    assert "search_kernel_source" in names


def test_write_patch():
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        make_optimizer_tools(registry, kernel_src=tmp)
        result = registry.call("write_patch", {
            "filename": "test.diff",
            "content": "--- a/kernel/sched/core.c\n+++ b/kernel/sched/core.c\n@@ -1 +1 @@\n-old\n+new\n",
        })
        assert result["status"] == "ok"
        assert os.path.exists(result["path"])


def test_read_source_file():
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        src_file = os.path.join(tmp, "test.c")
        with open(src_file, "w") as f:
            f.write("int main() { return 0; }\n")
        make_optimizer_tools(registry, kernel_src=tmp)
        result = registry.call("read_source_file", {"path": "test.c"})
        assert "int main" in result["content"]
