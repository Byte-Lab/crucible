# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Crucible is a closed-loop agentic system that optimizes Linux gaming performance by running benchmarks in a passthrough-GPU VM, identifying bottlenecks, generating kernel/userspace patches, and re-measuring. A Rust orchestrator daemon drives the state machine; Python agents powered by the Anthropic SDK do the reasoning-heavy steps.

Design and plan documents live in `docs/superpowers/specs/` and `docs/superpowers/plans/`. Read `docs/superpowers/specs/2026-04-12-crucible-design.md` before making architectural changes.

## Common commands

Rust workspace (run from repo root):

```bash
cargo build                                    # build all crates
cargo test                                     # all unit + integration tests
cargo test -p crucible-orchestrator           # one crate
cargo test -p crucible-orchestrator state_machine::tests::valid_full_cycle  # one test
cargo clippy --all-targets -- -D warnings
cargo run --bin crucible-orchestrator -- --config config/crucible.toml --single-cycle
cargo run --bin crucible-orchestrator -- --config config/crucible.toml --max-cycles 5
```

Python tests (no `src/` layout — `PYTHONPATH=.` is required so `agents.*` and `guest.*` resolve):

```bash
PYTHONPATH=. pytest tests/python                                  # all
PYTHONPATH=. pytest tests/python/test_agent_base.py -v            # one file
PYTHONPATH=. pytest tests/python/test_optimizer.py::test_name -v  # one test
```

Run a single agent by hand (it reads a `TaskEnvelope` JSON on stdin, writes a `ResultEnvelope` on stdout):

```bash
PYTHONPATH=. python3 -m agents.echo.agent < task.json
```

Claude-backed agents (`game_selector`, `profiler`, `analyzer`, `optimizer`) need `ANTHROPIC_API_KEY` in the environment. `echo` does not.

Logging: orchestrator uses `tracing` with `RUST_LOG`-style env filter — default is `crucible_orchestrator=info`.

## Architecture

### Two-process split

- **`crates/crucible-orchestrator`** — long-running Rust daemon. Owns the SQLite state DB, the cycle state machine, the VM lifecycle, and statistical evaluation. Never calls the Anthropic API directly.
- **`agents/<name>/agent.py`** — short-lived Python workers spawned per task. Stateless: they receive context, optionally call the Claude API via tools, and return a JSON result. They do **not** read or write SQLite, and they do not talk to each other.
- **`crates/crucible-common`** — shared protocol types (`TaskEnvelope`, `ResultEnvelope`, `AgentName`, `GuestCommand`/`GuestResponse`). Mirrored 1:1 in `agents/common/protocol.py` and `guest/protocol.py`. **If you change a wire type in one language, change it in the other.**
- **`guest/`** — code that runs *inside* the VM. `crucible_guest_agent.py` is a length-prefixed JSON RPC daemon listening on vsock port 5000.

### The orchestration loop

`crates/crucible-orchestrator/src/orchestrator.rs` walks an explicit state machine (`state_machine.rs`):

```
Idle → SelectGame → ProvisionVm → BaselineMeasurement → Analyze
     → GenerateOptimization → ApplyChanges → ComparisonMeasurement
     → Evaluate → (Accept | Reject | Iterate) → Idle | Analyze
```

Transitions are validated by `CycleState::valid_transitions()` and persisted to SQLite via `db.update_cycle_status` *before* the work runs, so a crash mid-state is recoverable. Any cycle error in `run_loop` resets the state machine to `Idle` and continues.

The full pipeline is wired:
- `ProvisionVm` calls `Orchestrator::provision_vm` → `KernelBuilder::build_kernel` (cached in `current_kernel`) → `VmManager::boot` → `wait_for_ready` (vsock health check).
- `BaselineMeasurement` / `ComparisonMeasurement` call the Profiler agent, then `persist_measurement` writes one row per phase to the `measurements` table.
- `ApplyChanges` calls `KernelBuilder::apply_and_build` (auto-reverts patch on build failure), shuts the VM down, reboots with the new image.
- `Evaluate` calls `run_evaluation` → per-metric `score_metric` → `db.insert_evaluation` → `determine_overall_verdict`. Branches: `Accept | Marginal | Neutral` → `Accept`; `Regressed` → `Reject`.

