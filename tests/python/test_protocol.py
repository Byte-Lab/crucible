import json
import uuid

from agents.common.protocol import (
    AgentConfig,
    ApiUsage,
    ResultEnvelope,
    TaskEnvelope,
    TaskStatus,
)
from guest.protocol import GuestCommand


def test_task_envelope_from_json():
    task_id = str(uuid.uuid4())
    raw = json.dumps({
        "task_id": task_id,
        "agent": "analyzer",
        "context": {"game_id": 1091500},
        "config": {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8192,
            "timeout_seconds": 300,
        },
    })
    task = TaskEnvelope.model_validate_json(raw)
    assert str(task.task_id) == task_id
    assert task.agent == "analyzer"
    assert task.config.max_tokens == 8192
    # Missing `max_retries` falls back to the schema default so older Rust
    # callers stay forward-compatible.
    assert task.config.max_retries == 3


def test_agent_config_accepts_max_retries():
    config = AgentConfig(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        timeout_seconds=300,
        max_retries=5,
    )
    assert config.max_retries == 5


def test_result_envelope_to_json():
    result = ResultEnvelope(
        task_id=uuid.uuid4(),
        status=TaskStatus.SUCCESS,
        result={"bottleneck": "kcompactd"},
        usage=ApiUsage(input_tokens=1234, output_tokens=567, api_calls=3),
        logs=["analyzed trace"],
    )
    raw = result.model_dump_json()
    parsed = json.loads(raw)
    assert parsed["status"] == "success"
    assert parsed["usage"]["api_calls"] == 3


def test_guest_command_run_benchmark_roundtrip():
    cmd = GuestCommand(
        cmd="run_benchmark",
        name="stress-ng",
        args=["--cpu", "4"],
        duration_secs=30,
    )
    raw = cmd.to_json(exclude_none=True)
    parsed = json.loads(raw)
    assert parsed == {
        "cmd": "run_benchmark",
        "args": ["--cpu", "4"],
        "name": "stress-ng",
        "duration_secs": 30,
    }
    restored = GuestCommand.from_json(raw)
    assert restored.cmd == "run_benchmark"
    assert restored.name == "stress-ng"
    assert restored.args == ["--cpu", "4"]
    assert restored.duration_secs == 30


def test_result_envelope_failure_status():
    result = ResultEnvelope(
        task_id=uuid.uuid4(),
        status=TaskStatus.FAILURE,
        result={"error": "timeout"},
        usage=ApiUsage(input_tokens=100, output_tokens=0, api_calls=1),
        logs=["agent timed out"],
    )
    raw = result.model_dump_json()
    parsed = json.loads(raw)
    assert parsed["status"] == "failure"
