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
CRUCIBLE_E2E=1 cargo test --test e2e -- --nocapture       # hardware-gated synthetic smoke
CRUCIBLE_E2E_GPU=1 cargo test --test e2e -- --nocapture   # hardware-gated game-mode smoke
```

Both e2e gates (and any orchestrator run that spawns agents) need the uv
venv's python on PATH — the orchestrator spawns bare `python3`, and
`claude-agent-sdk`/pydantic live in `.venv`, not the system interpreter:

```bash
PATH="$PWD/.venv/bin:$PATH" CRUCIBLE_E2E=1 cargo test --test e2e -- --nocapture
```

The GPU gate additionally requires (all runtime-only, reset by reboot):
`scripts/setup-host.sh <gpu-addr> --bind` run for the GPU (binds **every
function of the slot** to vfio-pci and chowns the `/dev/vfio/<group>`
nodes), a memlock limit ≥ guest RAM for the orchestrator process
(`sudo prlimit --pid <pid> --memlock=unlimited`), and `~/.cache/virtme-ng`
existing (QEMU dies instantly on the missing 9p fsdev dir if a cache
cleaner removed it).

Build the guest rootfs images (mmdebstrap; both share `scripts/lib/rootfs-common.sh`):

```bash
scripts/setup-rootfs.sh                              # synthetic (bookworm) → ~/.crucible/rootfs
scripts/setup-rootfs.sh --target /path --force       # explicit target + rebuild
scripts/setup-game-rootfs.sh                         # game (trixie + Mesa/Vulkan/vkmark/glmark2/MangoHud) → ~/.crucible/game-rootfs
scripts/setup-host.sh 03:00.0                        # VFIO precheck, prints bind commands (--bind executes)
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

