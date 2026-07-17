# Crucible: Agentic Linux Gaming Performance Optimization

**Status:** Original system design (April 2026). The architecture here
(two-process split, state machine, agent contract, evaluation) is still
the map of the system. Operational details have drifted since -- vng
flags, model names, script paths (scripts/ is now testbed/virt/), the
Agent SDK migration, and the project-structure tree below predate the
testbed/patches/skills reorg. CLAUDE.md is authoritative wherever the
two disagree.

## Overview

Crucible is a closed-loop agentic AI system that continuously measures and
optimizes gaming performance on Linux. It orchestrates a set of specialized AI
agents that select games, run benchmarks, collect performance profiles,
identify bottlenecks, and generate code changes across the full stack -- from
the Linux kernel through userspace compositors, Wine/Proton, Mesa, and game
engines.

The system runs on a single machine, using a QEMU/KVM virtual machine with GPU
passthrough for safe experimentation. A Rust orchestrator daemon manages the
optimization loop, while Python agents powered by Claude API handle the
reasoning-heavy tasks.

## Hardware

- **CPU:** AMD Ryzen 9 7950X (16-core, integrated RDNA 2 GPU)
- **GPU:** PowerColor AMD Radeon RX 7900 XT (discrete, passed through to guest VM)
- **Host display:** Ryzen iGPU drives the host
- **Guest display:** 7900 XT via VFIO passthrough for real gaming workloads

## Architecture

### Approach: Orchestrator Process + Agent Workers

```
Host Machine (Ryzen 7950X iGPU)
|
+-- crucible-orchestrator (Rust daemon, systemd-managed)
|   +-- State machine (optimization loop lifecycle)
|   +-- SQLite DB (runs, measurements, patches, results)
|   +-- VM Manager (virtme-ng, 7900 XT passthrough)
|   +-- Agent Runner (spawns Python agent processes)
|   +-- IPC layer (stdin/stdout JSON protocol)
|
+-- Python Agents (invoked by orchestrator)
|   +-- game-selector    -> picks game + benchmark/play strategy
|   +-- game-player      -> drives gameplay via vision + input (V2)
|   +-- profiler         -> configures and collects measurements
|   +-- analyzer         -> interprets profiles, identifies bottlenecks
|   +-- optimizer        -> generates code changes
|
+-- Guest VM (QEMU/KVM via virtme-ng, 7900 XT passthrough)
|   +-- Custom kernel (built from patched source)
|   +-- Userspace under test (Wayland/gamescope, Wine/Proton, Mesa)
|   +-- Steam + target game
|   +-- Profiling tools (perf, perfetto, MangoHud, PSI collector)
|   +-- crucible-guest-agent (vsock RPC daemon)
|
+-- Artifact Store
    +-- kernel sources + patches
    +-- profiling data (perfetto traces, PSI logs, frame times)
    +-- measurement history
    +-- build cache (sccache)
```

### Key Design Decisions

- **Rust orchestrator + Python agents:** Rust for the long-running daemon (reliability, concurrency via tokio, no GC). Python for agent logic (rapid iteration, first-party Anthropic SDK, subprocess ergonomics).
- **Agents are stateless workers:** They receive context from the orchestrator and return results. They don't read/write the SQLite DB or communicate with each other directly.
- **virtme-ng for VM management:** Purpose-built for kernel development. Fast kernel boot cycles. GPU passthrough via `--qemu-opts`. Simpler than libvirt for a single-purpose VM. Plan to fall back to direct QEMU invocation if virtme-ng's abstraction becomes limiting.
- **vsock for host-guest RPC:** No network configuration needed. Fast. Works with virtme-ng.
- **SQLite for state:** Lightweight, single-file, survives crashes. No external database dependency.

## Rust Orchestrator

### State Machine

The core optimization loop is an explicit state machine with persisted transitions:

