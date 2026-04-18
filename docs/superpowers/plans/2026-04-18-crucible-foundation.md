# Crucible Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational layer of Crucible -- the Rust orchestrator skeleton that can load config, persist state to SQLite, spawn Python agent processes, send them tasks via stdin, and receive results via stdout.

**Architecture:** Rust workspace with two crates (crucible-common for shared types, crucible-orchestrator for the daemon). Python package for agents with a base class handling the stdin/stdout JSON protocol. An echo test agent proves the full round-trip works. This foundation is the scaffolding that all subsequent plans (VM management, real agents, orchestration loop) build on.

**Tech Stack:** Rust (tokio, rusqlite, serde, toml, clap, tracing), Python 3.12+ (pydantic, anthropic SDK), pytest

**Spec reference:** `docs/superpowers/specs/2026-04-12-crucible-design.md`

**Plan series:**
- **Plan 1 (this plan):** Foundation
- Plan 2: VM management (virtme-ng, guest agent, vsock)
- Plan 3: Core agents (game selector, profiler, analyzer, optimizer)
- Plan 4: Orchestration loop (state machine, evaluator, closed loop)

---

## File Map

### Rust

| File | Responsibility |
|------|---------------|
| `Cargo.toml` | Workspace root |
| `crates/crucible-common/Cargo.toml` | Common crate manifest |
| `crates/crucible-common/src/lib.rs` | Re-exports |
| `crates/crucible-common/src/protocol.rs` | TaskEnvelope, ResultEnvelope, AgentConfig JSON types |
| `crates/crucible-common/src/metrics.rs` | Metric types (FPS, PSI, frame times) |
| `crates/crucible-orchestrator/Cargo.toml` | Orchestrator crate manifest |
| `crates/crucible-orchestrator/src/main.rs` | CLI entry point, tokio runtime |
| `crates/crucible-orchestrator/src/config.rs` | TOML config loading and validation |
| `crates/crucible-orchestrator/src/db.rs` | SQLite schema, migrations, CRUD operations |
| `crates/crucible-orchestrator/src/agent_runner.rs` | Spawn Python agents, stdin/stdout IPC, timeouts |

### Python

| File | Responsibility |
|------|---------------|
| `pyproject.toml` | Python project config |
| `agents/__init__.py` | Package root |
| `agents/common/__init__.py` | Common package |
| `agents/common/protocol.py` | TaskEnvelope, ResultEnvelope pydantic models |
| `agents/common/agent_base.py` | Base class: read stdin, call execute(), write stdout |
| `agents/echo/__init__.py` | Echo agent package |
| `agents/echo/agent.py` | Echo agent: returns input as output (for testing) |

### Config

| File | Responsibility |
|------|---------------|
| `config/crucible.toml` | Default configuration file |

### Tests

| File | Responsibility |
|------|---------------|
| `crates/crucible-orchestrator/tests/integration_test.rs` | Rust integration test: config -> db -> agent runner -> echo agent round-trip |
| `tests/python/test_protocol.py` | Python unit test: protocol serialization |
| `tests/python/test_agent_base.py` | Python unit test: agent base stdin/stdout handling |

---

## Task 1: Project Scaffolding

**Files:**
- Create: `Cargo.toml`
- Create: `crates/crucible-common/Cargo.toml`
- Create: `crates/crucible-common/src/lib.rs`
- Create: `crates/crucible-orchestrator/Cargo.toml`
- Create: `crates/crucible-orchestrator/src/main.rs`
- Create: `pyproject.toml`
- Create: `agents/__init__.py`
- Create: `agents/common/__init__.py`

- [ ] **Step 1: Create Rust workspace root**

```toml
# Cargo.toml
[workspace]
members = ["crates/*"]
resolver = "2"

[workspace.dependencies]
tokio = { version = "1", features = ["full"] }
rusqlite = { version = "0.31", features = ["bundled"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
toml = "0.8"
uuid = { version = "1", features = ["v4", "serde"] }
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
clap = { version = "4", features = ["derive"] }
thiserror = "2"
anyhow = "1"
```

- [ ] **Step 2: Create crucible-common crate**

```toml
# crates/crucible-common/Cargo.toml
[package]
name = "crucible-common"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { workspace = true }
serde_json = { workspace = true }
uuid = { workspace = true }
thiserror = { workspace = true }
```

```rust
// crates/crucible-common/src/lib.rs
pub mod metrics;
pub mod protocol;
```

- [ ] **Step 3: Create crucible-orchestrator crate**

```toml
# crates/crucible-orchestrator/Cargo.toml
[package]
name = "crucible-orchestrator"
version = "0.1.0"
edition = "2021"

[dependencies]
crucible-common = { path = "../crucible-common" }
tokio = { workspace = true }
rusqlite = { workspace = true }
serde = { workspace = true }
serde_json = { workspace = true }
toml = { workspace = true }
uuid = { workspace = true }
tracing = { workspace = true }
tracing-subscriber = { workspace = true }
clap = { workspace = true }
thiserror = { workspace = true }
anyhow = { workspace = true }
```

```rust
// crates/crucible-orchestrator/src/main.rs
fn main() {
    println!("crucible-orchestrator");
}
```

- [ ] **Step 4: Create Python package skeleton**

```toml
# pyproject.toml
[project]
name = "crucible-agents"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=0.40",
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff>=0.4"]

[tool.pytest.ini_options]
testpaths = ["tests/python"]

[tool.ruff]
target-version = "py312"
```

```python
# agents/__init__.py
```

```python
# agents/common/__init__.py
```

- [ ] **Step 5: Verify build**

Run: `cargo build`
Expected: Compiles with no errors.

