# Virt lane: synthetic mode + kernel-patch corpus grind

`[measurement] mode = "synthetic"` (the default) is the only path the
bookworm bench rootfs supports, and the platform for the kernel-patch
corpus grind (real profile-guided kernel SOURCE patches -- the primary
user goal -- with clean patch -> build -> reboot -> re-measure cycles
and no GPU passthrough to lose).

## Profiler path

The profiler agent calls `run_benchmark('stress-ng', args,
duration_secs)` via the guest RPC, parses ops/sec from
`--metrics-brief`, and emits `fps_avg = fps_p1 = 0`,
`frame_time_p99_ms = 1000 / ops_per_sec`, `psi_*_avg = psi_*_delta`.
(`fps_avg = 0` is expected here; in game/steam modes non-zero fps_avg
is the real-frames discriminator.)

## Corpus-grind configuration

Synthetic memory workload: `stress-ng --vm --vm-method flip`,
`vfio_device = "none"`, `allowed_layers = ["kernel"]`,
`runs_per_phase >= 10` (config `~/.crucible/synth-grind.toml`). Each
cycle reverts its patch and starts from the base kernel; diffs persist
in `linux/.crucible_patches/`.

## The four loop fixes that make it work (do not regress)

- **Analyzer is workload-aware** (`workload_kind`/`workload_args` in
  its context). It used to treat synthetic stress-ng traces as failed
  GAME captures (no wine/proton threads) -> empty bottleneck ->
  optimizer no-op. For synthetic it is told the stress-ng + mm kernel
  threads (kcompactd/khugepaged/kswapd/rcu) ARE the signal and to name
  the kernel source area.
- **Cross-cycle diversity** via `explored_areas`
  (`db.list_all_patch_diffs` -> basenames, threaded into analyzer +
  optimizer). Without it every cycle re-derived the same mm bottleneck
  and produced near-duplicate patches; with it the corpus spans
  khugepaged -> kcompactd -> page_alloc -> RCU etc.
- **Zero-frame-time samples are rejected** in `persist_measurement`:
  an intermittent stress-ng run returning ops/sec=0 makes the profiler
  emit `frame_time_p99_ms = 0.0`; one such 0 among ~0.0039 fabricated
  a spurious -10% delta at 33% CV. A valid run always has positive
  frame time.
- **Optimizer verifies APIs exist** before use (a well-reasoned
  `khugepaged mmap_write_trylock` patch failed to build -- the fn was
  not visible in-tree). It is told to `search_kernel_source` for the
  definition/header first and fall back to APIs the file already uses.

## Reality check

LLM patches are plausible, mostly compile, and are grounded in real
trace numbers (e.g. a kcompactd nice-boost citing ">8x runnable-wait
vs on-CPU"), but are **Neutral on the microbenchmark** -- proving
benefit needs a workload that stresses each patched path. The corpus +
human triage is the deliverable; measurable-on-synthetic is a bonus.
Build failures auto-revert (`KernelBuilder`) and the cycle continues.