```
Idle
  -> SelectGame
    -> ProvisionVM (boot or reconfigure)
      -> BaselineMeasurement
        -> Analyze
          -> GenerateOptimization
            -> ApplyChanges
              -> ComparisonMeasurement
                -> Evaluate
                  -> Accept | Reject | Iterate
                    -> Idle (or back to Analyze)
```

Each state transition is written to SQLite before executing. On crash/restart, the orchestrator reads the last committed state and resumes. Incomplete states trigger rollback (e.g., crash during `ApplyChanges` -> revert patch, re-enter `Analyze`).

### SQLite Schema (core tables)

- **cycles** -- one row per optimization cycle (id, game, status, started_at, completed_at)
- **measurements** -- profiling data per run (cycle_id, phase [baseline|comparison], fps_avg, fps_p1, frame_time_p99, psi_cpu_avg, psi_memory_avg, custom metrics as JSON)
- **patches** -- generated changes (cycle_id, layer [kernel|userspace|tuning], diff_path, applied_at, reverted_at)
- **evaluations** -- delta analysis (cycle_id, metric, baseline_value, comparison_value, delta_pct, verdict)
- **vm_state** -- current VM config (kernel_version, applied_patches, gpu_passthrough_status)

### VM Management

Uses virtme-ng to manage QEMU/KVM:

- Persistent guest rootfs directory on host (shared into guest). Steam games and cached data persist across cycles.
- Kernel under test is built on the host and booted via `vng --boot --kernel /path/to/bzImage`.
- VFIO passthrough for 7900 XT via `--qemu-opts "-device vfio-pci,host=0a:00.0"`.
- vsock channel via `--qemu-opts "-device vhost-vsock-pci,guest-cid=3"`.
- Boot timeout: 60 seconds. No heartbeat = kill VM, revert to known-good kernel.

**VM progression plan:**
1. Phase 1: virtme-ng for kernel-focused optimization (no GPU passthrough)
2. Phase 2: virtme-ng + `--qemu-opts` for GPU passthrough
3. Phase 3: Direct QEMU invocation if virtme-ng becomes limiting

### Agent Runner

Spawns Python agent processes with stdin/stdout JSON protocol:

```
Orchestrator                          Agent (Python process)
    |                                      |
    +--- spawn process -----------------> |
    +--- send task (JSON over stdin) ---> |
    |                                      +-- calls Claude API
    |                                      +-- uses tools
    |                                      +-- streams progress (stdout)
    |<--- receive result (JSON) ---------- |
    |                                      |
    +--- kill / timeout ----------------> |
```

- Configurable timeouts per agent invocation
- No shared state -- agents receive everything in the task envelope and return structured results
- Stderr captured for logging

### Safety / Rollback

- **Kernel changes:** VM always has a known-good kernel. Failed boot (no heartbeat within timeout) -> destroy VM, restart with last working kernel. Patch recorded as "failed-boot."
- **Userspace changes:** Overlay approach. Revert = discard changes and restart from clean rootfs state.
- **Tuning changes:** Stored as before/after values. Revert = re-apply "before" values.
- **Cumulative regression detection:** If performance degrades across multiple cycles despite individual patches showing improvement, the orchestrator bisects by reverting patches in order.

## Python Agents

### Agent Interface Contract

Every agent reads a JSON task envelope from stdin, calls Claude API with agent-specific system prompt and tools, writes a JSON result envelope to stdout, and exits.

**Task envelope (orchestrator -> agent):**
```json
{
    "task_id": "uuid",
    "agent": "analyzer",
    "context": {},
    "config": {
        "model": "claude-sonnet-4-6-20250414",
        "max_tokens": 8192,
        "timeout_seconds": 300
    }
}
```

**Result envelope (agent -> orchestrator):**
```json
{
    "task_id": "uuid",
    "status": "success",  // or "failure" or "needs_input"
    "result": {},
    "usage": {
        "input_tokens": 1234,
        "output_tokens": 567,
        "api_calls": 3
    },
    "logs": []
}
```

