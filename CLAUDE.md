# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Crucible is a closed-loop agentic system that optimizes Linux gaming performance by running benchmarks in a passthrough-GPU VM, identifying bottlenecks, generating kernel/userspace patches, and re-measuring. A Rust orchestrator daemon drives the state machine; Python agents powered by the Anthropic SDK do the reasoning-heavy steps.

Task-scoped reference material lives in `skills/` -- `skills/README.md` maps which subtree to load by cycle stage (discovery, review, validation) and platform (virt VM lane, Steam Deck lane). Read `skills/architecture/design.md` before making architectural changes.

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
CRUCIBLE_E2E=1 cargo test --test e2e -- --nocapture       # hardware-gated synthetic smoke
CRUCIBLE_E2E_GPU=1 cargo test --test e2e -- --nocapture   # hardware-gated game-mode smoke
```

Both e2e gates (and any orchestrator run that spawns agents) need the uv
venv's python on PATH — the orchestrator spawns bare `python3`, and
`claude-agent-sdk`/pydantic live in `.venv`, not the system interpreter:

```bash
PATH="$PWD/.venv/bin:$PATH" CRUCIBLE_E2E=1 cargo test --test e2e -- --nocapture
```

The GPU gate additionally needs runtime-only host setup (VFIO bind of
every GPU function, a memlock limit >= guest RAM on the orchestrator
process, `~/.cache/virtme-ng` present) -- full checklist including the
pgrep-comm gotcha in `skills/platform/virt/gpu-passthrough.md`.

Build the guest rootfs images (mmdebstrap; both share `testbed/virt/lib/rootfs-common.sh`):

```bash
testbed/virt/setup-rootfs.sh                              # synthetic (bookworm) → ~/.crucible/rootfs
testbed/virt/setup-rootfs.sh --target /path --force       # explicit target + rebuild
testbed/virt/setup-game-rootfs.sh                         # game (trixie + Mesa/Vulkan/vkmark/glmark2/MangoHud) → ~/.crucible/game-rootfs
testbed/virt/setup-host.sh 03:00.0                        # VFIO precheck, prints bind commands (--bind executes)
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

Claude-backed agents (`game_selector`, `profiler`, `analyzer`, `optimizer`) route through the `claude-agent-sdk` PyPI package, which shells out to the bundled `claude` CLI for inference. That CLI inherits the OAuth session created by `claude /login`, so usage bills against the user's Claude.ai Pro/Max plan — **no `ANTHROPIC_API_KEY` is read or required**. `echo` is plain Python and needs neither. If the `claude` CLI hasn't been logged in on this machine, run `claude /login` interactively once before running any Claude-backed agent. The adapter explicitly passes `ANTHROPIC_API_KEY=""` through `ClaudeAgentOptions.env` so a stray host-env console key (e.g. a zero-balance `sk-ant-api03-…` from a parent shell) cannot silently override the OAuth session and fail the run with `assistant error: billing_error`.

Two protocol fields kept on the wire for backwards compatibility have drifted in meaning under the Agent SDK:
- `AgentConfig.max_tokens` is **advisory only** — the SDK has no per-call output-token cap. Tool loops are bounded by `MAX_TOOL_ROUNDS` on `ClaudeAgentBase` (default 40; `OptimizerAgent` overrides to 80 because the `read_source_file → edit_file × N → finalize_patch` chain on a real kernel bottleneck routinely overruns the base cap) and by the orchestrator's `timeout_seconds` subprocess kill.
- `AgentConfig.max_retries` is forwarded to the CLI via `CLAUDE_CODE_MAX_RETRIES` (default 10 in the CLI; our default 3 here).

Logging: orchestrator uses `tracing` with `RUST_LOG`-style env filter — default is `crucible_orchestrator=info`.

## Architecture

### Repository layout

- `crates/` -- Rust workspace: orchestrator daemon + shared protocol types
- `agents/` -- host-side Python agent workers (Claude-backed)
- `guest/` -- payload deployed INSIDE the measurement VM / onto the Deck
  (guest agent RPC daemon, stdlib-only protocol mirror, systemd unit,
  cgroup setup). Copied into every rootfs by testbed/virt/lib, overlaid
  live via `[vm] guest_payload`, rsynced to the Deck by the deck backend.