Run: `python3 -c "import agents; print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add Cargo.toml crates/ pyproject.toml agents/
git commit -m "chore: scaffold Rust workspace and Python agent package"
```

---

## Task 2: Protocol Types (Rust)

**Files:**
- Create: `crates/crucible-common/src/protocol.rs`
- Create: `crates/crucible-common/src/metrics.rs`

- [ ] **Step 1: Write tests for protocol types**

```rust
// crates/crucible-common/src/protocol.rs

// ... (types will go above tests)

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_envelope_roundtrip() {
        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Analyzer,
            context: serde_json::json!({"game_id": 1091500}),
            config: AgentConfig {
                model: "claude-sonnet-4-6-20250414".to_string(),
                max_tokens: 8192,
                timeout_seconds: 300,
            },
        };
        let json = serde_json::to_string(&task).unwrap();
        let parsed: TaskEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.task_id, task.task_id);
        assert_eq!(parsed.agent, AgentName::Analyzer);
        assert_eq!(parsed.config.max_tokens, 8192);
    }

    #[test]
    fn result_envelope_success() {
        let result = ResultEnvelope {
            task_id: uuid::Uuid::new_v4(),
            status: TaskStatus::Success,
            result: serde_json::json!({"bottleneck": "kcompactd"}),
            usage: ApiUsage {
                input_tokens: 1234,
                output_tokens: 567,
                api_calls: 3,
            },
            logs: vec!["analyzed trace".to_string()],
        };
        let json = serde_json::to_string(&result).unwrap();
        let parsed: ResultEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.status, TaskStatus::Success);
        assert_eq!(parsed.usage.api_calls, 3);
    }

    #[test]
    fn agent_name_serializes_as_snake_case() {
        let name = AgentName::GameSelector;
        let json = serde_json::to_value(&name).unwrap();
        assert_eq!(json, serde_json::json!("game_selector"));
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p crucible-common`
Expected: FAIL -- types not defined yet.

- [ ] **Step 3: Implement protocol types**

```rust
// crates/crucible-common/src/protocol.rs
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentName {
    GameSelector,
    GamePlayer,
    Profiler,
    Analyzer,
    Optimizer,
    Echo,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentConfig {
    pub model: String,
    pub max_tokens: u32,
    pub timeout_seconds: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskEnvelope {
    pub task_id: Uuid,
    pub agent: AgentName,
    pub context: serde_json::Value,
    pub config: AgentConfig,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Success,
    Failure,
    NeedsInput,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiUsage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub api_calls: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResultEnvelope {
    pub task_id: Uuid,
    pub status: TaskStatus,
    pub result: serde_json::Value,
    pub usage: ApiUsage,
    pub logs: Vec<String>,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p crucible-common`
Expected: All 3 tests pass.

- [ ] **Step 5: Add metric types**

```rust
// crates/crucible-common/src/metrics.rs
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FrameMetrics {
    pub fps_avg: f64,
    pub fps_p1: f64,
    pub frame_time_p50_ms: f64,
    pub frame_time_p95_ms: f64,
    pub frame_time_p99_ms: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PsiMetrics {
    /// PSI avg10 value (percentage of time stalled)
    pub cpu_avg: f64,
    pub memory_avg: f64,
    pub io_avg: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CgroupPsiMetrics {
    pub cgroup_path: String,
    pub psi: PsiMetrics,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemMetrics {
    pub context_switches_per_sec: f64,
    pub page_faults_per_sec: f64,
    pub gpu_utilization_pct: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunMeasurement {
    pub frame: FrameMetrics,
    pub system_psi: PsiMetrics,
    pub cgroup_psi: Vec<CgroupPsiMetrics>,
    pub system: SystemMetrics,
    pub custom: serde_json::Value,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_measurement_roundtrip() {
        let m = RunMeasurement {
            frame: FrameMetrics {
                fps_avg: 60.0,
                fps_p1: 45.0,
                frame_time_p50_ms: 16.6,
                frame_time_p95_ms: 20.0,
                frame_time_p99_ms: 25.0,
            },
            system_psi: PsiMetrics {
                cpu_avg: 0.5,
                memory_avg: 1.2,
                io_avg: 0.1,
            },
            cgroup_psi: vec![CgroupPsiMetrics {
                cgroup_path: "crucible/game".to_string(),
                psi: PsiMetrics {
                    cpu_avg: 2.0,
                    memory_avg: 3.5,
                    io_avg: 0.0,
                },
            }],
            system: SystemMetrics {
                context_switches_per_sec: 5000.0,
                page_faults_per_sec: 120.0,
                gpu_utilization_pct: 85.0,
            },
            custom: serde_json::json!({}),
        };
        let json = serde_json::to_string(&m).unwrap();
        let parsed: RunMeasurement = serde_json::from_str(&json).unwrap();
        assert!((parsed.frame.fps_avg - 60.0).abs() < f64::EPSILON);
        assert_eq!(parsed.cgroup_psi.len(), 1);
        assert_eq!(parsed.cgroup_psi[0].cgroup_path, "crucible/game");
    }
}
```

- [ ] **Step 6: Run all tests**

Run: `cargo test -p crucible-common`
Expected: All 4 tests pass.

- [ ] **Step 7: Commit**

```bash
git add crates/crucible-common/src/
git commit -m "feat: add protocol and metric types for agent communication"
```

---

## Task 3: Protocol Types (Python)

**Files:**
- Create: `agents/common/protocol.py`
- Create: `tests/python/__init__.py`
- Create: `tests/python/test_protocol.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/python/__init__.py
```

