# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

import json
import io
import sys
import uuid

from agents.common.protocol import AgentConfig, TaskEnvelope, TaskStatus


def make_task(context: dict | None = None) -> str:
    task = TaskEnvelope(
        task_id=uuid.uuid4(),
        agent="echo",
        context=context or {"message": "hello"},
        config=AgentConfig(
            model="claude-sonnet-4-20250514",
            max_tokens=8192,
            timeout_seconds=300,
        ),
    )
    return task.model_dump_json()


def test_echo_agent_returns_input_as_output():
    from agents.echo.agent import EchoAgent

    task_json = make_task({"message": "hello crucible"})

    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = io.TextIOWrapper(io.BytesIO(task_json.encode()))
    captured = io.StringIO()
    sys.stdout = captured

    try:
        agent = EchoAgent()
        agent.run()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout

    output = captured.getvalue()
    result = json.loads(output)
    assert result["status"] == "success"
    assert result["result"]["echo"]["message"] == "hello crucible"


def test_agent_base_handles_execute_exception():
    from agents.common.agent_base import AgentBase

    class FailingAgent(AgentBase):
        def execute(self, task):
            raise RuntimeError("something broke")

    task_json = make_task()

    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = io.TextIOWrapper(io.BytesIO(task_json.encode()))
    captured = io.StringIO()
    sys.stdout = captured

    try:
        agent = FailingAgent()
        agent.run()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout

    output = captured.getvalue()
    result = json.loads(output)
    assert result["status"] == "failure"
    assert "something broke" in result["result"]["error"]