- `testbed/` -- platform setup + benchmarking machinery, NOT product code:
  `testbed/virt/` (rootfs builders, VFIO host setup for the vng VM loop),
  `testbed/deck/` (Steam Deck bootstrap/slot-B deploy, interleaved A/B
  harnesses, stats tools -- see testbed/README.md for the inventory)
- `patches/` -- the upstream patch corpus: `candidates/` pipeline managed
  by `patches/candidates/patchctl`, `negative-results/`, shared `evidence/`
  (see "Upstream patch corpus" section below). Perfetto traces are
  co-located with their patch but gitignored (*.pftrace).
- `skills/` -- task-scoped reference material, organized by cycle stage
  and platform: `architecture/` (design spec), `discovery/` (profiling
  toolkit + prototype-first rule), `validation/` (adversarial review,
  winner validation + A/A calibration), `platform/{virt,deck}/` (lane
  constraints). Start at `skills/README.md` for the when-to-load map.
- `config/` -- crucible.toml; `tests/python/` -- agent/protocol tests

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
- `Evaluate` calls `run_evaluation` → per-metric `evaluator::evaluate_metric` → `db.insert_evaluation` → `determine_overall_verdict`. Inside `run_cycle` the Analyze → Evaluate block runs in an iteration loop: a `Marginal` verdict with attempt number below `agents.optimizer.max_attempts_per_bottleneck` (default 3) transitions `Evaluate → Iterate → Analyze` and re-runs the inner pipeline with `previous_attempts` threaded into the Analyzer/Optimizer envelopes via `TaskEnvelope.context`. Once the cap is reached the verdict-to-state mapping applies: `Accept | Marginal | Neutral` → `Accept`; `Regressed` → `Reject` (with `KernelBuilder::revert_patch` on the final applied patch).

`VmManager::boot` spawns `vng` with the kernel-source path as cwd (virtme-ng 1.35 has no `--kernel`/`--boot` flags — it picks up `arch/x86/boot/bzImage` from cwd itself). The qemu device for vsock is appended via `--qemu-opts=` (the `=` form is required because argparse rejects bare values starting with `-`). `--exec` runs the guest agent directly instead of letting systemd start the unit, so the synthetic loop doesn't depend on cgroups or service ordering. When `[vm] guest_payload` is set, the host path is overlaid via `--rodir /opt/crucible/guest=…` so editing the agent doesn't require a rootfs rebuild. The child has `kill_on_drop(true)` to prevent leaked QEMUs from holding the vsock CID across panicking tests.

Measurement modes (`[measurement] mode`, granular mechanics live in the
platform skills -- they are the source of truth, not this file):
- `"synthetic"` (default): profiler drives `stress-ng` via the guest
  RPC on the bookworm bench rootfs. Details + the kernel-patch corpus
  grind (the four loop fixes: workload-aware analyzer,
  `explored_areas` diversity, zero-frame-time rejection, optimizer API
  verification): `skills/platform/virt/synthetic-mode.md`.
- `"game"`: vkmark/glmark2 under MangoHud on the passthrough GPU
  (trixie game rootfs). Verified on real hardware; non-zero `fps_avg`
  is the discriminator that real frames flowed. The hard-won
  VFIO/MangoHud/process-group constraints are encoded in code and
  cataloged in `skills/platform/virt/gpu-passthrough.md` -- read it
  before touching `VmManager`/`KernelBuilder` boot paths or running a
  GPU grind (it also covers the known robustness gaps: no
  agent-failure backoff, QEMU leak on harness kill, `setsid` for long
  grinds).
- `"steam"`: a real Steam title launched headless in the VM. Launch
  recipe, weston `--idle-time=0` presentation stack, Civ 6 benchmark
  modes, rootfs seeding, and debug lessons:
  `skills/platform/virt/steam-mode.md`.

The Perfetto profiling stage (baseline-phase profiled repeat ->
analyzer trace mining -> optimizer patch) closed the full loop live on
real hardware; its capture constraints and known gaps (guest daemons,
flush-on-stop, host-binary packaging, sysctl_changes not applied in
guest): `skills/platform/virt/perfetto-loop.md`.

Winner validation: a high-confidence patch (Welch-significant
improvement, no regression) additionally runs the standard upstream
regression benchmarks for the subsystem it touches -- mapping and
protocol in `skills/validation/winner-validation.md`. Results go into
the winner's `EVIDENCE.md` package, regressions included.