```python
# tests/python/test_protocol.py
import json
import uuid

from agents.common.protocol import (
    AgentConfig,
    ApiUsage,
    ResultEnvelope,
    TaskEnvelope,
    TaskStatus,
)


def test_task_envelope_from_json():
    task_id = str(uuid.uuid4())
    raw = json.dumps({
        "task_id": task_id,
        "agent": "analyzer",
        "context": {"game_id": 1091500},
        "config": {
            "model": "claude-sonnet-4-6-20250414",
            "max_tokens": 8192,
            "timeout_seconds": 300,
        },
    })
    task = TaskEnvelope.model_validate_json(raw)
    assert str(task.task_id) == task_id
    assert task.agent == "analyzer"
    assert task.config.max_tokens == 8192


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/python/test_protocol.py -v`
Expected: FAIL -- `agents.common.protocol` does not exist.

- [ ] **Step 3: Implement protocol types**

```python
# agents/common/protocol.py
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
    max_tokens: int
    timeout_seconds: int


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/python/test_protocol.py -v`
Expected: All 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/common/protocol.py tests/python/
git commit -m "feat: add Python protocol types mirroring Rust definitions"
```

---

## Task 4: Python Agent Base Class

**Files:**
- Create: `agents/common/agent_base.py`
- Create: `agents/echo/__init__.py`
- Create: `agents/echo/agent.py`
- Create: `tests/python/test_agent_base.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/python/test_agent_base.py
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
            model="claude-sonnet-4-6-20250414",
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/python/test_agent_base.py -v`
Expected: FAIL -- modules not defined.

- [ ] **Step 3: Implement AgentBase**

```python
# agents/common/agent_base.py
import json
import sys
import traceback
from typing import Any

from agents.common.protocol import (
    ApiUsage,
    ResultEnvelope,
    TaskEnvelope,
    TaskStatus,
)


class AgentBase:
    """Base class for all Crucible agents.

    Handles the stdin/stdout JSON protocol. Subclasses implement execute().
    """

    def run(self) -> None:
        task_json = sys.stdin.read()
        task = TaskEnvelope.model_validate_json(task_json)

        try:
            result_data, usage = self.execute(task)
            result = ResultEnvelope(
                task_id=task.task_id,
                status=TaskStatus.SUCCESS,
                result=result_data,
                usage=usage,
                logs=self._logs,
            )
        except Exception as exc:
            result = ResultEnvelope(
                task_id=task.task_id,
                status=TaskStatus.FAILURE,
                result={"error": str(exc), "traceback": traceback.format_exc()},
                usage=ApiUsage(),
                logs=self._logs,
            )

        sys.stdout.write(result.model_dump_json())
        sys.stdout.flush()

    def execute(self, task: TaskEnvelope) -> tuple[dict[str, Any], ApiUsage]:
        raise NotImplementedError("Subclasses must implement execute()")

    def __init__(self) -> None:
        self._logs: list[str] = []

    def log(self, message: str) -> None:
        self._logs.append(message)
```

- [ ] **Step 4: Implement EchoAgent**

```python
# agents/echo/__init__.py
```

```python
# agents/echo/agent.py
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/python/test_agent_base.py -v`
Expected: Both tests pass.

- [ ] **Step 6: Commit**

```bash
git add agents/ tests/python/test_agent_base.py
git commit -m "feat: add agent base class and echo test agent"
```

---

## Task 5: Configuration System

**Files:**
- Create: `crates/crucible-orchestrator/src/config.rs`
- Create: `config/crucible.toml`
- Modify: `crates/crucible-orchestrator/src/main.rs`

- [ ] **Step 1: Write the failing test**

```rust
// crates/crucible-orchestrator/src/config.rs