### Agent Definitions

**1. Game Selector**

- **Input:** Installed games list, previous optimization history, optimization goals
- **Tools:** Steam library query, benchmark detection, game metadata
- **Output:** Game ID, launch command, benchmark method (built-in or AI-play), expected workload profile
- **Role:** Reason about which game is most informative to test next. Prioritize games that exercise code paths related to recent changes or haven't been profiled recently.

**2. Game Player (V2 -- fallback when no built-in benchmark)**

- **Input:** Game ID, launch command, target duration, play style goals
- **Tools:** Screen capture (via guest RPC), synthetic input injection (uinput), game state detection
- **Output:** Session recording metadata, duration played, screenshots
- **Role:** Vision-based gameplay. Receives screenshots, decides actions, sends input commands. Aims for "normal user" behavior. Last agent to be built.

**3. Profiler**

- **Input:** Game ID, measurement phase (baseline vs comparison), metrics to collect
- **Tools:**
  - Start/stop perfetto traces
  - Configure MangoHud overlay logging
  - System-wide PSI from `/proc/pressure/{cpu,memory,io}`
  - Per-cgroup PSI from cgroupfs for rendering pipeline processes
  - Cgroup hierarchy setup and process classification
  - perf stat/record
  - Custom eBPF probes
- **Output:** Paths to trace files, summary statistics
- **Role:** Decide which profiling tools to deploy based on the optimization target. Configure cgroup hierarchy:
  - `crucible/game` -- game process
  - `crucible/compositor` -- gamescope
  - `crucible/wine` -- Wine/Proton server processes
  - `crucible/mesa` -- shader compiler threads
  - `crucible/system` -- everything else

**4. Analyzer**

- **Input:** Profiling data (traces, metrics, frame times), previous analysis history, current optimization hypothesis
- **Tools:** Read perfetto traces (trace_processor_shell), parse MangoHud CSV, statistical comparison, read kernel source
- **Output:** Bottleneck identification, root cause hypothesis, optimization targets with confidence levels
- **Role:** Core reasoning agent. Reads profiling data, identifies hotspots, correlates stalls with subsystems, produces ranked optimization opportunities.

**5. Optimizer**

- **Input:** Analysis results, target subsystem, current source code
- **Tools:** Read/write source files, generate patches (diff -u), invoke builds, run unit tests
- **Output:** Patch file(s), build status, change description and rationale
- **Role:** Generate code changes -- kernel scheduler tweaks, Mesa optimizations, Wine/Proton workarounds, sysctl tuning. Reads relevant source, understands the bottleneck, produces targeted patches.

## Guest VM Environment

### Base Guest Environment

- Steam (installed, logged in with cached credentials)
- Target games (pre-downloaded)
- Profiling tools: perfetto, perf, MangoHud, radeontop
- crucible-guest-agent (vsock RPC daemon)
- cgroup v2 hierarchy pre-configured
- gamescope as Wayland compositor

### Boot Flow

```
1. Orchestrator calls virtme-ng:
   vng --boot \
       --kernel /path/to/bzImage \
       --root /path/to/guest-rootfs \
       --qemu-opts "-device vfio-pci,host=0a:00.0 \
                     -m 16G -smp 8 \
                     -device vhost-vsock-pci,guest-cid=3"

2. Guest boots, systemd starts:
   - crucible-guest-agent.service (vsock RPC daemon)
   - gamescope (Wayland compositor)
   - cgroup hierarchy setup

3. Orchestrator detects guest ready via vsock heartbeat

4. Orchestrator dispatches agent tasks into the guest
```

Root filesystem: persistent directory on host shared into guest via virtme-ng. Steam games persist across cycles. Kernel and userspace under test are overlaid on top.

### Host-Guest RPC via vsock

crucible-guest-agent is a lightweight Python daemon (~200-300 lines) that listens on vsock port 5000 and handles:

- Health checks
- Cgroup hierarchy creation and process classification
- Game launching via Steam CLI
- MangoHud configuration and log collection
- Perfetto trace session management
- PSI snapshot collection (system-wide and per-cgroup)
- Screenshot capture (gamescope or grim)
- Synthetic input injection via uinput
- File transfer back to host for analysis

### Kernel Build Pipeline

```
1. Orchestrator receives patch from optimizer agent
2. Apply: cd /home/void/upstream/questing && git apply patch.diff
3. Build: vng --build bzImage modules (sccache for caching)
4. Boot new VM with patched kernel via virtme-ng
5. If boot fails (no heartbeat within 60s):
   - Kill VM
   - Revert: git checkout -- .
   - Record failure
   - Resume with known-good kernel
```

Incremental kernel build on 7950X with warm sccache: ~30-90 seconds for localized patches. Full rebuild: ~5-10 minutes.

### Userspace Build Pipeline

```
1. Orchestrator receives patch from optimizer agent
2. Apply patch to relevant source tree
3. Build (Mesa: meson compile, Wine: make -j16, gamescope: meson compile)
4. Updated binaries available in shared rootfs
5. Restart relevant service or reboot VM
6. Rollback: revert patch if build fails or service unhealthy
```

## Optimization Loop Lifecycle

### Cycle Structure

Each cycle has a hypothesis -- a specific, testable claim:

```
1. SELECT game + workload
2. MEASURE baseline (N runs for statistical confidence)
3. ANALYZE profiles -> identify bottleneck
4. HYPOTHESIZE -> e.g. "reducing kcompactd wake-ups during shader
                  load will reduce frame time p99 by ~10%"
5. IMPLEMENT -> generate patch
6. MEASURE comparison (N runs, same workload)
7. EVALUATE -> statistically significant improvement?
   +-- YES -> accept, record, move to next bottleneck
   +-- NO CHANGE -> discard, refine hypothesis, retry (max 3)
   +-- REGRESSION -> revert, blacklist approach
8. PERSIST results and loop
```

### Measurement Protocol

**Baseline establishment:**
- Run benchmark/workload N times (default 5)
- Discard first run (cold cache, shader compilation, JIT warmup)
- Compute mean and standard deviation per metric
- If stddev > 10% of mean for key metrics, increase N or investigate variance

**Comparison:** Same workload, same N runs, same discard policy. Same VM config except for the change under test.

**Key metrics per run:**

| Metric | Source | Purpose |
|--------|--------|---------|
| FPS avg | MangoHud | Overall throughput |
| FPS 1% low | MangoHud | Worst-case sustained performance |
| Frame time p50/p95/p99 | MangoHud | Latency distribution and stutter |
| CPU PSI avg (system) | /proc/pressure/cpu | System-wide CPU contention |
| CPU PSI avg (per-cgroup) | cgroupfs | Per-component CPU pressure |
| Memory PSI avg (per-cgroup) | cgroupfs | Per-component memory pressure |
| IO PSI avg (per-cgroup) | cgroupfs | Per-component IO pressure |
| Context switches/sec | perf stat | Scheduler overhead |
| Page faults/sec | perf stat | Memory subsystem pressure |
| GPU utilization % | radeontop or amdgpu sysfs | GPU saturation |
| Shader compilation time | perfetto trace | One-time vs steady-state cost |

### Statistical Evaluation

- **Welch's t-test** on primary metric (frame time p99 by default), significance at p < 0.05
- **Cohen's d** for effect size -- is the difference meaningful, not just detectable?
- **Regression check:** All metrics compared, not just the target. Improvement in one metric that degrades another is flagged as a tradeoff.

**Verdicts:**
- **ACCEPT:** Target metric improved, p < 0.05, Cohen's d > 0.5, no regression in other metrics
- **MARGINAL:** Improvement detected but small effect size (d < 0.5) or borderline p -- record but don't prioritize
- **NEUTRAL:** No statistically significant difference -- discard patch
- **REGRESSED:** Any metric significantly worsened -- revert and blacklist