What still doesn't work end-to-end: there is no bootable guest rootfs yet, so `cargo run -- --single-cycle` requires real hardware + a populated `kernel_src` + a rootfs at `[vm].guest_rootfs`. There is no e2e smoke test. `Iterate` exists in the state machine but is never reached. `Reject` does not yet revert the applied patch via `KernelBuilder::revert_patch`.

### Agent dispatch protocol

`AgentRunner` (`agent_runner.rs`) spawns `python3 -m agents.<name>.agent` with `PYTHONPATH` set to the workspace root, writes a `TaskEnvelope` JSON to stdin, reads a `ResultEnvelope` from stdout, and enforces a per-agent timeout (`config.agents.timeout_secs`).

`ResultEnvelope.status` is one of `success | failure | needs_input`. The orchestrator currently treats `needs_input` as a hard error.

To add a new agent:
1. Add a variant to `AgentName` in `crates/crucible-common/src/protocol.rs`.
2. Add the matching arm in `AgentRunner::agent_module` in `agent_runner.rs`.
3. Create `agents/<name>/agent.py` subclassing `AgentBase` or `ClaudeAgentBase`, ending with `if __name__ == "__main__": MyAgent().run()`.

`ClaudeAgentBase` (`agents/common/claude_agent.py`) handles the Anthropic tool-use loop: subclasses implement `system_prompt()`, `build_user_message()`, optionally `setup_tools(registry)` and `extract_result()`. Tools are registered via the `@registry.tool(description=...)` decorator in `agents/common/tool_registry.py`, which derives the JSON Schema from Python type hints. The loop is capped at `MAX_TOOL_ROUNDS = 20`.

### Evaluation

`evaluator.rs` runs Welch's t-test + Cohen's d per metric. `orchestrator::determine_overall_verdict` aggregates per-metric verdicts: **any** `Regressed` blocks the whole cycle; all `Neutral` is `Neutral`; mix of `Accept` and `Neutral` is `Accept`; otherwise `Marginal`. Thresholds come from `[measurement]` in `config/crucible.toml`.

Metrics scored: `fps_avg`, `fps_p1` (higher is better); `frame_time_p99_ms`, `psi_cpu_avg`, `psi_memory_avg` (lower is better). Defined in `METRIC_DEFS` in `orchestrator.rs`.

Gotcha: `evaluator::welch_t_test` panics on zero-variance input (`StudentsT::new` returns `FreedomInvalid`). `orchestrator::score_metric` guards by checking `sample_variance` before delegating, falling back to a delta-only `Neutral` verdict. Real fix belongs in `evaluator.rs`.

### Configuration

Single source of truth: `config/crucible.toml`, parsed by `config.rs` into `CrucibleConfig`. All numeric/string fields have `serde(default)` fallbacks defined as `default_*` functions — keep those defaults in sync with `config/crucible.toml` if you add a field.

Hardware-specific values live in `[vm]` (`vfio_device`, `kernel_src`, `guest_rootfs`). Don't hardcode these elsewhere.

## Conventions specific to this repo

- **Wire types are duplicated across Rust and Python.** Treat `crucible-common::protocol`, `agents/common/protocol.py`, and `guest/protocol.py` as one logical schema in three files. Tests in `tests/python/test_protocol.py` and `crates/crucible-common/src/protocol.rs` exist to catch drift.
- **Agents do not import `crucible-orchestrator` or talk to SQLite.** All persistence goes through the orchestrator. If an agent needs prior cycle data, the orchestrator passes it in via `TaskEnvelope.context`.
- The `agents.*` and `guest.*` packages have no `setup.py`/`pyproject` install — they are imported by path. Always set `PYTHONPATH=.` (the workspace root) when running Python directly. The orchestrator does this automatically when spawning agents.
- Guest-agent RPC is **length-prefixed JSON over vsock** (4-byte big-endian length, then payload), not newline-delimited. See `guest/crucible_guest_agent.py:_recv_message`.
- Claude-backed agents (anything subclassing `ClaudeAgentBase`) return `{"response": "<final assistant text>"}` in their `ResultEnvelope.result`. The orchestrator uses `parse_agent_response()` to unwrap that envelope, optionally strip ` ```json ` fences, and parse the inner JSON. Use it whenever consuming a Claude agent's structured output.