E2E status: with `CRUCIBLE_E2E=1` the full SelectGame -> ... ->
Evaluate pipeline runs live (~280-330s on a cached kernel); the
hardware-gated test prints `e2e skipped` and passes when unset. The
first autonomous game-mode grind (10 cycles, 8 subsystems, 2
since-killed wins) is recorded in
`patches/evidence/vkmark-game-grind/RESULTS.md`.

### Agent dispatch protocol

`AgentRunner` (`agent_runner.rs`) spawns `python3 -m agents.<name>.agent` with `PYTHONPATH` set to the workspace root, writes a `TaskEnvelope` JSON to stdin, reads a `ResultEnvelope` from stdout, and enforces a per-agent timeout (`config.agents.timeout_secs`).

`ResultEnvelope.status` is one of `success | failure | needs_input`. The orchestrator currently treats `needs_input` as a hard error.

To add a new agent:
1. Add a variant to `AgentName` in `crates/crucible-common/src/protocol.rs`.
2. Add the matching arm in `AgentRunner::agent_module` in `agent_runner.rs`.
3. Create `agents/<name>/agent.py` subclassing `AgentBase` or `ClaudeAgentBase`, ending with `if __name__ == "__main__": MyAgent().run()`.

`ClaudeAgentBase` (`agents/common/claude_agent.py`) handles the Anthropic tool-use loop: subclasses implement `system_prompt()`, `build_user_message()`, optionally `setup_tools(registry)` and `extract_result()`. Tools are registered via the `@registry.tool(description=...)` decorator in `agents/common/tool_registry.py`, which derives the JSON Schema from Python type hints. The loop is capped at `MAX_TOOL_ROUNDS` (40 on the base class, overridden to 80 on `OptimizerAgent`).

Before `setup_tools` runs, `execute` reads `task.context["vsock_cid"]` (set by the orchestrator) and instantiates `agents/common/guest_rpc.py::GuestRpc`, assigning it to `self._guest_rpc`. Tools that need to talk to the guest agent (`agents/profiler/tools.py`: `start_profiling`, `stop_profiling`, `get_guest_metrics`, `run_benchmark`) pick the client up via `getattr(self, "_guest_rpc", None)` and fall through to dry-run/local-PSI behaviour when it is absent. The `GuestRpc.call(cmd, args)` method does an AF_VSOCK connect-per-call to port 5000 with the same length-prefixed JSON framing the guest serves.

Every spawned agent's captured stderr is teed to `<config.orchestrator.artifact_dir>/agents/<task_id>.stderr` by `AgentRunner::run_agent` regardless of exit status, so post-run grep can verify that the `_BUILTIN_TOOLS_TO_DISALLOW` lockdown in `ClaudeAgentBase` actually held. `ClaudeAgentBase._run` mirrors every `ToolUseBlock` to stderr as `tool_call: <name>` (names only — tool inputs may contain file contents or secrets and stay in the `ResultEnvelope.logs` channel via `self.log(...)`). The heavy e2e test (`crates/crucible-orchestrator/tests/e2e.rs`) walks that directory and fails if any `tool_call:` line references a tool not prefixed with `mcp__crucible__`. Timed-out agents leave no stderr file because `wait_with_output()` never resolves on the timeout path — the timeout itself is the diagnostic.

`claude-agent-sdk` exports `RateLimitEvent`, `RateLimitInfo`, and `RateLimitStatus`, but `ClaudeAgentBase._run` currently does not branch on them. The bundled `claude` CLI's built-in retry loop (capped by `CLAUDE_CODE_MAX_RETRIES`, set to `task.config.max_retries` in `agents/common/claude_agent.py`) handles transient rate limits transparently. A typed `RateLimitError` branch (which would let the orchestrator distinguish rate-limit aborts from generic agent failure and back off instead of resetting to `Idle`) is deliberately not added until an actual rate-limit event has been observed in a heavy run; speculative branching here is more likely to mask real failure modes than help. If rate limits start surfacing in the stderr artifacts, plan a proper add then.

### Evaluation

`evaluator.rs` runs Welch's t-test + Cohen's d per metric. `orchestrator::determine_overall_verdict` aggregates per-metric verdicts: **any** `Regressed` blocks the whole cycle; all `Neutral` is `Neutral`; mix of `Accept` and `Neutral` is `Accept`; otherwise `Marginal`. Thresholds come from `[measurement]` in `config/crucible.toml`.

