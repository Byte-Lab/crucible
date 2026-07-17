# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

"""Profiler `run_benchmark` tool behaviour."""
from typing import Any

import pytest

from agents.common.tool_registry import ToolRegistry
from agents.profiler.tools import make_profiler_tools


class FakeGuestRpc:
    def __init__(self, response: dict[str, Any] | None = None, raise_with: Exception | None = None) -> None:
        self.response = response or {"status": "ok", "data": {"ops_per_sec": 12345.6}}
        self.raise_with = raise_with
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, cmd: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((cmd, args))
        if self.raise_with is not None:
            raise self.raise_with
        return self.response


def test_run_benchmark_dry_run_when_no_guest_rpc():
    registry = ToolRegistry()
    make_profiler_tools(registry, guest_rpc=None)
    result = registry.call("run_benchmark", {
        "name": "stress-ng",
        "args": ["--cpu", "4"],
        "duration_secs": 10,
    })
    assert result["status"] == "dry_run"
    assert "stress-ng" in result["message"]


def test_run_benchmark_forwards_to_guest_rpc():
    rpc = FakeGuestRpc(response={"status": "ok", "data": {"ops_per_sec": 1500.0}})
    registry = ToolRegistry()
    make_profiler_tools(registry, guest_rpc=rpc)
    result = registry.call("run_benchmark", {
        "name": "stress-ng",
        "args": ["--cpu", "4"],
        "duration_secs": 10,
    })
    assert result == {"status": "ok", "data": {"ops_per_sec": 1500.0}}
    assert rpc.calls == [(
        "run_benchmark",
        {"name": "stress-ng", "args": ["--cpu", "4"], "duration_secs": 10},
    )]


def test_run_benchmark_catches_rpc_failure():
    rpc = FakeGuestRpc(raise_with=ConnectionRefusedError("vm not up"))
    registry = ToolRegistry()
    make_profiler_tools(registry, guest_rpc=rpc)
    result = registry.call("run_benchmark", {
        "name": "stress-ng",
        "args": [],
        "duration_secs": 5,
    })
    assert result["status"] == "error"
    assert "vm not up" in result["error"]


def test_profiler_synthetic_user_message_instructs_run_benchmark():
    from uuid import uuid4
    from agents.common.protocol import AgentConfig, TaskEnvelope
    from agents.profiler.agent import ProfilerAgent

    task = TaskEnvelope(
        task_id=uuid4(),
        agent="profiler",
        context={
            "phase": "baseline",
            "game": "synthetic",
            "workload_kind": "synthetic",
            "benchmark_args": ["--cpu", "4"],
            "duration_secs": 15,
        },
        config=AgentConfig(model="m", max_tokens=1, timeout_seconds=1),
    )
    msg = ProfilerAgent().build_user_message(task)
    assert "synthetic workload" in msg
    assert "run_benchmark" in msg
    assert "stress-ng" in msg
    assert "duration_secs=15" in msg
    assert "psi_cpu_avg" in msg
    # No capture_perfetto in context -> no profiling step for this baseline.
    assert "start_profiling" not in msg