// ... (types go above)

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn parse_minimal_config() {
        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/crucible/state.db"
            artifact_dir = "/tmp/crucible/artifacts"

            [vm]
            kernel_src = "/home/void/upstream/questing"
            guest_rootfs = "/home/void/.crucible/rootfs"
            vfio_device = "0a:00.0"

            [measurement]

            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.orchestrator.db_path, "/tmp/crucible/state.db");
        assert_eq!(config.vm.kernel_src, "/home/void/upstream/questing");
        assert_eq!(config.vm.memory, "16G"); // default
        assert_eq!(config.measurement.runs_per_phase, 5); // default
        assert_eq!(config.agents.model, "claude-sonnet-4-6-20250414"); // default
    }

    #[test]
    fn parse_full_config_from_file() {
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        write!(
            tmp,
            r#"
            [orchestrator]
            db_path = "~/.crucible/state.db"
            artifact_dir = "~/.crucible/artifacts"
            max_cycles = 10
            cycle_cooldown_secs = 120

            [vm]
            kernel_src = "/home/void/upstream/questing"
            guest_rootfs = "/home/void/.crucible/rootfs"
            memory = "32G"
            cpus = 16
            vfio_device = "0a:00.0"
            boot_timeout_secs = 120
            vsock_cid = 5

            [measurement]
            runs_per_phase = 10
            warmup_runs = 2
            significance_threshold = 0.01
            effect_size_threshold = 0.8
            max_stddev_pct = 5

            [agents]
            model = "claude-opus-4-6-20250414"
            max_retries = 5
            timeout_secs = 600

            [agents.optimizer]
            max_attempts_per_bottleneck = 5
            allowed_layers = ["kernel", "tuning"]

            [agents.game_player]
            enabled = true
            "#
        )
        .unwrap();

        let config = CrucibleConfig::from_file(tmp.path()).unwrap();
        assert_eq!(config.vm.memory, "32G");
        assert_eq!(config.vm.cpus, 16);
        assert_eq!(config.measurement.runs_per_phase, 10);
        assert_eq!(config.agents.model, "claude-opus-4-6-20250414");
        assert_eq!(config.agents.optimizer.max_attempts_per_bottleneck, 5);
        assert_eq!(config.agents.optimizer.allowed_layers, vec!["kernel", "tuning"]);
        assert!(config.agents.game_player.enabled);
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p crucible-orchestrator config`
Expected: FAIL -- types not defined.

- [ ] **Step 3: Add tempfile dev-dependency**

```toml
# Add to crates/crucible-orchestrator/Cargo.toml under [dev-dependencies]
[dev-dependencies]
tempfile = "3"
```

- [ ] **Step 4: Implement config types**

```rust
// crates/crucible-orchestrator/src/config.rs
use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::Path;

#[derive(Debug, Deserialize)]
pub struct CrucibleConfig {
    pub orchestrator: OrchestratorConfig,
    pub vm: VmConfig,
    #[serde(default)]
    pub measurement: MeasurementConfig,
    #[serde(default)]
    pub agents: AgentsConfig,
}

#[derive(Debug, Deserialize)]
pub struct OrchestratorConfig {
    pub db_path: String,
    pub artifact_dir: String,
    #[serde(default)]
    pub max_cycles: u64,
    #[serde(default = "default_cooldown")]
    pub cycle_cooldown_secs: u64,
}

#[derive(Debug, Deserialize)]
pub struct VmConfig {
    pub kernel_src: String,
    pub guest_rootfs: String,
    #[serde(default = "default_memory")]
    pub memory: String,
    #[serde(default = "default_cpus")]
    pub cpus: u32,
    pub vfio_device: String,
    #[serde(default = "default_boot_timeout")]
    pub boot_timeout_secs: u64,
    #[serde(default = "default_vsock_cid")]
    pub vsock_cid: u32,
}

#[derive(Debug, Deserialize)]
pub struct MeasurementConfig {
    #[serde(default = "default_runs_per_phase")]
    pub runs_per_phase: u32,
    #[serde(default = "default_warmup_runs")]
    pub warmup_runs: u32,
    #[serde(default = "default_significance")]
    pub significance_threshold: f64,
    #[serde(default = "default_effect_size")]
    pub effect_size_threshold: f64,
    #[serde(default = "default_max_stddev")]
    pub max_stddev_pct: f64,
}

#[derive(Debug, Deserialize)]
pub struct AgentsConfig {
    #[serde(default = "default_model")]
    pub model: String,
    #[serde(default = "default_max_retries")]
    pub max_retries: u32,
    #[serde(default = "default_timeout")]
    pub timeout_secs: u64,
    #[serde(default)]
    pub optimizer: OptimizerConfig,
    #[serde(default)]
    pub game_player: GamePlayerConfig,
}

#[derive(Debug, Deserialize)]
pub struct OptimizerConfig {
    #[serde(default = "default_max_attempts")]
    pub max_attempts_per_bottleneck: u32,
    #[serde(default = "default_allowed_layers")]
    pub allowed_layers: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct GamePlayerConfig {
    #[serde(default)]
    pub enabled: bool,
}

impl CrucibleConfig {
    pub fn from_file(path: &Path) -> Result<Self> {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read config: {}", path.display()))?;
        toml::from_str(&content)
            .with_context(|| format!("failed to parse config: {}", path.display()))
    }
}

fn default_cooldown() -> u64 { 60 }
fn default_memory() -> String { "16G".to_string() }
fn default_cpus() -> u32 { 8 }
fn default_boot_timeout() -> u64 { 60 }
fn default_vsock_cid() -> u32 { 3 }
fn default_runs_per_phase() -> u32 { 5 }
fn default_warmup_runs() -> u32 { 1 }
fn default_significance() -> f64 { 0.05 }
fn default_effect_size() -> f64 { 0.5 }
fn default_max_stddev() -> f64 { 10.0 }
fn default_model() -> String { "claude-sonnet-4-6-20250414".to_string() }
fn default_max_retries() -> u32 { 3 }
fn default_timeout() -> u64 { 300 }
fn default_max_attempts() -> u32 { 3 }
fn default_allowed_layers() -> Vec<String> {
    vec!["kernel".to_string(), "userspace".to_string(), "tuning".to_string()]
}

impl Default for MeasurementConfig {
    fn default() -> Self {
        Self {
            runs_per_phase: default_runs_per_phase(),
            warmup_runs: default_warmup_runs(),
            significance_threshold: default_significance(),
            effect_size_threshold: default_effect_size(),
            max_stddev_pct: default_max_stddev(),
        }
    }
}

impl Default for AgentsConfig {
    fn default() -> Self {
        Self {
            model: default_model(),
            max_retries: default_max_retries(),
            timeout_secs: default_timeout(),
            optimizer: OptimizerConfig::default(),
            game_player: GamePlayerConfig::default(),
        }
    }
}

impl Default for OptimizerConfig {
    fn default() -> Self {
        Self {
            max_attempts_per_bottleneck: default_max_attempts(),
            allowed_layers: default_allowed_layers(),
        }
    }
}

impl Default for GamePlayerConfig {
    fn default() -> Self {
        Self { enabled: false }
    }
}
```

- [ ] **Step 5: Create default config file**

```toml
# config/crucible.toml
[orchestrator]
db_path = "~/.crucible/state.db"
artifact_dir = "~/.crucible/artifacts"
max_cycles = 0              # 0 = unlimited
cycle_cooldown_secs = 60

[vm]
kernel_src = "/home/void/upstream/questing"
guest_rootfs = "/home/void/.crucible/rootfs"
memory = "16G"
cpus = 8
vfio_device = "0a:00.0"    # 7900 XT PCI address
boot_timeout_secs = 60
vsock_cid = 3

[measurement]
runs_per_phase = 5
warmup_runs = 1
significance_threshold = 0.05
effect_size_threshold = 0.5
max_stddev_pct = 10

[agents]
model = "claude-sonnet-4-6-20250414"
max_retries = 3
timeout_secs = 300

[agents.optimizer]
max_attempts_per_bottleneck = 3
allowed_layers = ["kernel", "userspace", "tuning"]

[agents.game_player]
enabled = false
```

- [ ] **Step 6: Wire config into main.rs**

```rust
// crates/crucible-orchestrator/src/main.rs
mod config;

use clap::Parser;
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "crucible-orchestrator")]
#[command(about = "Agentic Linux gaming performance optimization")]
struct Cli {
    /// Path to configuration file
    #[arg(short, long, default_value = "config/crucible.toml")]
    config: PathBuf,
}

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "crucible_orchestrator=info".into()),
        )
        .init();

    let cli = Cli::parse();
    let config = config::CrucibleConfig::from_file(&cli.config)?;
    tracing::info!(db = %config.orchestrator.db_path, "loaded configuration");

    Ok(())
}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cargo test -p crucible-orchestrator config`
Expected: Both tests pass.

- [ ] **Step 8: Commit**

```bash
git add crates/crucible-orchestrator/src/ config/
git commit -m "feat: add TOML configuration system with defaults"
```

---

## Task 6: SQLite Persistence Layer

**Files:**
- Create: `crates/crucible-orchestrator/src/db.rs`
- Modify: `crates/crucible-orchestrator/src/main.rs`

- [ ] **Step 1: Write the failing tests**

```rust
// crates/crucible-orchestrator/src/db.rs

