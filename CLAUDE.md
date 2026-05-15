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
CRUCIBLE_E2E=1 cargo test --test e2e -- --nocapture   # hardware-gated end-to-end smoke
```

Build the minimal guest rootfs (Debian bookworm via mmdebstrap):

```bash
scripts/setup-rootfs.sh                              # writes ~/.crucible/rootfs
scripts/setup-rootfs.sh --target /path --force       # explicit target + rebuild
```

The script auto-elevates with sudo because `mmdebstrap --mode=root` is
required for the guest's files to be owned by uid 0 on disk; vng's 9p
mount surfaces those uids inside the guest, and init refuses to mount
`/run` unless it's actually uid 0. On hosts without
`debian-archive-keyring` (Ubuntu by default) the script switches on
insecure apt options for the bootstrap. The default target follows
`SUDO_USER`'s HOME, not root's.

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

Claude-backed agents (`game_selector`, `profiler`, `analyzer`, `optimizer`) route through the `claude-agent-sdk` PyPI package, which shells out to the bundled `claude` CLI for inference. That CLI inherits the OAuth session created by `claude /login`, so usage bills against the user's Claude.ai Pro/Max plan — **no `ANTHROPIC_API_KEY` is read or required**. `echo` is plain Python and needs neither. If the `claude` CLI hasn't been logged in on this machine, run `claude /login` interactively once before running any Claude-backed agent.

Two protocol fields kept on the wire for backwards compatibility have drifted in meaning under the Agent SDK:
- `AgentConfig.max_tokens` is **advisory only** — the SDK has no per-call output-token cap. Tool loops are bounded by `MAX_TOOL_ROUNDS = 40` (in `ClaudeAgentBase`) and the orchestrator's `timeout_seconds` subprocess kill.
- `AgentConfig.max_retries` is forwarded to the CLI via `CLAUDE_CODE_MAX_RETRIES` (default 10 in the CLI; our default 3 here).

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
- `BaselineMeasurement` / `ComparisonMeasurement` call the Profiler agent, then `persist_measurement` writes one row per phase to the `measurements` table. The orchestrator threads `vsock_cid`, `workload_kind`, `benchmark_args`, and `duration_secs` into `TaskEnvelope.context` so the profiler can drive synthetic stress-ng runs.
- `ApplyChanges` calls `KernelBuilder::apply_and_build` (auto-reverts patch on build failure), shuts the VM down, reboots with the new image.
- `Evaluate` calls `run_evaluation` → per-metric `evaluator::evaluate_metric` → `db.insert_evaluation` → `determine_overall_verdict`. Branches: `Accept | Marginal | Neutral` → `Accept`; `Regressed` → `Reject`.

`VmManager::boot` spawns `vng` with the kernel-source path as cwd (virtme-ng 1.35 has no `--kernel`/`--boot` flags — it picks up `arch/x86/boot/bzImage` from cwd itself). The qemu device for vsock is appended via `--qemu-opts=` (the `=` form is required because argparse rejects bare values starting with `-`). `--exec` runs the guest agent directly instead of letting systemd start the unit, so the synthetic loop doesn't depend on cgroups or service ordering. When `[vm] guest_payload` is set, the host path is overlaid via `--rodir /opt/crucible/guest=…` so editing the agent doesn't require a rootfs rebuild. The child has `kill_on_drop(true)` to prevent leaked QEMUs from holding the vsock CID across panicking tests.

Synthetic-loop path (`[measurement] mode = "synthetic"`, default): the profiler agent calls `run_benchmark('stress-ng', args, duration_secs)` via the guest RPC, parses ops/sec from `--metrics-brief`, and emits `fps_avg = fps_p1 = 0`, `frame_time_p99_ms = 1000 / ops_per_sec`, `psi_*_avg = psi_*_delta`. Game-mode (`mode = "game"`) keeps the MangoHud/perfetto tools and is gated on a Steam/Wine/Mesa rootfs that doesn't exist yet.

End-to-end status (2026-05-15): `cargo test --test e2e` with `CRUCIBLE_E2E=1` runs cleanly through **SelectGame → ProvisionVm → BaselineMeasurement → Analyze**, then previously tripped the Anthropic API 429 rate limit on the **GenerateOptimization** call (org cap = 30k input tokens/min). Migration to the Claude Agent SDK (commit pending) routes inference through the user's Pro/Max subscription instead of the API console, removing that cap. `ApplyChanges`, `ComparisonMeasurement`, `Evaluate` haven't been exercised live since the migration. `Iterate` exists in the state machine but is never reached. `Reject` does not yet revert the applied patch via `KernelBuilder::revert_patch`. The hardware-gated test prints `e2e skipped` and passes when `CRUCIBLE_E2E` is unset.

### Agent dispatch protocol

`AgentRunner` (`agent_runner.rs`) spawns `python3 -m agents.<name>.agent` with `PYTHONPATH` set to the workspace root, writes a `TaskEnvelope` JSON to stdin, reads a `ResultEnvelope` from stdout, and enforces a per-agent timeout (`config.agents.timeout_secs`).

`ResultEnvelope.status` is one of `success | failure | needs_input`. The orchestrator currently treats `needs_input` as a hard error.

To add a new agent:
1. Add a variant to `AgentName` in `crates/crucible-common/src/protocol.rs`.
2. Add the matching arm in `AgentRunner::agent_module` in `agent_runner.rs`.
3. Create `agents/<name>/agent.py` subclassing `AgentBase` or `ClaudeAgentBase`, ending with `if __name__ == "__main__": MyAgent().run()`.

`ClaudeAgentBase` (`agents/common/claude_agent.py`) handles the Anthropic tool-use loop: subclasses implement `system_prompt()`, `build_user_message()`, optionally `setup_tools(registry)` and `extract_result()`. Tools are registered via the `@registry.tool(description=...)` decorator in `agents/common/tool_registry.py`, which derives the JSON Schema from Python type hints. The loop is capped at `MAX_TOOL_ROUNDS = 20`.

Before `setup_tools` runs, `execute` reads `task.context["vsock_cid"]` (set by the orchestrator) and instantiates `agents/common/guest_rpc.py::GuestRpc`, assigning it to `self._guest_rpc`. Tools that need to talk to the guest agent (`agents/profiler/tools.py`: `start_profiling`, `stop_profiling`, `get_guest_metrics`, `run_benchmark`) pick the client up via `getattr(self, "_guest_rpc", None)` and fall through to dry-run/local-PSI behaviour when it is absent. The `GuestRpc.call(cmd, args)` method does an AF_VSOCK connect-per-call to port 5000 with the same length-prefixed JSON framing the guest serves.

### Evaluation

`evaluator.rs` runs Welch's t-test + Cohen's d per metric. `orchestrator::determine_overall_verdict` aggregates per-metric verdicts: **any** `Regressed` blocks the whole cycle; all `Neutral` is `Neutral`; mix of `Accept` and `Neutral` is `Accept`; otherwise `Marginal`. Thresholds come from `[measurement]` in `config/crucible.toml`.

Metrics scored: `fps_avg`, `fps_p1` (higher is better); `frame_time_p99_ms`, `psi_cpu_avg`, `psi_memory_avg` (lower is better). Defined in `METRIC_DEFS` in `orchestrator.rs`.

`evaluator::welch_t_test` returns `Option<TTestResult>` — `None` on degenerate input (fewer than two samples per side, zero variance, or non-finite Satterthwaite df). `evaluate_metric` maps `None` to a delta-only `Neutral` verdict so the cycle always produces evaluation rows. `StudentsT::new` is no longer called on a zero-variance path; do not reintroduce an outer guard.

### Configuration

Single source of truth: `config/crucible.toml`, parsed by `config.rs` into `CrucibleConfig`. All numeric/string fields have `serde(default)` fallbacks defined as `default_*` functions — keep those defaults in sync with `config/crucible.toml` if you add a field.

Hardware-specific values live in `[vm]` (`vfio_device`, `kernel_src`, `guest_rootfs`, `vsock_cid`, optional `guest_payload`). Don't hardcode these elsewhere. `vfio_device` accepts the empty string or `"none"` to skip GPU passthrough — required for the synthetic loop on commodity hardware. `guest_payload` is a host path that gets overlaid into the guest at `/opt/crucible/guest`; the e2e test points it at the repo's `guest/` directory so iteration doesn't need a rootfs rebuild.

`[measurement] mode` selects the profiler path. `"synthetic"` (default) drives `stress-ng` via the guest RPC and is the only path the bookworm rootfs from `scripts/setup-rootfs.sh` supports today. `"game"` keeps the legacy MangoHud/perfetto tooling for when a Steam/Wine rootfs exists. `benchmark_args` and `benchmark_duration_secs` configure the synthetic workload.

## Conventions specific to this repo

- **Wire types are duplicated across Rust and Python.** Treat `crucible-common::protocol`, `agents/common/protocol.py`, and `guest/protocol.py` as one logical schema in three files. Tests in `tests/python/test_protocol.py` and `crates/crucible-common/src/protocol.rs` exist to catch drift. `guest/protocol.py` is intentionally stdlib-only (`@dataclass` + `to_dict`/`from_dict`/`to_json`/`from_json`) — Debian bookworm ships pydantic v1 and the agent has to import there too. The Anthropic-side host agents still use pydantic v2 freely; just don't introduce pydantic types in the guest module.
- **Agents do not import `crucible-orchestrator` or talk to SQLite.** All persistence goes through the orchestrator. If an agent needs prior cycle data, the orchestrator passes it in via `TaskEnvelope.context`.
- The `agents.*` and `guest.*` packages have no `setup.py`/`pyproject` install — they are imported by path. Always set `PYTHONPATH=.` (the workspace root) when running Python directly. The orchestrator does this automatically when spawning agents.
- Guest-agent RPC is **length-prefixed JSON over vsock** (4-byte big-endian length, then payload), not newline-delimited. See `guest/crucible_guest_agent.py:_recv_message`. The host-side counterpart is `agents/common/guest_rpc.py::GuestRpc` (connect-per-call AF_VSOCK).
- Claude-backed agents (anything subclassing `ClaudeAgentBase`) return `{"response": "<final assistant text>"}` in their `ResultEnvelope.result`. The orchestrator uses `parse_agent_response()` to unwrap that envelope, optionally strip ` ```json ` fences, and parse the inner JSON. Use it whenever consuming a Claude agent's structured output.
- The minimal guest rootfs is built by `scripts/setup-rootfs.sh` using `mmdebstrap --mode=root` (auto-elevates with sudo) into `~/.crucible/rootfs`. It installs `systemd-sysv`, `udev` (required for `/dev/virtio-ports/*` symlinks that virtme-init looks for), `python3`, `stress-ng`, `linux-perf`, `dbus`, `kmod`, and enables `crucible-guest-agent.service` plus a oneshot `crucible-cgroups.service`. No `python3-pydantic` — the guest agent uses stdlib only. No Steam/Wine/Mesa — those land in a later milestone. The script fails fast if `mmdebstrap` is missing (no silent `debootstrap` fallback). On hosts without `debian-archive-keyring` (Ubuntu) the bootstrap runs with apt's insecure-repo options. Idempotent via the `.crucible-built` stamp file in the target.