Synthetic-loop path (`[measurement] mode = "synthetic"`, default): the profiler agent calls `run_benchmark('stress-ng', args, duration_secs)` via the guest RPC, parses ops/sec from `--metrics-brief`, and emits `fps_avg = fps_p1 = 0`, `frame_time_p99_ms = 1000 / ops_per_sec`, `psi_*_avg = psi_*_delta`. Game-mode path (`mode = "game"`): the profiler calls `launch_benchmark(name, args, mangohud_output)` — the guest runs an allow-listed native benchmark (`vkmark`/`glmark2`, per `[measurement] game_benchmark`) under MangoHud with `autostart_log=1` and renames the generated CSV to the deterministic `mangohud_output` path — then `fetch_mangohud_log(log_path)`, which pulls the CSV over vsock as base64 (`fetch_file` returns `contents_b64`/`truncated`, 8 MiB cap) and parses it with `parse_mangohud_csv` (nearest-rank percentiles, skips MangoHud's two system-info header rows). Non-zero `fps_avg` is the discriminator that real frames flowed. Requires the trixie game rootfs (`scripts/setup-game-rootfs.sh`); without VFIO passthrough Mesa's lavapipe renders in software, which still exercises the full path. The game_selector is told `workload_kind` and pivots to `list_native_benchmarks` in game mode (no Steam library exists in the guest).

Steam-mode path (`mode = "steam"`, milestone G3): the profiler calls `launch_steam_benchmark(app_id, args, mangohud_output, duration_secs)` against the steam rootfs (`scripts/setup-steam-rootfs.sh`). The guest handler DHCPs the slirp NIC (`_ensure_network` — Steam's CM logon needs a route out; `VmManager` appends the netdev unconditionally), raises `vm.max_map_count`/`overcommit_memory`, starts weston **as the steam user** (a root-owned Wayland socket/X-auth cookie segfaults the crucible-user client with "Unable to open display"), then does a **two-phase launch**: the first `steam.sh -silent` start MUST carry `-applaunch <id>` and the MangoHud env (the game inherits the *client's* env; a bare client that gets the launch only via a later IPC `-applaunch` never spawns the game), followed by a second IPC `-applaunch` as an update-restart retry. Client liveness is probed with `pgrep -x steam` polled until continuously up (`steam.sh` exits 0 once the client daemonizes — its exit code says nothing; the client also restarts itself once to self-update). Never invoke the Debian `/usr/games/steam` wrapper: it targets `~/.steam/debian-installation` and hangs on a zenity dialog headless. `[agents] timeout_secs = 1500` sizes the whole launch RPC (client settle 240s + Fossilize + asset load + log window); the steam e2e case boots 24G (pressure-vessel container build + game OOM 16G). **Measurement caveat:** Dota 2 launches and renders on RADV, but its headless idle menu stops presenting frames (gpu_load=0), so no meaningful CSV yet — a `.dem` file + `+timedemo` launch is the missing workload. The rootfs script seeds full-client login creds from `CRUCIBLE_STEAM_CLIENT_CREDS` (steamcmd's cached session alone cannot log the full client in) and documents the library-refresh recipe (the client refuses `-applaunch` on an update-required app, and the in-guest download dies in the ephemeral overlay).

End-to-end status (2026-05-15): post Agent SDK migration, `cargo test --test e2e` with `CRUCIBLE_E2E=1` now runs the full **SelectGame → ProvisionVm → BaselineMeasurement → Analyze → GenerateOptimization → ApplyChanges → ComparisonMeasurement → Evaluate → (Accept | Reject)** pipeline live; a green run completes in ~280-330s on a cached kernel. `Reject` calls `KernelBuilder::revert_patch` on the final applied patch (orchestrator.rs maps `Verdict::Regressed → CycleState::Reject` and invokes the revert before transitioning to `Idle`). `Iterate` is now wired: `Verdict::Marginal` re-enters Analyze through the `Evaluate → Iterate → Analyze` path, bounded by `agents.optimizer.max_attempts_per_bottleneck` (default 3), with `previous_attempts` threaded into the Analyzer/Optimizer context. The hardware-gated test prints `e2e skipped` and passes when `CRUCIBLE_E2E` is unset.

Game-mode status (2026-06-12): **verified on real hardware.** The 7900 XT passes through to the guest (RADV NAVI31), vkmark renders real frames, and MangoHud produces a parseable frame-time CSV. Hard-won constraints, all encoded in code — do not regress them:
- The GPU is a **4-function device** (VGA/audio/USB/UCSI in separate IOMMU groups); QEMU's bus reset needs all of them on vfio-pci. `VmManager::vfio_sibling_functions` discovers them, `validate_passthrough` checks each, and `setup-host.sh` binds the whole slot.
- `rombar=0` on the VGA function is mandatory — QEMU hangs forever reading the option ROM of a GPU the host previously drove.
- The guest runs vkmark with `--winsys headless`: the default kms winsys presents via raw DRM atomic commits with no VkSwapchainKHR, so MangoHud's present hook records nothing.
- MangoHud only flushes its CSV when logging stops *before* the app exits, and `no_display` starves the logger. The guest derives a finite `log_duration` from `LaunchBenchmark.duration_secs` (wire field, three-file rule) and keeps the HUD enabled.
- `KernelBuilder` runs `make modules_install INSTALL_MOD_PATH=.virtme_mods` and `VmManager` overlays it via `--rodir` — vng with `--root` resolves modules only from inside the rootfs, and the test kernel needs `CONFIG_DRM_AMDGPU=m` for the guest to drive the card.
- vng's QEMU grandchild survives `kill_on_drop` (`sh -c` chain); `VmManager` spawns into a process group and `shutdown` kills the group, otherwise a leaked QEMU holds vsock CID 3 across runs. **`shutdown` must also drain the group before returning** (`wait_for_process_group_exit` polls `kill(-pgid, 0)` until ESRCH + a 300ms settle): `child.wait()` reaps only the direct vng wrapper, but the QEMU grandchild dies asynchronously and keeps the GPU's `/dev/vfio/<group>` open. This is what lost GPU passthrough across a **kernel-patch reboot** — boot 2 raced the dying QEMU and hit `Could not open '/dev/vfio/14': Device or resource busy` → GPU never attached → guest never booted → downstream vsock ENODEV (misleading; the real error is only in the vng/QEMU console). `boot` now tees that console to `<kernel_src>/../crucible-vm-boot.log`. Verified: baseline 14k fps → amdgpu patch → rebuild → reboot → comparison 14k fps, 0 vfio-busy. **The game-mode kernel-patch loop (patch → rebuild → reboot → re-measure on the passthrough GPU) now works** — this was the last blocker.
- The profiler prompt forbids fabricating metrics on tool failure (`{"error": …}` instead of zeros); zero `fps_avg` previously masked a complete VFIO failure as a successful cycle.

Perfetto loop status (2026-07-01): **the full optimization loop closed live** on vkmark + the 7900 XT (`CRUCIBLE_E2E_GPU=1`, ~320s): clean baseline → Perfetto-profiled repeat → analyzer mines the trace → optimizer emits a compiling `kernel/sched/fair.c` patch → `KernelBuilder` applies + rebuilds → comparison → 5 metrics evaluated. How the profiling stage works — constraints are encoded, do not regress them:
- The trace is captured in the **baseline** phase as a separate profiled repeat after the clean run (Analyze runs before ComparisonMeasurement, so a comparison-phase trace would arrive too late; the clean run stays unprofiled so tracing overhead can't skew reported numbers). `measurement_context` threads `capture_perfetto`/`perfetto_output`/`perfetto_host_dir` in game mode; the analyzer context gets `trace_paths` from the profiler's `collection_paths.traces`.
- Guest capture: `_ensure_perfetto_daemons` spawns `traced` + `traced_probes` first (the `perfetto` CLI is only a consumer of the traced socket), logging to `/tmp/crucible_*.log` — never DEVNULL, a silent daemon death already cost a blind debugging cycle. The client rejects `-c -` together with `--time`, so the capture bound is a `duration_ms:` line prepended to the stdin config (`PERFETTO_KERNEL_CONFIG`: sched switch/wakeup/waking, cpu freq/idle, IRQ/softirq, compact_sched).
- perfetto only writes the trace file when the capture **ends**: the profiler calls `stop_profiling` (guest TERMs the client with `pkill -x` and waits for exit = flush complete) before `fetch_perfetto_trace`, which itself retries on empty. Traces land in `config.orchestrator.artifact_dir`.
- Debian trixie does not package perfetto — both rootfs scripts copy the **host Ubuntu binaries** (`perfetto`, `traced`, `traced_probes`, plus `libperfetto.so` + an `ld.so.conf.d` entry + `ldconfig`; the lib is what makes or breaks daemon startup). The analyzer's `run_trace_processor_query` uses the `perfetto` PyPI package (uv dependency) with `trace_processor_shell` as fallback.
- Known gap: the optimizer's `sysctl_changes` output is **not applied in the guest**, so a knob-exposing patch alone is behavior-neutral and the comparison measures an unchanged kernel — wire sysctl application after ApplyChanges before trusting fps deltas. Single-run phases produce all-Neutral verdicts by design (Welch needs ≥2 samples/side).

Kernel-patch corpus mode (2026-07-02): the loop generates real, profile-guided kernel **source** patches (not just sysctl knobs) — the primary user goal. Runs on the synthetic memory workload (`mode="synthetic"`, `stress-ng --vm --vm-method flip`, `vfio_device="none"`) so patch → build → **reboot** → re-measure cycles are clean (no GPU passthrough to lose; sidesteps the task-10 reboot bug). Config `~/.crucible/synth-grind.toml`, `allowed_layers=["kernel"]`, `runs_per_phase≥10`. Each cycle reverts its patch and starts from the base kernel; diffs persist in `linux/.crucible_patches/`. Four fixes made it work, do not regress:
  - **Analyzer is workload-aware** (`workload_kind`/`workload_args` in its context). It used to treat synthetic stress-ng traces as failed *game* captures (no wine/proton threads) → empty bottleneck → optimizer no-op. For synthetic it's told the stress-ng + mm kernel threads (kcompactd/khugepaged/kswapd/rcu) ARE the signal and to name the kernel source area.
  - **Cross-cycle diversity** via `explored_areas` (`db.list_all_patch_diffs` → basenames, threaded into analyzer+optimizer). Without it every cycle re-derived the same mm bottleneck and produced near-duplicate patches; with it the corpus spans khugepaged → kcompactd → page_alloc → RCU etc.
  - **Zero-frame-time samples are rejected** in `persist_measurement`: an intermittent `stress-ng` run returning ops/sec=0 makes the profiler emit `frame_time_p99_ms=0.0`; one such 0 among ~0.0039 fabricated a spurious -10% delta at 33% CV. A valid run always has positive frame time.
  - **Optimizer verifies APIs exist** before use (a well-reasoned `khugepaged mmap_write_trylock` patch failed to build — the fn wasn't visible in-tree). It's told to `search_kernel_source` for the definition/header first and fall back to APIs the file already uses.
  - Reality: LLM patches are plausible, mostly compile, and are grounded in real trace numbers (e.g. a kcompactd nice-boost citing ">8x runnable-wait vs on-CPU"), but are **Neutral on the microbenchmark** — proving benefit needs a workload that stresses each patched path. The corpus + human triage is the deliverable; measurable-on-synthetic is a bonus. Build-failure auto-reverts (`KernelBuilder`) and the cycle continues.

Game-mode kernel-patch corpus (2026-07-02, `~/.crucible/game-grind.toml`, vkmark + 7900 XT): ran **10 full E2E loops** (baseline → profiled re-run → analyze → kernel patch → rebuild → reboot → re-measure). 10 distinct patches across 8 subsystems (drm_atomic_helper, drm scheduler, CFS fair ×2, RCU ×2, workqueue, amdgpu IH, softirq, cpuidle); **0 vfio-busy across all 10 reboots** (the shutdown-drain fix held). **2 statistically-significant wins** (Welch p<0.05): `sched-fair-sis-util-idle-floor` (`select_idle_cpu`: probe up to 4 CPUs instead of `return -1` when `nr_idle_scan` reads the LLC as overloaded — improved fps_avg +6.5% / fps_p1 +19% / frame_time_p99 -15%) and `drm-sched-wq-highpri` (`WQ_HIGHPRI` on the DRM sched ordered wq — fps_p1 +58%). Caveats: single measurement run per phase + vkmark variance → accepts need **confirmation runs** (repeat N times) before trusting magnitudes; some patches regressed (the loop proposes, the data rejects). Diffs in `linux/.crucible_patches/`. **Gap for the gaming talk: workload is vkmark, not a shipping game** (Aspyr GL on RADV headless — see the game-mode caveats above and the plan doc).

Orchestrator robustness gaps observed during the grind (not yet fixed): (1) **no backoff on agent failure** — when the Claude API hit a spend cap, GameSelector failed instantly and the loop burned all `--max-cycles` in seconds; a rate-limit/backoff branch would pause instead. (2) A **harness-killed orchestrator leaks its QEMU** (the process isn't reaped through `shutdown`), holding the GPU + vsock CID; manual cleanup (`pkill qemu` + `git checkout` the kernel tree) is needed. Launch long grinds with `setsid` so they survive the parent shell's process-group teardown.

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

`[measurement] mode` selects the profiler path. `"synthetic"` (default) drives `stress-ng` via the guest RPC and is the only path the bookworm rootfs from `scripts/setup-rootfs.sh` supports. `"game"` drives the native GPU benchmark selected by `game_benchmark` (`vkmark` default, `glmark2` alternative) under MangoHud and needs the trixie rootfs from `scripts/setup-game-rootfs.sh`. `benchmark_args` and `benchmark_duration_secs` configure the synthetic workload.

## Conventions specific to this repo

- **Wire types are duplicated across Rust and Python.** Treat `crucible-common::protocol`, `agents/common/protocol.py`, and `guest/protocol.py` as one logical schema in three files. Tests in `tests/python/test_protocol.py` and `crates/crucible-common/src/protocol.rs` exist to catch drift. `guest/protocol.py` is intentionally stdlib-only (`@dataclass` + `to_dict`/`from_dict`/`to_json`/`from_json`) — Debian bookworm ships pydantic v1 and the agent has to import there too. The Anthropic-side host agents still use pydantic v2 freely; just don't introduce pydantic types in the guest module.
- **Agents do not import `crucible-orchestrator` or talk to SQLite.** All persistence goes through the orchestrator. If an agent needs prior cycle data, the orchestrator passes it in via `TaskEnvelope.context`.
- The `agents.*` and `guest.*` packages have no `setup.py`/`pyproject` install — they are imported by path. Always set `PYTHONPATH=.` (the workspace root) when running Python directly. The orchestrator does this automatically when spawning agents.
- Guest-agent RPC is **length-prefixed JSON over vsock** (4-byte big-endian length, then payload), not newline-delimited. See `guest/crucible_guest_agent.py:_recv_message`. The host-side counterpart is `agents/common/guest_rpc.py::GuestRpc` (connect-per-call AF_VSOCK).
- Claude-backed agents (anything subclassing `ClaudeAgentBase`) return `{"response": "<final assistant text>"}` in their `ResultEnvelope.result`. The orchestrator uses `parse_agent_response()` to unwrap that envelope, optionally strip ` ```json ` fences, and parse the inner JSON. Use it whenever consuming a Claude agent's structured output.
- The minimal guest rootfs is built by `scripts/setup-rootfs.sh` using `mmdebstrap --mode=root` (auto-elevates with sudo) into `~/.crucible/rootfs`. It installs `systemd-sysv`, `udev` (required for `/dev/virtio-ports/*` symlinks that virtme-init looks for), `python3`, `stress-ng`, `linux-perf`, `dbus`, `kmod`, and enables `crucible-guest-agent.service` plus a oneshot `crucible-cgroups.service`. No `python3-pydantic` — the guest agent uses stdlib only. The script fails fast if `mmdebstrap` is missing (no silent `debootstrap` fallback). On hosts without `debian-archive-keyring` (Ubuntu) the bootstrap runs with apt's insecure-repo options. Idempotent via the `.crucible-built` stamp file in the target.
- The game rootfs is built by `scripts/setup-game-rootfs.sh` into `~/.crucible/game-rootfs` on Debian **trixie** (bookworm's Mesa 22.x predates usable RDNA3 support; trixie ships Mesa 25.x) with `mesa-vulkan-drivers`, `vulkan-tools`, `vkmark`, `glmark2`, `glmark2-drm`, `mangohud`, and `firmware-amd-graphics` (non-free-firmware component). vkmark/glmark2 render via DRM/KMS directly — no compositor in the guest; vng already passes `-display none` to QEMU, so don't add another. Stamp file `.crucible-game-built`. All rootfs scripts share `scripts/lib/rootfs-common.sh`.
- The steam rootfs is built by `scripts/setup-steam-rootfs.sh` into `~/.crucible/steam-rootfs` (trixie + i386 multiarch + steam-installer/steamcmd/weston/xwayland/dbus-x11/isc-dhcp-client). It extracts the Steam client bootstrap directly (never runs the Debian wrapper), creates `~/.steam/{steam,root}` symlinks, adds the `crucible` user to `video`+`render` (weston EGL dies without them), seeds the steamcmd session + game library + **full-client login creds** (`CRUCIBLE_STEAM_CLIENT_CREDS`, default the snap Steam dir — the client JWT lives in `local.vdf` + `config/loginusers.vdf`, which steamcmd never writes), and copies the host's perfetto binaries. Stamp `.crucible-steam-built`. The seeded library must be kept current with host steamcmd (recipe in the script header) — the client force-updates stale apps and the download dies in the ephemeral overlay.