// ... (implementation goes above)

#[cfg(test)]
mod tests {
    use super::*;

    fn test_db() -> Database {
        Database::open_in_memory().unwrap()
    }

    #[test]
    fn create_and_get_cycle() {
        let db = test_db();
        let id = db
            .create_cycle("shadow_of_the_tomb_raider", 1091500)
            .unwrap();

        let cycle = db.get_cycle(id).unwrap();
        assert_eq!(cycle.game_name, "shadow_of_the_tomb_raider");
        assert_eq!(cycle.game_app_id, 1091500);
        assert_eq!(cycle.status, "select_game");
    }

    #[test]
    fn update_cycle_status() {
        let db = test_db();
        let id = db.create_cycle("cyberpunk_2077", 1091500).unwrap();
        db.update_cycle_status(id, "baseline_measurement").unwrap();

        let cycle = db.get_cycle(id).unwrap();
        assert_eq!(cycle.status, "baseline_measurement");
    }

    #[test]
    fn insert_and_query_measurement() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        db.insert_measurement(cycle_id, "baseline", 60.0, 45.0, 25.0, 0.5, 1.2)
            .unwrap();
        db.insert_measurement(cycle_id, "baseline", 62.0, 47.0, 24.0, 0.4, 1.1)
            .unwrap();

        let measurements = db.get_measurements(cycle_id, "baseline").unwrap();
        assert_eq!(measurements.len(), 2);
        assert!((measurements[0].fps_avg - 60.0).abs() < f64::EPSILON);
        assert!((measurements[1].fps_avg - 62.0).abs() < f64::EPSILON);
    }

    #[test]
    fn insert_and_get_patch() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        let patch_id = db
            .insert_patch(cycle_id, "kernel", "/tmp/patches/001.diff")
            .unwrap();

        let patch = db.get_patch(patch_id).unwrap();
        assert_eq!(patch.layer, "kernel");
        assert_eq!(patch.diff_path, "/tmp/patches/001.diff");
        assert!(patch.reverted_at.is_none());
    }

    #[test]
    fn mark_patch_reverted() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        let patch_id = db
            .insert_patch(cycle_id, "kernel", "/tmp/patches/001.diff")
            .unwrap();
        db.mark_patch_reverted(patch_id).unwrap();

        let patch = db.get_patch(patch_id).unwrap();
        assert!(patch.reverted_at.is_some());
    }

    #[test]
    fn insert_evaluation() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        db.insert_evaluation(cycle_id, "frame_time_p99", 25.0, 22.0, -12.0, "accept")
            .unwrap();

        let evals = db.get_evaluations(cycle_id).unwrap();
        assert_eq!(evals.len(), 1);
        assert_eq!(evals[0].metric, "frame_time_p99");
        assert_eq!(evals[0].verdict, "accept");
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p crucible-orchestrator db`
Expected: FAIL -- `Database` not defined.

- [ ] **Step 3: Implement Database struct and schema migration**

```rust
// crates/crucible-orchestrator/src/db.rs
use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use std::path::Path;

const SCHEMA: &str = r#"
    CREATE TABLE IF NOT EXISTS cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_name TEXT NOT NULL,
        game_app_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'select_game',
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS measurements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL REFERENCES cycles(id),
        phase TEXT NOT NULL,
        fps_avg REAL NOT NULL,
        fps_p1 REAL NOT NULL,
        frame_time_p99_ms REAL NOT NULL,
        psi_cpu_avg REAL NOT NULL,
        psi_memory_avg REAL NOT NULL,
        custom_json TEXT NOT NULL DEFAULT '{}',
        recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS patches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL REFERENCES cycles(id),
        layer TEXT NOT NULL,
        diff_path TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now')),
        reverted_at TEXT
    );

    CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL REFERENCES cycles(id),
        metric TEXT NOT NULL,
        baseline_value REAL NOT NULL,
        comparison_value REAL NOT NULL,
        delta_pct REAL NOT NULL,
        verdict TEXT NOT NULL,
        evaluated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_measurements_cycle ON measurements(cycle_id, phase);
    CREATE INDEX IF NOT EXISTS idx_patches_cycle ON patches(cycle_id);
    CREATE INDEX IF NOT EXISTS idx_evaluations_cycle ON evaluations(cycle_id);
"#;

pub struct Database {
    conn: Connection,
}

