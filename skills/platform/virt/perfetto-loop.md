# Virt lane: Perfetto profiling stage

How the optimization loop captures and mines kernel-scheduler traces
in the VM (closed live 2026-07-01 on vkmark + the 7900 XT: clean
baseline -> Perfetto-profiled repeat -> analyzer mines the trace ->
optimizer emits a compiling kernel patch -> rebuild -> comparison ->
5 metrics evaluated). Constraints are encoded in code -- do not
regress them.

## Capture placement

The trace is captured in the **baseline** phase as a separate profiled
repeat after the clean run: Analyze runs before ComparisonMeasurement,
so a comparison-phase trace would arrive too late, and the clean run
stays unprofiled so tracing overhead cannot skew reported numbers.
`measurement_context` threads `capture_perfetto` / `perfetto_output` /
`perfetto_host_dir` in game mode; the analyzer context gets
`trace_paths` from the profiler's `collection_paths.traces`.

## Guest capture mechanics

- `_ensure_perfetto_daemons` spawns `traced` + `traced_probes` first
  (the `perfetto` CLI is only a consumer of the traced socket),
  logging to `/tmp/crucible_*.log` -- never DEVNULL; a silent daemon
  death already cost a blind debugging cycle.
- The client rejects `-c -` together with `--time`, so the capture
  bound is a `duration_ms:` line prepended to the stdin config
  (`PERFETTO_KERNEL_CONFIG`: sched switch/wakeup/waking, cpu
  freq/idle, IRQ/softirq, compact_sched).
- perfetto only writes the trace file when the capture ENDS: the
  profiler calls `stop_profiling` (guest TERMs the client with
  `pkill -x` and waits for exit = flush complete) before
  `fetch_perfetto_trace`, which itself retries on empty. Traces land
  in `config.orchestrator.artifact_dir`.

## Toolchain packaging

Debian trixie does not package perfetto -- both rootfs scripts copy
the host Ubuntu binaries (`perfetto`, `traced`, `traced_probes`, plus
`libperfetto.so` + an `ld.so.conf.d` entry + `ldconfig`; the lib is
what makes or breaks daemon startup). The analyzer's
`run_trace_processor_query` uses the `perfetto` PyPI package (uv
dependency) with `trace_processor_shell` as fallback.

## Known gaps

- The optimizer's `sysctl_changes` output is NOT applied in the guest,
  so a knob-exposing patch alone is behavior-neutral and the
  comparison measures an unchanged kernel -- wire sysctl application
  after ApplyChanges before trusting fps deltas from such patches.
- Single-run phases produce all-Neutral verdicts by design (Welch
  needs >=2 samples per side).