Metrics scored: `fps_avg`, `fps_p1` (higher is better); `frame_time_p99_ms`, `psi_cpu_avg`, `psi_memory_avg` (lower is better). Defined in `METRIC_DEFS` in `orchestrator.rs`.

`evaluator::welch_t_test` returns `Option<TTestResult>` — `None` on degenerate input (fewer than two samples per side, zero variance, or non-finite Satterthwaite df). `evaluate_metric` maps `None` to a delta-only `Neutral` verdict so the cycle always produces evaluation rows. `StudentsT::new` is no longer called on a zero-variance path; do not reintroduce an outer guard.

### Configuration

Single source of truth: `config/crucible.toml`, parsed by `config.rs` into `CrucibleConfig`. All numeric/string fields have `serde(default)` fallbacks defined as `default_*` functions — keep those defaults in sync with `config/crucible.toml` if you add a field.

Hardware-specific values live in `[vm]` (`vfio_device`, `kernel_src`, `guest_rootfs`, `vsock_cid`, optional `guest_payload`). Don't hardcode these elsewhere. `vfio_device` accepts the empty string or `"none"` to skip GPU passthrough — required for the synthetic loop on commodity hardware. `guest_payload` is a host path that gets overlaid into the guest at `/opt/crucible/guest`; the e2e test points it at the repo's `guest/` directory so iteration doesn't need a rootfs rebuild.

`[measurement] mode` selects the profiler path. `"synthetic"` (default) drives `stress-ng` via the guest RPC and is the only path the bookworm rootfs from `testbed/virt/setup-rootfs.sh` supports. `"game"` drives the native GPU benchmark selected by `game_benchmark` (`vkmark` default, `glmark2` alternative) under MangoHud and needs the trixie rootfs from `testbed/virt/setup-game-rootfs.sh`. `benchmark_args` and `benchmark_duration_secs` configure the synthetic workload.

## Upstream patch corpus (patches/)

The project's end product is upstream-quality kernel and sched_ext patches;
`patches/` is the corpus. Layout (since 2026-07-17):

- `patches/candidates/{kern,scx}/{created,sent,merged,rejected}/<slug>/`
  -- one directory per candidate patch containing ALL its artifacts (diff,
  commitmsg with SoB, EVIDENCE.md, reproducers, raw A/B logs, rejected
  iteration diffs) plus a `SCORECARD.md` (frontmatter: title/state/tier/
  target/suggested_cc/base/review_status; body: numbers, review trajectory,
  prep-before-sending). kern = LKML, scx = sched-ext/scx GitHub.
- `patches/candidates/patchctl` -- browse/manage tool. `patchctl` lists
  (kern/scx split), `summary`, `path`/`evidence`/`scorecard` dump matching
  patches, `show <slug>`, `move <slug> <state>` advances a patch through the
  pipeline (moves dir + rewrites scorecard state). Filters: `-c kern|scx`,
  `-t 1|2|3` (TIER_1 = send-ready, TIER_2 = needs work, TIER_3 = blocked/
  unlikely), `-s <state>`, slug substrings.
- `patches/negative-results/` -- patches killed by measurement, kept
  with full evidence (negative results are deliverables too).
- `patches/evidence/` -- shared investigation corpus: perfetto traces
  (`traces/*.pftrace`), raw A/B data (`ab-data/`), root-cause/design docs.
- `patches/SUMMARY.md` -- narrative index of wins and kills. Paths
  inside predate the reorg; resolve any file via `patchctl path <slug>`.

Discipline rules for adding to the corpus (non-negotiable, learned the hard
way -- see EVIDENCE files for the body count):

1. **Adversarial review loop**: every patch iterates author vs fresh
   skeptical reviewer until a FRESH reviewer returns APPROVE with no
   required changes. Reviewers must independently recompute claimed numbers
   from raw logs and verify cited commits/mechanisms against source.
2. **Interleaved measurement only**: A/B alternates kernels boot-by-boot
   (or blocks within one session on the Deck); never compare against a
   baseline from a different session/thermal state. Verify kernel identity
   per boot (`/proc/version` md5 in the log). Welch t-test; report CVs;
   prefer boot-level clustering over rep-level n.