#[derive(Debug)]
pub struct Cycle {
    pub id: i64,
    pub game_name: String,
    pub game_app_id: i64,
    pub status: String,
    pub started_at: String,
    pub completed_at: Option<String>,
}

#[derive(Debug)]
pub struct Measurement {
    pub id: i64,
    pub cycle_id: i64,
    pub phase: String,
    pub fps_avg: f64,
    pub fps_p1: f64,
    pub frame_time_p99_ms: f64,
    pub psi_cpu_avg: f64,
    pub psi_memory_avg: f64,
}

#[derive(Debug)]
pub struct Patch {
    pub id: i64,
    pub cycle_id: i64,
    pub layer: String,
    pub diff_path: String,
    pub applied_at: String,
    pub reverted_at: Option<String>,
}

#[derive(Debug)]
pub struct Evaluation {
    pub id: i64,
    pub cycle_id: i64,
    pub metric: String,
    pub baseline_value: f64,
    pub comparison_value: f64,
    pub delta_pct: f64,
    pub verdict: String,
}

impl Database {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("failed to create db directory: {}", parent.display()))?;
        }
        let conn = Connection::open(path)
            .with_context(|| format!("failed to open database: {}", path.display()))?;
        let db = Self { conn };
        db.migrate()?;
        Ok(db)
    }

    pub fn open_in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory()?;
        let db = Self { conn };
        db.migrate()?;
        Ok(db)
    }

    fn migrate(&self) -> Result<()> {
        self.conn
            .execute_batch(SCHEMA)
            .context("failed to run schema migration")
    }

    pub fn create_cycle(&self, game_name: &str, game_app_id: i64) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO cycles (game_name, game_app_id) VALUES (?1, ?2)",
            params![game_name, game_app_id],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_cycle(&self, id: i64) -> Result<Cycle> {
        self.conn
            .query_row(
                "SELECT id, game_name, game_app_id, status, started_at, completed_at FROM cycles WHERE id = ?1",
                params![id],
                |row| {
                    Ok(Cycle {
                        id: row.get(0)?,
                        game_name: row.get(1)?,
                        game_app_id: row.get(2)?,
                        status: row.get(3)?,
                        started_at: row.get(4)?,
                        completed_at: row.get(5)?,
                    })
                },
            )
            .context("failed to get cycle")
    }

    pub fn update_cycle_status(&self, id: i64, status: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE cycles SET status = ?1 WHERE id = ?2",
            params![status, id],
        )?;
        Ok(())
    }

    pub fn insert_measurement(
        &self,
        cycle_id: i64,
        phase: &str,
        fps_avg: f64,
        fps_p1: f64,
        frame_time_p99_ms: f64,
        psi_cpu_avg: f64,
        psi_memory_avg: f64,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO measurements (cycle_id, phase, fps_avg, fps_p1, frame_time_p99_ms, psi_cpu_avg, psi_memory_avg) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![cycle_id, phase, fps_avg, fps_p1, frame_time_p99_ms, psi_cpu_avg, psi_memory_avg],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_measurements(&self, cycle_id: i64, phase: &str) -> Result<Vec<Measurement>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, cycle_id, phase, fps_avg, fps_p1, frame_time_p99_ms, psi_cpu_avg, psi_memory_avg FROM measurements WHERE cycle_id = ?1 AND phase = ?2 ORDER BY id",
        )?;
        let rows = stmt.query_map(params![cycle_id, phase], |row| {
            Ok(Measurement {
                id: row.get(0)?,
                cycle_id: row.get(1)?,
                phase: row.get(2)?,
                fps_avg: row.get(3)?,
                fps_p1: row.get(4)?,
                frame_time_p99_ms: row.get(5)?,
                psi_cpu_avg: row.get(6)?,
                psi_memory_avg: row.get(7)?,
            })
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .context("failed to collect measurements")
    }

    pub fn insert_patch(&self, cycle_id: i64, layer: &str, diff_path: &str) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO patches (cycle_id, layer, diff_path) VALUES (?1, ?2, ?3)",
            params![cycle_id, layer, diff_path],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_patch(&self, id: i64) -> Result<Patch> {
        self.conn
            .query_row(
                "SELECT id, cycle_id, layer, diff_path, applied_at, reverted_at FROM patches WHERE id = ?1",
                params![id],
                |row| {
                    Ok(Patch {
                        id: row.get(0)?,
                        cycle_id: row.get(1)?,
                        layer: row.get(2)?,
                        diff_path: row.get(3)?,
                        applied_at: row.get(4)?,
                        reverted_at: row.get(5)?,
                    })
                },
            )
            .context("failed to get patch")
    }

    pub fn mark_patch_reverted(&self, id: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE patches SET reverted_at = datetime('now') WHERE id = ?1",
            params![id],
        )?;
        Ok(())
    }

    pub fn insert_evaluation(
        &self,
        cycle_id: i64,
        metric: &str,
        baseline_value: f64,
        comparison_value: f64,
        delta_pct: f64,
        verdict: &str,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO evaluations (cycle_id, metric, baseline_value, comparison_value, delta_pct, verdict) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![cycle_id, metric, baseline_value, comparison_value, delta_pct, verdict],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_evaluations(&self, cycle_id: i64) -> Result<Vec<Evaluation>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, cycle_id, metric, baseline_value, comparison_value, delta_pct, verdict FROM evaluations WHERE cycle_id = ?1 ORDER BY id",
        )?;
        let rows = stmt.query_map(params![cycle_id], |row| {
            Ok(Evaluation {
                id: row.get(0)?,
                cycle_id: row.get(1)?,
                metric: row.get(2)?,
                baseline_value: row.get(3)?,
                comparison_value: row.get(4)?,
                delta_pct: row.get(5)?,
                verdict: row.get(6)?,
            })
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .context("failed to collect evaluations")
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p crucible-orchestrator db`
Expected: All 6 tests pass.

- [ ] **Step 5: Add db module to main.rs**

Add `mod db;` to the top of `crates/crucible-orchestrator/src/main.rs` after `mod config;`.

- [ ] **Step 6: Commit**

```bash
git add crates/crucible-orchestrator/src/db.rs crates/crucible-orchestrator/src/main.rs
git commit -m "feat: add SQLite persistence layer with schema and CRUD operations"
```

---

## Task 7: Agent Runner

**Files:**
- Create: `crates/crucible-orchestrator/src/agent_runner.rs`
- Modify: `crates/crucible-orchestrator/src/main.rs`

- [ ] **Step 1: Write the failing test**

```rust
// crates/crucible-orchestrator/src/agent_runner.rs

// ... (implementation goes above)

#[cfg(test)]
mod tests {
    use super::*;
    use crucible_common::protocol::{AgentConfig, AgentName, TaskEnvelope};
    use std::path::PathBuf;

    fn test_runner() -> AgentRunner {
        // Resolve agents dir relative to workspace root
        let agents_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("agents");

        AgentRunner::new(
            PathBuf::from("python3"),
            agents_dir,
            std::time::Duration::from_secs(10),
        )
    }

    #[tokio::test]
    async fn run_echo_agent_returns_context() {
        let runner = test_runner();
        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Echo,
            context: serde_json::json!({"message": "hello from rust"}),
            config: AgentConfig {
                model: "test".to_string(),
                max_tokens: 100,
                timeout_seconds: 10,
            },
        };

        let result = runner.run_agent(task.clone()).await.unwrap();
        assert_eq!(result.task_id, task.task_id);
        assert_eq!(
            result.status,
            crucible_common::protocol::TaskStatus::Success
        );
        assert_eq!(
            result.result["echo"]["message"],
            "hello from rust"
        );
    }

    #[tokio::test]
    async fn run_agent_timeout() {
        // Create a runner with a very short timeout
        let agents_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("agents");

        let runner = AgentRunner::new(
            PathBuf::from("python3"),
            agents_dir,
            std::time::Duration::from_millis(1), // impossibly short
        );

        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Echo,
            context: serde_json::json!({}),
            config: AgentConfig {
                model: "test".to_string(),
                max_tokens: 100,
                timeout_seconds: 0,
            },
        };

        let result = runner.run_agent(task).await;
        assert!(result.is_err());
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo test -p crucible-orchestrator agent_runner`
Expected: FAIL -- `AgentRunner` not defined.

- [ ] **Step 3: Implement AgentRunner**

```rust
// crates/crucible-orchestrator/src/agent_runner.rs
use anyhow::{Context, Result};
use crucible_common::protocol::{AgentName, ResultEnvelope, TaskEnvelope};
use std::path::PathBuf;
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

pub struct AgentRunner {
    python_path: PathBuf,
    agents_dir: PathBuf,
    timeout: Duration,
}

impl AgentRunner {
    pub fn new(python_path: PathBuf, agents_dir: PathBuf, timeout: Duration) -> Self {
        Self {
            python_path,
            agents_dir,
            timeout,
        }
    }

    fn agent_module(&self, name: AgentName) -> String {
        let module = match name {
            AgentName::GameSelector => "game_selector",
            AgentName::GamePlayer => "game_player",
            AgentName::Profiler => "profiler",
            AgentName::Analyzer => "analyzer",
            AgentName::Optimizer => "optimizer",
            AgentName::Echo => "echo",
        };
        format!("agents.{}.agent", module)
    }

    pub async fn run_agent(&self, task: TaskEnvelope) -> Result<ResultEnvelope> {
        let module = self.agent_module(task.agent);
        let task_json =
            serde_json::to_string(&task).context("failed to serialize task envelope")?;

        // The agents dir's parent is the workspace root, which we need on PYTHONPATH
        let workspace_root = self
            .agents_dir
            .parent()
            .unwrap_or(&self.agents_dir);

        let mut child = Command::new(&self.python_path)
            .arg("-m")
            .arg(&module)
            .env("PYTHONPATH", workspace_root)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .with_context(|| format!("failed to spawn agent: {}", module))?;

        let mut stdin = child.stdin.take().context("failed to open agent stdin")?;
        stdin
            .write_all(task_json.as_bytes())
            .await
            .context("failed to write task to agent stdin")?;
        drop(stdin);

        let output = tokio::time::timeout(self.timeout, child.wait_with_output())
            .await
            .with_context(|| {
                format!("agent {} timed out after {:?}", module, self.timeout)
            })?
            .with_context(|| format!("agent {} failed to execute", module))?;

        let stderr = String::from_utf8_lossy(&output.stderr);
        if !stderr.is_empty() {
            tracing::debug!(agent = %module, stderr = %stderr, "agent stderr");
        }

        if !output.status.success() {
            anyhow::bail!(
                "agent {} exited with status {}: {}",
                module,
                output.status,
                stderr
            );
        }

        let result: ResultEnvelope = serde_json::from_slice(&output.stdout)
            .with_context(|| {
                format!(
                    "failed to parse agent {} output: {}",
                    module,
                    String::from_utf8_lossy(&output.stdout)
                )
            })?;

        Ok(result)
    }
}
```

- [ ] **Step 4: Add agent_runner module to main.rs**

Add `mod agent_runner;` to the top of `crates/crucible-orchestrator/src/main.rs`.

- [ ] **Step 5: Install Python dependencies**

Run: `pip install pydantic anthropic`
Expected: Dependencies installed.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cargo test -p crucible-orchestrator agent_runner`
Expected: Both tests pass. The echo agent test proves the full Rust -> Python -> Rust round-trip works.

- [ ] **Step 7: Commit**

```bash
git add crates/crucible-orchestrator/src/agent_runner.rs crates/crucible-orchestrator/src/main.rs
git commit -m "feat: add agent runner with subprocess IPC and timeout handling"
```

---

## Task 8: CLI Entry Point with Full Wiring

**Files:**
- Modify: `crates/crucible-orchestrator/src/main.rs`

- [ ] **Step 1: Write the integration test**

This test verifies the whole stack works: config -> db -> agent runner -> echo agent -> result stored in db.

Create the test file:

```rust
// crates/crucible-orchestrator/tests/integration_test.rs
use std::io::Write;
use std::path::PathBuf;

#[tokio::test]
async fn full_round_trip_echo_agent_to_db() {
    // 1. Create a temp config
    let tmp_dir = tempfile::tempdir().unwrap();
    let db_path = tmp_dir.path().join("test.db");
    let config_path = tmp_dir.path().join("config.toml");

    let mut config_file = std::fs::File::create(&config_path).unwrap();
    write!(
        config_file,
        r#"
        [orchestrator]
        db_path = "{}"
        artifact_dir = "{}"

        [vm]
        kernel_src = "/tmp/linux"
        guest_rootfs = "/tmp/rootfs"
        vfio_device = "0a:00.0"

        [measurement]

        [agents]
        "#,
        db_path.display(),
        tmp_dir.path().join("artifacts").display(),
    )
    .unwrap();

    // 2. Load config
    let config = crucible_orchestrator::config::CrucibleConfig::from_file(&config_path).unwrap();

    // 3. Open database
    let db = crucible_orchestrator::db::Database::open(&db_path).unwrap();
    let cycle_id = db.create_cycle("test_game", 12345).unwrap();

    // 4. Run echo agent
    let agents_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("agents");

    // Verify agents dir is at the workspace root level
    let workspace_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();

    let runner = crucible_orchestrator::agent_runner::AgentRunner::new(
        PathBuf::from("python3"),
        workspace_root.join("agents"),
        std::time::Duration::from_secs(10),
    );

    let task = crucible_common::protocol::TaskEnvelope {
        task_id: uuid::Uuid::new_v4(),
        agent: crucible_common::protocol::AgentName::Echo,
        context: serde_json::json!({"game": "test_game", "cycle_id": cycle_id}),
        config: crucible_common::protocol::AgentConfig {
            model: config.agents.model.clone(),
            max_tokens: 100,
            timeout_seconds: config.agents.timeout_secs,
        },
    };

    let result = runner.run_agent(task).await.unwrap();
    assert_eq!(
        result.status,
        crucible_common::protocol::TaskStatus::Success
    );
    assert_eq!(result.result["echo"]["game"], "test_game");

    // 5. Store a measurement as if the agent had produced one
    db.insert_measurement(cycle_id, "baseline", 60.0, 45.0, 25.0, 0.5, 1.2)
        .unwrap();
    db.update_cycle_status(cycle_id, "baseline_measurement")
        .unwrap();

    let cycle = db.get_cycle(cycle_id).unwrap();
    assert_eq!(cycle.status, "baseline_measurement");
    let measurements = db.get_measurements(cycle_id, "baseline").unwrap();
    assert_eq!(measurements.len(), 1);
}
```

- [ ] **Step 2: Make modules public for integration tests**

```rust
// crates/crucible-orchestrator/src/main.rs
pub mod agent_runner;
pub mod config;
pub mod db;

use clap::Parser;
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "crucible-orchestrator")]
#[command(about = "Agentic Linux gaming performance optimization")]
struct Cli {
    /// Path to configuration file
    #[arg(short, long, default_value = "config/crucible.toml")]
    config: PathBuf,
}

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "crucible_orchestrator=info".into()),
        )
        .init();

    let cli = Cli::parse();
    let config = config::CrucibleConfig::from_file(&cli.config)?;
    tracing::info!(db = %config.orchestrator.db_path, "loaded configuration");

    let db = db::Database::open(std::path::Path::new(&config.orchestrator.db_path))?;
    tracing::info!("database initialized");

    tracing::info!("crucible orchestrator ready");

    Ok(())
}
```

- [ ] **Step 3: Run the integration test**

Run: `cargo test -p crucible-orchestrator --test integration_test`
Expected: PASS -- full round trip works.

- [ ] **Step 4: Run the full test suite**

Run: `cargo test`
Expected: All Rust tests pass.

Run: `python3 -m pytest tests/python/ -v`
Expected: All Python tests pass.

- [ ] **Step 5: Commit**

```bash
git add crates/crucible-orchestrator/
git commit -m "feat: wire up CLI entry point and add full round-trip integration test"
```

---

## Completion Checklist

After all tasks:

- [ ] `cargo build --release` compiles cleanly
- [ ] `cargo test` -- all Rust tests pass
- [ ] `python3 -m pytest tests/python/ -v` -- all Python tests pass
- [ ] `cargo run -- --config config/crucible.toml` loads config, opens DB, prints "crucible orchestrator ready"
- [ ] The echo agent round-trip (Rust spawns Python, sends JSON, receives JSON, parses result) works end-to-end
- [ ] All changes committed

## Next Plans

After this foundation is solid:

- **Plan 2: VM Management** -- virtme-ng wrapper in Rust, guest agent (Python vsock daemon), host-guest RPC protocol, kernel build pipeline integration
- **Plan 3: Core Agents** -- game selector, profiler, analyzer, optimizer agents with Claude API tool-use, each with its own system prompt and tool definitions
- **Plan 4: Orchestration Loop** -- state machine implementation, statistical evaluator (Welch's t-test, Cohen's d), full closed-loop integration with VM + agents
