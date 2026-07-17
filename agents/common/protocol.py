# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 David Vernet

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class TaskStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEEDS_INPUT = "needs_input"


class AgentConfig(BaseModel):
    model: str
    # Advisory under the Claude Agent SDK — the bundled CLI has no per-call
    # output-token cap. Tool loops are bounded by ClaudeAgentBase.MAX_TOOL_ROUNDS
    # and the orchestrator's subprocess timeout.
    max_tokens: int
    timeout_seconds: int
    # Forwarded to the CLI via CLAUDE_CODE_MAX_RETRIES.
    max_retries: int = 3


class TaskEnvelope(BaseModel):
    task_id: UUID
    agent: str
    context: dict[str, Any]
    config: AgentConfig


class ApiUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0


class ResultEnvelope(BaseModel):
    task_id: UUID
    status: TaskStatus
    result: dict[str, Any]
    usage: ApiUsage
    logs: list[str]