### Iteration Strategy

- 3 attempts per bottleneck before moving on
- Attempt 1: direct fix from analyzer recommendation
- Attempt 2: analyzer reviews why attempt 1 failed, refines hypothesis
- Attempt 3: different approach to same bottleneck
- After 3 failures: mark as "investigated, no improvement found," move on

### Cross-Game Regression Testing

When a patch is accepted for one game, the orchestrator queues regression runs against previously tested games:
- Not every game every time
- Priority: 3 most recently optimized games + games exercising the same subsystem
- Regressions flagged for review rather than auto-reverted (tradeoff, not clear bug)

### Long-Term Tracking

SQLite accumulates history of:
- Bottlenecks identified per game
- Patches tried, accepted, rejected
- Performance trajectory per game over time
- Which subsystems yield the most improvement (guides future analysis priority)

## Project Structure

```
crucible/
+-- Cargo.toml                     # Rust workspace root
+-- pyproject.toml                 # Python project config (agents)
|
+-- crates/
|   +-- crucible-orchestrator/     # Main daemon binary
|   |   +-- Cargo.toml
|   |   +-- src/
|   |       +-- main.rs            # Entry point, CLI, systemd integration
|   |       +-- state_machine.rs   # Optimization cycle state machine
|   |       +-- db.rs              # SQLite persistence layer
|   |       +-- vm.rs              # virtme-ng wrapper, VM lifecycle
|   |       +-- agent_runner.rs    # Spawn Python agents, stdin/stdout IPC
|   |       +-- evaluator.rs       # Statistical evaluation
|   |       +-- config.rs          # Configuration loading
|   |
|   +-- crucible-common/           # Shared types, protocol definitions
|       +-- Cargo.toml
|       +-- src/
|           +-- lib.rs
|           +-- protocol.rs        # JSON task/result envelope types
|           +-- metrics.rs         # Metric types
|
+-- agents/                        # Python agent implementations
|   +-- common/
|   |   +-- __init__.py
|   |   +-- agent_base.py          # stdin/stdout protocol, Claude API wrapper
|   |   +-- tools.py               # Shared tool definitions
|   |   +-- metrics.py             # Metric types
|   +-- game_selector/
|   +-- profiler/
|   +-- analyzer/
|   +-- optimizer/
|   +-- game_player/               # V2
|
+-- guest/                         # Guest VM components
|   +-- crucible-guest-agent.py    # vsock RPC daemon
|   +-- crucible-guest-agent.service
|   +-- setup-cgroups.sh
|   +-- gamescope-session.sh
|
+-- config/
|   +-- crucible.toml              # Orchestrator config
|   +-- vm.toml                    # VM settings
|   +-- agents.toml                # Per-agent config
|
+-- scripts/
|   +-- setup-host.sh              # VFIO, IOMMU, dependency setup
|   +-- setup-rootfs.sh            # Guest rootfs with Steam + tools
|   +-- install-systemd.sh         # systemd service installation
|
+-- skills/
    +-- architecture/
```

## Configuration

```toml
[orchestrator]
db_path = "~/.crucible/state.db"
artifact_dir = "~/.crucible/artifacts"
max_cycles = 0              # 0 = unlimited
cycle_cooldown_secs = 60

[vm]
kernel_src = "/home/void/linux"
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
enabled = false             # V2 feature
```

## First Milestone

Single game, full stack. Prove the complete closed loop works end-to-end:

1. Game selector picks a game with a built-in benchmark (e.g. Shadow of the Tomb Raider, Cyberpunk 2077)
2. Baseline measurement with 5 runs
3. Analyzer identifies a bottleneck
4. Optimizer generates a patch (kernel, userspace, or tuning)
5. Comparison measurement with 5 runs
6. Statistical evaluation determines if the patch helped
7. Accept or reject, loop

Game player agent (AI-driven gameplay via vision + input) is deferred to V2.
