# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

from typing import Any

from agents.common.agent_base import AgentBase
from agents.common.protocol import ApiUsage, TaskEnvelope


class EchoAgent(AgentBase):
    """Test agent that echoes back the task context."""

    def execute(self, task: TaskEnvelope) -> tuple[dict[str, Any], ApiUsage]:
        self.log(f"echoing task {task.task_id}")
        return {"echo": task.context}, ApiUsage()


if __name__ == "__main__":
    EchoAgent().run()