3. **Prototype-first**: before building any layout/perf patch, run a cheap
   targeted probe (perf c2c on the workload, or a reader-vs-writer
   microbench) proving the claimed cost exists. Static-scan plausibility
   is not evidence.
4. **Distrust documented perf lore**: layout/optimization comments encode
   dead microarchitectures; re-measure before "restoring" any documented
   optimum.
5. Negative results get an EVIDENCE.md in `negative-results/` -- they
   prevent re-litigating dead ideas.

Benchmark harness facts: mainline tree `~/upstream/crucible_kernel_1`
(bzImage boots via `script -qec "vng --cpus 32 --memory 8G -- bash
<guest.sh>" /dev/null` from the tree dir; 9p exposes host fs so host
binaries work in-guest); Deck/neptune tree `~/upstream/crucible_kernel_2`.
will-it-scale binaries: `-t N` is the task-count flag (positional arg is
IGNORED); output line `average:N`. perf c2c works on host AMD (IBS) but
NOT inside VMs (KVM does not virtualize IBS) -- c2c evidence must come
from bare metal. null_blk: the q-level shared-tags atomic path needs
`shared_tag_bitmap=1` (HCTX_SHARED), not just `shared_tags=1`.

## Conventions specific to this repo

- **Wire types are duplicated across Rust and Python.** Treat `crucible-common::protocol`, `agents/common/protocol.py`, and `guest/protocol.py` as one logical schema in three files. Tests in `tests/python/test_protocol.py` and `crates/crucible-common/src/protocol.rs` exist to catch drift. `guest/protocol.py` is intentionally stdlib-only (`@dataclass` + `to_dict`/`from_dict`/`to_json`/`from_json`) — Debian bookworm ships pydantic v1 and the agent has to import there too. The Anthropic-side host agents still use pydantic v2 freely; just don't introduce pydantic types in the guest module.
- **Agents do not import `crucible-orchestrator` or talk to SQLite.** All persistence goes through the orchestrator. If an agent needs prior cycle data, the orchestrator passes it in via `TaskEnvelope.context`.
- The `agents.*` and `guest.*` packages have no `setup.py`/`pyproject` install — they are imported by path. Always set `PYTHONPATH=.` (the workspace root) when running Python directly. The orchestrator does this automatically when spawning agents.
- Guest-agent RPC is **length-prefixed JSON over vsock** (4-byte big-endian length, then payload), not newline-delimited. See `guest/crucible_guest_agent.py:_recv_message`. The host-side counterpart is `agents/common/guest_rpc.py::GuestRpc` (connect-per-call AF_VSOCK).
- Claude-backed agents (anything subclassing `ClaudeAgentBase`) return `{"response": "<final assistant text>"}` in their `ResultEnvelope.result`. The orchestrator uses `parse_agent_response()` to unwrap that envelope, optionally strip ` ```json ` fences, and parse the inner JSON. Use it whenever consuming a Claude agent's structured output.
- The minimal guest rootfs is built by `testbed/virt/setup-rootfs.sh` using `mmdebstrap --mode=root` (auto-elevates with sudo) into `~/.crucible/rootfs`. It installs `systemd-sysv`, `udev` (required for `/dev/virtio-ports/*` symlinks that virtme-init looks for), `python3`, `stress-ng`, `linux-perf`, `dbus`, `kmod`, and enables `crucible-guest-agent.service` plus a oneshot `crucible-cgroups.service`. No `python3-pydantic` — the guest agent uses stdlib only. The script fails fast if `mmdebstrap` is missing (no silent `debootstrap` fallback). On hosts without `debian-archive-keyring` (Ubuntu) the bootstrap runs with apt's insecure-repo options. Idempotent via the `.crucible-built` stamp file in the target.
- The game rootfs is built by `testbed/virt/setup-game-rootfs.sh` into `~/.crucible/game-rootfs` (Debian trixie for Mesa 25.x/RDNA3). Package set, rationale, and constraints: `skills/platform/virt/gpu-passthrough.md`. Stamp `.crucible-game-built`. All rootfs scripts share `testbed/virt/lib/rootfs-common.sh`.
- The steam rootfs is built by `testbed/virt/setup-steam-rootfs.sh` into `~/.crucible/steam-rootfs`. Build details, credential seeding (`CRUCIBLE_STEAM_CLIENT_CREDS`), and the library-refresh recipe: `skills/platform/virt/steam-mode.md`. Stamp `.crucible-steam-built`.
