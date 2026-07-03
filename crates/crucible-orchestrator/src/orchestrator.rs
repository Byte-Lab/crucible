use anyhow::Result;
use crucible_common::protocol::{AgentConfig, AgentName, TaskEnvelope};
use std::path::PathBuf;
use std::time::Duration;
use uuid::Uuid;

use crate::agent_runner::AgentRunner;
use crate::config::CrucibleConfig;
use crate::db::{Database, Measurement};
use crate::evaluator::{evaluate_metric, EvalConfig, MetricEvaluation, Verdict};
use crate::kernel_builder::KernelBuilder;
use crate::state_machine::{CycleState, StateMachine};
use crate::vm::{VmManager, VmState};
use crate::vsock_client::VsockClient;

/// Metrics persisted per measurement, paired with whether lower values are better.
const METRIC_DEFS: &[(&str, bool)] = &[
    ("fps_avg", false),
    ("fps_p1", false),
    ("frame_time_p99_ms", true),
    ("psi_cpu_avg", true),
    ("psi_memory_avg", true),
];

/// Mean of each metric across a phase's persisted rows, as the JSON blob
/// the Analyzer consumes (its `metrics` context). Averaging the samples
/// gives the analyzer a stable per-metric value instead of one noisy run.
fn mean_metrics(measurements: &[Measurement]) -> serde_json::Value {
    let mean = |metric: &str| -> f64 {
        let s = metric_samples(measurements, metric);
        if s.is_empty() {
            0.0
        } else {
            s.iter().sum::<f64>() / s.len() as f64
        }
    };
    serde_json::json!({
        "fps_avg": mean("fps_avg"),
        "fps_p1": mean("fps_p1"),
        "frame_time_p99_ms": mean("frame_time_p99_ms"),
        "psi_cpu_avg": mean("psi_cpu_avg"),
        "psi_memory_avg": mean("psi_memory_avg"),
        "samples": measurements.len(),
    })
}

fn metric_samples(measurements: &[Measurement], metric: &str) -> Vec<f64> {
    measurements
        .iter()
        .map(|m| match metric {
            "fps_avg" => m.fps_avg,
            "fps_p1" => m.fps_p1,
            "frame_time_p99_ms" => m.frame_time_p99_ms,
            "psi_cpu_avg" => m.psi_cpu_avg,
            "psi_memory_avg" => m.psi_memory_avg,
            _ => 0.0,
        })
        .collect()
}

pub fn build_task_envelope(
    agent: AgentName,
    context: serde_json::Value,
    model: &str,
    max_tokens: u32,
    timeout_seconds: u64,
    max_retries: u32,
) -> TaskEnvelope {
    TaskEnvelope {
        task_id: Uuid::new_v4(),
        agent,
        context,
        config: AgentConfig {
            model: model.to_string(),
            max_tokens,
            timeout_seconds,
            max_retries,
        },
    }
}

/// Unwrap a Claude-backed agent's `{"response": "<json>"}` envelope.
/// Falls back to the raw value if `response` is absent or the inner string
/// is not parseable JSON. Strips ``` fences if present.
/// Guest-side path where the launch_benchmark handler deposits the MangoHud
/// frame-time CSV. The profiler fetches it back over vsock after the run.
pub const GUEST_MANGOHUD_OUTPUT: &str = "/tmp/crucible_mangohud.csv";
/// Guest path for the Perfetto kernel trace captured during the
/// comparison-phase run (fetched to the artifact dir for the analyzer).
pub const GUEST_PERFETTO_OUTPUT: &str = "/tmp/crucible_trace.perfetto-trace";

/// Build the profiler `TaskEnvelope.context` for a measurement phase.
/// Shared by the baseline and comparison dispatch sites so the synthetic
/// and game threading can't drift between them.
pub fn measurement_context(
    config: &CrucibleConfig,
    phase: &str,
    game_name: &str,
    capture_perfetto: bool,
) -> serde_json::Value {
    let mut context = serde_json::json!({
        "action": "measure",
        "phase": phase,
        "game": game_name,
        "runs": config.measurement.runs_per_phase,
        "workload_kind": config.measurement.mode,
        "vsock_cid": config.vm.vsock_cid,
    });
    if config.measurement.mode == "game" {
        // benchmark_args are stress-ng knobs — leaking them here would put
        // `--cpu 2` on the vkmark command line. duration_secs is shared:
        // the profiler sizes both the benchmark scene duration and
        // MangoHud's log window from it.
        context["game_benchmark"] = serde_json::json!(config.measurement.game_benchmark);
        context["mangohud_output"] = serde_json::json!(GUEST_MANGOHUD_OUTPUT);
        context["duration_secs"] =
            serde_json::json!(config.measurement.benchmark_duration_secs);
        context["coload_cpu"] = serde_json::json!(config.measurement.coload_cpu);
        if capture_perfetto {
            // The profiled repeat runs the workload once more under a
            // Perfetto kernel-scheduler trace; the orchestrator only sets
            // this for a dedicated capture run in the baseline phase, never
            // for the persisted measurement samples (tracing overhead would
            // skew them). Analyze runs before ComparisonMeasurement, so the
            // trace must exist by the end of the baseline phase.
            context["capture_perfetto"] = serde_json::json!(true);
            context["perfetto_output"] = serde_json::json!(GUEST_PERFETTO_OUTPUT);
            context["perfetto_host_dir"] =
                serde_json::json!(config.orchestrator.artifact_dir);
        }
    } else if config.measurement.mode == "steam" {
        context["steam_app_id"] = serde_json::json!(config.measurement.steam_app_id);
        context["steam_launch_args"] =
            serde_json::json!(config.measurement.steam_launch_args);
        context["mangohud_output"] = serde_json::json!(GUEST_MANGOHUD_OUTPUT);
        context["duration_secs"] =
            serde_json::json!(config.measurement.benchmark_duration_secs);
        if capture_perfetto {
            // Same contract as game mode: only the dedicated profiled
            // repeat in the baseline phase carries this. Without it the
            // analyzer gets no kernel trace in steam mode and reasons
            // blind from the summary metrics.
            context["capture_perfetto"] = serde_json::json!(true);
            context["perfetto_output"] = serde_json::json!(GUEST_PERFETTO_OUTPUT);
            context["perfetto_host_dir"] =
                serde_json::json!(config.orchestrator.artifact_dir);
        }
    } else {
        context["benchmark_args"] = serde_json::json!(config.measurement.benchmark_args);
        context["duration_secs"] =
            serde_json::json!(config.measurement.benchmark_duration_secs);
        // Synthetic mode also profiles: a kernel trace during the stress-ng
        // run surfaces sched/memory-reclaim stalls the analyzer can act on.
        if capture_perfetto {
            context["capture_perfetto"] = serde_json::json!(true);
            context["perfetto_output"] = serde_json::json!(GUEST_PERFETTO_OUTPUT);
            context["perfetto_host_dir"] =
                serde_json::json!(config.orchestrator.artifact_dir);
        }
    }
    context
}

pub fn parse_agent_response(value: &serde_json::Value) -> serde_json::Value {
    let Some(text) = value.get("response").and_then(|v| v.as_str()) else {
        return value.clone();
    };
    let trimmed = text.trim();

    // 1. A code fence anywhere in the text (Claude routinely wraps the
    //    JSON in explanatory prose on both sides). Prefer the ```json
    //    opener; only fall back to a bare ``` fence when no ```json fence
    //    exists at all — otherwise the bare match re-finds the same fence
    //    with "json" glued to the payload.
    let opener = if trimmed.contains("```json") {
        "```json"
    } else {
        "```"
    };
    if let Some(start) = trimmed.find(opener) {
        let rest = &trimmed[start + opener.len()..];
        let inner = match rest.find("```") {
            Some(end) => &rest[..end],
            None => rest,
        };
        if let Ok(parsed) = serde_json::from_str(inner.trim()) {
            return parsed;
        }
    }

    // 2. The whole text as bare JSON.
    if let Ok(parsed) = serde_json::from_str(trimmed) {
        return parsed;
    }

    // 3. First '{' to last '}' — one JSON object embedded in prose.
    if let (Some(start), Some(end)) = (trimmed.find('{'), trimmed.rfind('}')) {
        if start < end {
            if let Ok(parsed) = serde_json::from_str(&trimmed[start..=end]) {
                return parsed;
            }
        }
    }

    value.clone()
}

/// Pull a `patch_path` out of an Optimizer envelope. Tries the top level first
/// (the Optimizer's `extract_result` lifts JSON fields up), then falls back to
/// the `{"response": "<json>"}` form via `parse_agent_response` so older
/// envelope shapes still light up. Returns `None` when the value is missing,
/// not a string, or an empty string.
fn extract_patch_path(value: &serde_json::Value) -> Option<String> {
    let direct = value.get("patch_path").and_then(|v| v.as_str());
    if let Some(s) = direct {
        if !s.is_empty() {
            return Some(s.to_string());
        }
    }
    let parsed = parse_agent_response(value);
    parsed
        .get("patch_path")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
}

/// Pull the optimizer's proposed sysctl tunings out of its envelope:
/// `sysctl_changes: [{"key": "kernel.x", "value": "123", ...}, ...]` →
/// a key→value map. Same top-level-then-parsed fallback as
/// `extract_patch_path`. Empty map when absent/malformed.
fn extract_sysctl_changes(value: &serde_json::Value) -> serde_json::Map<String, serde_json::Value> {
    let mut out = serde_json::Map::new();
    let direct = value.get("sysctl_changes");
    let parsed;
    let list = match direct.and_then(|v| v.as_array()) {
        Some(l) => Some(l),
        None => {
            parsed = parse_agent_response(value);
            // parsed is a temporary; clone entries out below.
            match parsed.get("sysctl_changes").and_then(|v| v.as_array()) {
                Some(l) => {
                    for item in l {
                        if let (Some(k), Some(v)) = (
                            item.get("key").and_then(|k| k.as_str()),
                            item.get("value"),
                        ) {
                            out.insert(k.to_string(), v.clone());
                        }
                    }
                    return out;
                }
                None => None,
            }
        }
    };
    if let Some(l) = list {
        for item in l {
            if let (Some(k), Some(v)) = (
                item.get("key").and_then(|k| k.as_str()),
                item.get("value"),
            ) {
                out.insert(k.to_string(), v.clone());
            }
        }
    }
    out
}

/// Extract a `f64` from a JSON object, returning 0.0 when missing or non-numeric.
fn json_f64(value: &serde_json::Value, key: &str) -> f64 {
    value
        .get(key)
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0)
}

/// Returns true iff we should loop back from Evaluate through Iterate to
/// Analyze and re-run the optimizer. A `Marginal` verdict means the patch
/// neither cleanly won nor regressed — worth another attempt with the
/// previous result in the Analyzer's context. We stop once we've hit
/// `max_attempts` so a stuck-in-marginal bottleneck doesn't loop forever.
pub fn should_iterate(verdict: Verdict, attempt_number: u32, max_attempts: u32) -> bool {
    matches!(verdict, Verdict::Marginal) && attempt_number < max_attempts
}

/// Determine overall verdict from metric evaluations.
/// Any regression blocks. All accept (or neutral) = Accept. Mixed = Marginal.
pub fn determine_overall_verdict(evals: &[MetricEvaluation]) -> Verdict {
    if evals.is_empty() {
        return Verdict::Neutral;
    }
    if evals.iter().any(|e| e.verdict == Verdict::Regressed) {
        return Verdict::Regressed;
    }
    if evals.iter().all(|e| e.verdict == Verdict::Neutral) {
        return Verdict::Neutral;
    }
    let all_accept = evals
        .iter()
        .all(|e| e.verdict == Verdict::Accept || e.verdict == Verdict::Neutral);
    if all_accept {
        return Verdict::Accept;
    }
    Verdict::Marginal
}

pub struct Orchestrator {
    config: CrucibleConfig,
    db: Database,
    agent_runner: AgentRunner,
    kernel_builder: KernelBuilder,
    vm_manager: VmManager,
    vsock_client: VsockClient,
    state_machine: StateMachine,
    /// Path to the most recently built kernel image. `None` until the first
    /// successful `KernelBuilder::build_kernel`.
    current_kernel: Option<PathBuf>,
}

impl Orchestrator {
    pub fn new(config: CrucibleConfig, db: Database, agent_runner: AgentRunner) -> Self {
        let kernel_builder = KernelBuilder::new(&config.vm.kernel_src);
        let vm_manager = VmManager::new(config.vm.clone());
        let vsock_client = VsockClient::new(
            config.vm.vsock_cid,
            Duration::from_secs(config.vm.boot_timeout_secs),
        );
        Self {
            config,
            db,
            agent_runner,
            kernel_builder,
            vm_manager,
            vsock_client,
            state_machine: StateMachine::new(),
            current_kernel: None,
        }
    }

    pub fn state(&self) -> CycleState {
        self.state_machine.state()
    }

    pub fn eval_config(&self) -> EvalConfig {
        EvalConfig {
            significance_threshold: self.config.measurement.significance_threshold,
            effect_size_threshold: self.config.measurement.effect_size_threshold,
        }
    }

    /// Build a kernel image (if not already cached for this cycle) and boot
    /// the VM. No-op if the VM is already running with a usable kernel.
    pub async fn provision_vm(&mut self) -> Result<()> {
        let kernel_path = match &self.current_kernel {
            Some(p) => p.clone(),
            None => {
                let p = self.kernel_builder.build_kernel().await?;
                self.current_kernel = Some(p.clone());
                p
            }
        };

        if self.vm_manager.state() == VmState::Running {
            tracing::info!("VM already running, skipping boot");
            return Ok(());
        }

        // A GPU not bound to vfio-pci hangs the QEMU boot with no useful
        // diagnostic — fail fast before spawning. No-op without passthrough.
        self.vm_manager.validate_passthrough()?;

        let kernel_str = kernel_path.to_string_lossy().to_string();
        self.vm_manager.boot(&kernel_str).await?;
        let timeout = Duration::from_secs(self.config.vm.boot_timeout_secs);
        self.vm_manager
            .wait_for_ready(&self.vsock_client, timeout)
            .await?;
        Ok(())
    }

    /// Apply a patch to the kernel source, rebuild, then reboot the VM with
    /// the new image. Caller is responsible for persisting the patch row in
    /// the `patches` table separately.
    pub async fn apply_changes(&mut self, patch_path: &str) -> Result<()> {
        let new_kernel = self.kernel_builder.apply_and_build(patch_path).await?;
        self.vm_manager.shutdown().await?;
        self.current_kernel = Some(new_kernel.clone());

        let kernel_str = new_kernel.to_string_lossy().to_string();
        self.vm_manager.boot(&kernel_str).await?;
        let timeout = Duration::from_secs(self.config.vm.boot_timeout_secs);
        self.vm_manager
            .wait_for_ready(&self.vsock_client, timeout)
            .await?;
        Ok(())
    }

    async fn run_agent(
        &self,
        agent: AgentName,
        context: serde_json::Value,
    ) -> Result<serde_json::Value> {
        let max_tokens = self
            .config
            .agents
            .per_agent_max_tokens
            .get(agent.as_str())
            .copied()
            .unwrap_or(self.config.agents.max_tokens);
        let task = build_task_envelope(
            agent,
            context,
            &self.config.agents.model,
            max_tokens,
            self.config.agents.timeout_secs,
            self.config.agents.max_retries,
        );
        let result = self.agent_runner.run_agent(task).await?;
        tracing::info!(
            agent = agent.as_str(),
            input_tokens = result.usage.input_tokens,
            output_tokens = result.usage.output_tokens,
            api_calls = result.usage.api_calls,
            "agent usage",
        );
        match result.status {
            crucible_common::protocol::TaskStatus::Success => Ok(result.result),
            crucible_common::protocol::TaskStatus::Failure => {
                let err_msg = result.result["error"]
                    .as_str()
                    .unwrap_or("unknown error");
                anyhow::bail!("agent {:?} failed: {}", agent, err_msg);
            }
            crucible_common::protocol::TaskStatus::NeedsInput => {
                anyhow::bail!("agent {:?} needs input (not supported yet)", agent);
            }
        }
    }

    /// Read both phases from the DB, run a t-test per metric, persist the
    /// per-metric `evaluations` rows, and return the overall verdict.
    /// Metrics with fewer than two samples per side fall back to a delta-only
    /// `Neutral` verdict so the cycle still completes on synthetic single-run data.
    pub fn run_evaluation(&self, cycle_id: i64) -> Result<Verdict> {
        let baseline = self.db.get_measurements(cycle_id, "baseline")?;
        let comparison = self.db.get_measurements(cycle_id, "comparison")?;
        let cfg = self.eval_config();
        let mut evals: Vec<MetricEvaluation> = Vec::with_capacity(METRIC_DEFS.len());
        for (metric, lower_is_better) in METRIC_DEFS {
            let b = metric_samples(&baseline, metric);
            let c = metric_samples(&comparison, metric);
            let scored = evaluate_metric(metric, &b, &c, *lower_is_better, &cfg);
            self.db.insert_evaluation(
                cycle_id,
                &scored.metric,
                scored.baseline_mean,
                scored.comparison_mean,
                scored.delta_pct,
                &scored.verdict.to_string(),
            )?;
            tracing::info!(
                cycle_id,
                metric = %scored.metric,
                baseline = scored.baseline_mean,
                comparison = scored.comparison_mean,
                delta_pct = scored.delta_pct,
                verdict = %scored.verdict,
                "metric scored"
            );
            evals.push(scored);
        }
        let overall = determine_overall_verdict(&evals);
        tracing::info!(cycle_id, verdict = %overall, "overall verdict");
        Ok(overall)
    }

    fn persist_measurement(
        &self,
        cycle_id: i64,
        phase: &str,
        agent_result: &serde_json::Value,
    ) -> Result<()> {
        let parsed = parse_agent_response(agent_result);
        // Every profiler path (synthetic and game) emits an explicit
        // fps_avg key — even the synthetic one writes fps_avg = 0.0. A
        // missing key means the response wasn't parseable (or the agent
        // reported an error); defaulting it to 0.0 once turned a 14k-fps
        // live-GPU run into a "successful" all-zeros measurement.
        if parsed.get("fps_avg").and_then(|v| v.as_f64()).is_none() {
            anyhow::bail!(
                "profiler {phase} response has no numeric fps_avg; raw response: {}",
                serde_json::to_string(agent_result)
                    .unwrap_or_default()
                    .chars()
                    .take(2000)
                    .collect::<String>()
            );
        }
        let fps_avg = json_f64(&parsed, "fps_avg");
        let fps_p1 = json_f64(&parsed, "fps_p1");
        let frame_time_p99_ms = json_f64(&parsed, "frame_time_p99_ms");
        let psi_cpu_avg = json_f64(&parsed, "psi_cpu_avg");
        let psi_memory_avg = json_f64(&parsed, "psi_memory_avg");

        // A valid run always yields a positive frame time (synthetic derives
        // it from ops/sec, game from the MangoHud percentile). A zero means
        // the benchmark produced no metrics that run (e.g. an intermittent
        // stress-ng miss returning ops/sec = 0). Recording it as a real
        // sample poisons the phase mean and variance — one 0.0 in a set of
        // 0.0039 fabricated a spurious -10% delta with 33% CV. Reject it as a
        // failed sample so measure_phase collects only clean data points.
        // NaN must also land here, so keep the comparison NaN-rejecting.
        if !frame_time_p99_ms.is_finite() || frame_time_p99_ms <= 0.0 {
            anyhow::bail!(
                "profiler {phase} sample has non-positive frame_time_p99_ms \
                 ({frame_time_p99_ms}); treating as a failed run"
            );
        }

        // Provenance for post-hoc triage: which frame-stat methodology
        // produced this sample (steam mode: "firstparty" per-frame log vs
        // "mangohud" 100ms samples — the two compute fps_p1/p99 differently
        // and must never mix within a phase).
        let metrics_source = parsed
            .get("metrics_source")
            .and_then(|v| v.as_str())
            .unwrap_or("unspecified");
        let custom = serde_json::json!({ "metrics_source": metrics_source }).to_string();

        let id = self.db.insert_measurement(
            cycle_id,
            phase,
            fps_avg,
            fps_p1,
            frame_time_p99_ms,
            psi_cpu_avg,
            psi_memory_avg,
            &custom,
        )?;
        tracing::info!(
            cycle_id,
            phase,
            measurement_id = id,
            fps_avg,
            metrics_source,
            "measurement persisted"
        );
        Ok(())
    }

    /// Run the profiler `runs_per_phase` times, persisting one measurement
    /// row per run so the evaluator's Welch t-test has real samples (a
    /// single aggregate row per phase makes every verdict Neutral). All
    /// runs are clean — no Perfetto — so tracing overhead never enters the
    /// measured distribution. Returns the number of samples persisted.
    async fn measure_phase(
        &mut self,
        cycle_id: i64,
        phase: &str,
        game_name: &str,
    ) -> Result<u32> {
        // Warmup runs are discarded: the first benchmark after boot (or a
        // kernel swap + reboot) hits a cold GPU whose clocks haven't ramped,
        // otherwise landing as a low-fps outlier in the sample set.
        for run in 0..self.config.measurement.warmup_runs {
            let ctx = measurement_context(&self.config, phase, game_name, false);
            if let Err(e) = self.run_agent(AgentName::Profiler, ctx).await {
                tracing::warn!(phase, run, error = %e, "warmup run failed; continuing");
            }
        }
        let runs = self.config.measurement.runs_per_phase.max(1);
        let mut persisted = 0u32;
        for run in 0..runs {
            let ctx = measurement_context(&self.config, phase, game_name, false);
            let result = self.run_agent(AgentName::Profiler, ctx).await?;
            match self.persist_measurement(cycle_id, phase, &result) {
                Ok(()) => persisted += 1,
                Err(e) => tracing::warn!(
                    phase, run, error = %e, "measurement sample failed; continuing"
                ),
            }
        }
        if persisted == 0 {
            anyhow::bail!("all {runs} {phase} measurement samples failed");
        }
        tracing::info!(phase, persisted, runs, "phase samples collected");
        Ok(persisted)
    }

    /// One extra profiled run (Perfetto kernel trace) after the baseline
    /// samples. Its frame numbers are discarded — only the trace matters.
    /// Returns the host trace paths for the analyzer (empty on failure;
    /// profiling is non-fatal).
    async fn capture_profile(
        &mut self,
        game_name: &str,
    ) -> Result<Vec<serde_json::Value>> {
        let ctx = measurement_context(&self.config, "baseline", game_name, true);
        let result = self.run_agent(AgentName::Profiler, ctx).await?;
        let parsed = parse_agent_response(&result);
        let traces = parsed
            .get("collection_paths")
            .and_then(|c| c.get("traces"))
            .and_then(|t| t.as_array())
            .cloned()
            .unwrap_or_default();
        Ok(traces)
    }

    pub async fn run_cycle(&mut self) -> Result<()> {
        // SelectGame
        self.state_machine
            .transition(CycleState::SelectGame)
            .map_err(|e| anyhow::anyhow!(e))?;
        tracing::info!(state = %self.state_machine.state(), "cycle state transition");

        let mut game_context = serde_json::json!({
            "action": "select_game",
            // Lets the selector pivot to native OSS benchmarks (vkmark/
            // glmark2) when game mode runs on a rootfs with no Steam
            // library, or to the configured Steam title in steam mode.
            "workload_kind": self.config.measurement.mode,
        });
        if self.config.measurement.mode == "steam" {
            game_context["steam_app_id"] =
                serde_json::json!(self.config.measurement.steam_app_id);
        }
        let game_result = self.run_agent(AgentName::GameSelector, game_context).await?;
        let game_info = parse_agent_response(&game_result);

        let game_name = game_info["name"]
            .as_str()
            .or_else(|| game_info["game_name"].as_str())
            .or_else(|| game_result["response"].as_str())
            .unwrap_or("unknown_game");
        let game_app_id = game_info["app_id"].as_i64().unwrap_or(0);

        let cycle_id = self.db.create_cycle(game_name, game_app_id)?;
        self.db
            .update_cycle_status(cycle_id, CycleState::SelectGame.as_str())?;
        tracing::info!(cycle_id, game = game_name, "cycle created");

        // ProvisionVm
        self.state_machine
            .transition(CycleState::ProvisionVm)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::ProvisionVm.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "provisioning VM");
        self.provision_vm().await?;

        // BaselineMeasurement
        self.state_machine
            .transition(CycleState::BaselineMeasurement)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::BaselineMeasurement.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "running baseline measurement");

        // Collect runs_per_phase clean samples, then one profiled run for
        // the analyzer's kernel trace.
        self.measure_phase(cycle_id, "baseline", game_name).await?;
        let baseline_traces = match self.capture_profile(game_name).await {
            Ok(t) => t,
            Err(e) => {
                tracing::warn!(error = %e, "profile capture failed; analyzing without a trace");
                Vec::new()
            }
        };
        // Metrics for the analyzer come from the persisted baseline rows,
        // not a single run.
        let baseline_rows = self.db.get_measurements(cycle_id, "baseline")?;
        let baseline_metrics = mean_metrics(&baseline_rows);

        // Iteration loop: Analyze → GenerateOptimization → ApplyChanges →
        // ComparisonMeasurement → Evaluate. A Marginal verdict re-enters via
        // Iterate → Analyze with the prior attempts threaded through context
        // so the Analyzer can pivot. Bounded by the configured per-bottleneck
        // attempt cap so a stuck-in-marginal cycle terminates.
        let max_attempts = self
            .config
            .agents
            .optimizer
            .max_attempts_per_bottleneck
            .max(1);
        let mut iteration: u32 = 0;
        let mut previous_attempts: Vec<serde_json::Value> = Vec::new();
        // Cross-cycle memory: patches from earlier cycles in this run. Each
        // cycle reverts its patch and starts from the base kernel, so without
        // this the analyzer keeps re-deriving the same bottleneck and the
        // optimizer produces near-duplicate patches. Seed both agents with the
        // already-explored diff names so a fresh cycle targets a different
        // subsystem/function.
        let explored_areas: Vec<serde_json::Value> = self
            .db
            .list_patch_outcomes()
            .unwrap_or_default()
            .into_iter()
            .map(|(p, outcome)| {
                let stem = std::path::Path::new(&p)
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .unwrap_or(&p)
                    .to_string();
                // Outcome annotation lets the agents treat an accepted
                // family as fertile ground (different mechanism, same
                // subsystem) instead of just a name to avoid.
                serde_json::Value::String(format!("{stem} [{outcome}]"))
            })
            .collect();
        let (overall, applied_patch): (Verdict, Option<String>) = loop {
            let attempt_number = iteration + 1;

            // Re-entry: Evaluate → Iterate → Analyze. First pass arrives from
            // BaselineMeasurement, which already permits the Analyze
            // transition directly.
            if iteration > 0 {
                self.state_machine
                    .transition(CycleState::Iterate)
                    .map_err(|e| anyhow::anyhow!(e))?;
                self.db
                    .update_cycle_status(cycle_id, CycleState::Iterate.as_str())?;
                tracing::info!(
                    state = %self.state_machine.state(),
                    iteration,
                    "iterating on marginal verdict"
                );
            }

            // Analyze
            self.state_machine
                .transition(CycleState::Analyze)
                .map_err(|e| anyhow::anyhow!(e))?;
            self.db
                .update_cycle_status(cycle_id, CycleState::Analyze.as_str())?;
            tracing::info!(
                state = %self.state_machine.state(),
                attempt = attempt_number,
                "analyzing performance data"
            );

            let mut analyze_context = serde_json::json!({
                "action": "analyze",
                "game_name": game_name,
                "cycle_id": cycle_id,
                "metrics": baseline_metrics,
                "attempt_number": attempt_number,
                // Tell the analyzer what the workload actually is, so it does
                // not mistake a synthetic stress-ng trace (mm/RCU/kcompactd
                // threads, no game) for a failed game capture.
                "workload_kind": self.config.measurement.mode,
                "workload_args": self.config.measurement.benchmark_args,
            });
            // The profiled baseline run leaves a Perfetto kernel trace on
            // the host; hand it to the analyzer so bottleneck hunting is
            // grounded in scheduler data, not just the summary metrics.
            if !baseline_traces.is_empty() {
                analyze_context["trace_paths"] =
                    serde_json::Value::Array(baseline_traces.clone());
            }
            if !previous_attempts.is_empty() {
                analyze_context["previous_attempts"] =
                    serde_json::Value::Array(previous_attempts.clone());
            }
            if !explored_areas.is_empty() {
                analyze_context["explored_areas"] =
                    serde_json::Value::Array(explored_areas.clone());
            }
            let analysis_result = self
                .run_agent(AgentName::Analyzer, analyze_context)
                .await?;
            let analysis = parse_agent_response(&analysis_result);

            // GenerateOptimization
            self.state_machine
                .transition(CycleState::GenerateOptimization)
                .map_err(|e| anyhow::anyhow!(e))?;
            self.db
                .update_cycle_status(cycle_id, CycleState::GenerateOptimization.as_str())?;
            tracing::info!(
                state = %self.state_machine.state(),
                attempt = attempt_number,
                "generating optimization"
            );

            // Defensive cleanup: if a prior cycle crashed between edit_file
            // and finalize_patch — or the prior iteration's marginal patch
            // is still on disk — the kernel tree may be dirty. The
            // optimizer's finalize_patch tool relies on diffing against a
            // clean base, so reset the working tree before invoking it.
            if let Err(e) = self.kernel_builder.revert_patch().await {
                tracing::warn!(error = %e, "pre-optimizer revert_patch failed; continuing");
            }

            // Hand the analyzer's bottleneck + optimization_targets to the
            // Optimizer so it doesn't fish through the kernel tree blindly.
            // Without this the LLM burns its whole timeout reading files at
            // random looking for something to change.
            // allowed_layers restricts what the optimizer may change. A
            // "tuning"-only set forces live sysctl changes (no kernel patch,
            // so no rebuild + reboot — the comparison runs on the same VM,
            // avoiding the fragile GPU-passthrough reboot path).
            let allowed = &self.config.agents.optimizer.allowed_layers;
            let tuning_only = allowed.len() == 1 && allowed[0] == "tuning";
            let mut optimize_context = serde_json::json!({
                "action": "optimize",
                "game_name": game_name,
                "cycle_id": cycle_id,
                "bottleneck": analysis,
                "kernel_src": self.config.vm.kernel_src,
                "attempt_number": attempt_number,
                "allowed_layers": allowed,
                "tuning_only": tuning_only,
                // The concrete workload so the optimizer can pick knobs the
                // benchmark actually responds to (e.g. a stress-ng --vm
                // memory workload → transparent hugepages), rather than
                // guessing from the bottleneck summary alone.
                "workload_mode": self.config.measurement.mode,
                "workload_args": self.config.measurement.benchmark_args,
            });
            if !previous_attempts.is_empty() {
                optimize_context["previous_attempts"] =
                    serde_json::Value::Array(previous_attempts.clone());
            }
            if !explored_areas.is_empty() {
                optimize_context["explored_areas"] =
                    serde_json::Value::Array(explored_areas.clone());
            }
            let optimization = self
                .run_agent(AgentName::Optimizer, optimize_context)
                .await?;

            // ApplyChanges
            self.state_machine
                .transition(CycleState::ApplyChanges)
                .map_err(|e| anyhow::anyhow!(e))?;
            self.db
                .update_cycle_status(cycle_id, CycleState::ApplyChanges.as_str())?;
            tracing::info!(
                state = %self.state_machine.state(),
                attempt = attempt_number,
                "applying changes"
            );

            let mut applied_patch: Option<String> = extract_patch_path(&optimization);
            if let Some(patch_path) = applied_patch.as_deref() {
                let parsed_opt = parse_agent_response(&optimization);
                let layer = optimization["layer"]
                    .as_str()
                    .or_else(|| parsed_opt["layer"].as_str())
                    .unwrap_or("kernel");
                self.db.insert_patch(cycle_id, layer, patch_path)?;
                tracing::info!(
                    patch = patch_path,
                    layer,
                    attempt = attempt_number,
                    "patch recorded"
                );
                // Soft-fail on apply: a corrupt or non-applicable patch must
                // not crash the cycle. Comparison runs against the unchanged
                // kernel, evaluator will report Neutral, and the cycle
                // terminates cleanly.
                if let Err(e) = self.apply_changes(patch_path).await {
                    tracing::warn!(
                        patch = patch_path,
                        error = %e,
                        "apply_changes failed; continuing with baseline kernel",
                    );
                    applied_patch = None;
                }
            } else {
                tracing::warn!(
                    attempt = attempt_number,
                    "no patch_path in optimization output, skipping apply"
                );
            }

            // Optimizer tunings: apply proposed sysctls in the (possibly
            // just-rebooted) guest so the comparison actually measures them.
            // Best-effort — a missing knob (e.g. its patch didn't build)
            // must not kill the cycle; the guest reports per-key results.
            let sysctls = extract_sysctl_changes(&optimization);
            if !sysctls.is_empty() {
                let summary = sysctls
                    .iter()
                    .map(|(k, v)| format!("{k}={}", v.as_str().unwrap_or(&v.to_string())))
                    .collect::<Vec<_>>()
                    .join(",");
                match self.vsock_client.apply_sysctls(sysctls).await {
                    Ok(resp) => {
                        tracing::info!(sysctls = %summary, response = ?resp, "sysctl tunings applied");
                        self.db.insert_patch(cycle_id, "tuning", &summary)?;
                    }
                    Err(e) => {
                        tracing::warn!(sysctls = %summary, error = %e, "sysctl apply failed; comparison runs untuned");
                    }
                }
            }

            // ComparisonMeasurement
            self.state_machine
                .transition(CycleState::ComparisonMeasurement)
                .map_err(|e| anyhow::anyhow!(e))?;
            self.db
                .update_cycle_status(cycle_id, CycleState::ComparisonMeasurement.as_str())?;
            tracing::info!(
                state = %self.state_machine.state(),
                attempt = attempt_number,
                "running comparison measurement"
            );

            self.measure_phase(cycle_id, "comparison", game_name).await?;

            // Evaluate
            self.state_machine
                .transition(CycleState::Evaluate)
                .map_err(|e| anyhow::anyhow!(e))?;
            self.db
                .update_cycle_status(cycle_id, CycleState::Evaluate.as_str())?;
            tracing::info!(
                state = %self.state_machine.state(),
                attempt = attempt_number,
                "evaluating results"
            );

            let attempt_verdict = self.run_evaluation(cycle_id)?;
            tracing::info!(
                attempt = attempt_number,
                verdict = %attempt_verdict,
                "attempt verdict"
            );

            if should_iterate(attempt_verdict, attempt_number, max_attempts) {
                // Stash this attempt for the next analyzer pass. Patch path
                // is preserved even though the next pre-optimizer revert
                // will erase the on-disk patch — the Analyzer wants to know
                // what was tried, not what's currently applied.
                previous_attempts.push(serde_json::json!({
                    "attempt_number": attempt_number,
                    "patch_path": applied_patch.clone(),
                    "verdict": "marginal",
                }));
                iteration += 1;
                continue;
            }
            break (attempt_verdict, applied_patch);
        };
        let next = match overall {
            Verdict::Accept | Verdict::Marginal | Verdict::Neutral => CycleState::Accept,
            Verdict::Regressed => CycleState::Reject,
        };
        if let Some(p) = applied_patch.as_deref() {
            // Corpus independence: every cycle's patch is measured against
            // the BASE kernel. Reverting only on Regressed left the source
            // patched on Accept/Neutral, and — worse — the VM kept running
            // the patched image either way, so the next cycle's baseline
            // silently measured patch N instead of base (each verdict then
            // compares patch N+1 against patch N, not against base). The
            // diff itself persists in .crucible_patches/.
            match self.kernel_builder.revert_patch().await {
                Ok(()) => tracing::info!(patch = p, verdict = %overall, "patch reverted at cycle end"),
                Err(e) => {
                    tracing::warn!(patch = p, error = %e, "revert_patch failed at cycle end")
                }
            }
            if let Err(e) = self.vm_manager.shutdown().await {
                tracing::warn!(error = %e, "VM shutdown failed at cycle end");
            }
            // Force a base-kernel rebuild + fresh boot on the next cycle.
            self.current_kernel = None;
        }
        self.state_machine
            .transition(next)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db.update_cycle_status(cycle_id, next.as_str())?;
        tracing::info!(state = %self.state_machine.state(), cycle_id, %overall, "cycle decision");

        // Back to Idle
        self.state_machine
            .transition(CycleState::Idle)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db.update_cycle_status(cycle_id, CycleState::Idle.as_str())?;

        Ok(())
    }

    pub async fn run_loop(&mut self, max_cycles: u64) -> Result<()> {
        let mut cycles_completed: u64 = 0;
        let unlimited = max_cycles == 0;

        loop {
            if !unlimited && cycles_completed >= max_cycles {
                tracing::info!(cycles = cycles_completed, "max cycles reached, stopping");
                break;
            }

            tracing::info!(
                cycle = cycles_completed + 1,
                max = if unlimited { 0 } else { max_cycles },
                "starting optimization cycle"
            );

            match self.run_cycle().await {
                Ok(()) => {
                    tracing::info!(cycles = cycles_completed + 1, "cycle completed successfully");
                }
                Err(e) => {
                    tracing::error!(error = %e, "cycle failed, resetting state machine");
                    self.state_machine = StateMachine::new();
                    // A cycle can die after apply_and_build succeeded: the
                    // source is patched and the VM (if alive) runs the
                    // patched image. Scrub both so the next cycle's
                    // baseline is really the base kernel.
                    if let Err(re) = self.kernel_builder.revert_patch().await {
                        tracing::debug!(error = %re, "post-failure revert_patch (often nothing to revert)");
                    }
                    if let Err(se) = self.vm_manager.shutdown().await {
                        tracing::warn!(error = %se, "post-failure VM shutdown failed");
                    }
                    self.current_kernel = None;
                    let backoff = self.config.orchestrator.failure_cooldown_secs;
                    if backoff > 0 {
                        tracing::warn!(
                            backoff_secs = backoff,
                            "backing off after failed cycle (transient API/VM outages \
                             would otherwise burn the cycle budget instantly)"
                        );
                        tokio::time::sleep(std::time::Duration::from_secs(backoff)).await;
                    }
                }
            }
            cycles_completed += 1;

            if !unlimited && cycles_completed >= max_cycles {
                break;
            }

            let cooldown = self.config.orchestrator.cycle_cooldown_secs;
            if cooldown > 0 {
                tracing::info!(cooldown_secs = cooldown, "cooling down between cycles");
                tokio::time::sleep(std::time::Duration::from_secs(cooldown)).await;
            }
        }

        // The vng wrapper dies with the process (kill_on_drop), but the
        // QEMU grandchild survives it and keeps the GPU's /dev/vfio group
        // open — observed leaking after a clean --single-cycle exit.
        // shutdown() kills and drains the whole process group.
        if let Err(e) = self.vm_manager.shutdown().await {
            tracing::warn!(error = %e, "final VM shutdown failed");
        }
        tracing::info!(total_cycles = cycles_completed, "orchestrator loop finished");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evaluator::TTestResult;

    #[test]
    fn should_iterate_loops_on_marginal_under_cap() {
        assert!(should_iterate(Verdict::Marginal, 1, 3));
        assert!(should_iterate(Verdict::Marginal, 2, 3));
    }

    #[test]
    fn should_iterate_stops_when_cap_reached() {
        // attempt_number is 1-indexed, so attempt 3 with cap 3 means we
        // already used all three attempts — no more iterations.
        assert!(!should_iterate(Verdict::Marginal, 3, 3));
        assert!(!should_iterate(Verdict::Marginal, 4, 3));
    }

    #[test]
    fn should_iterate_skips_non_marginal_verdicts() {
        assert!(!should_iterate(Verdict::Accept, 1, 5));
        assert!(!should_iterate(Verdict::Neutral, 1, 5));
        assert!(!should_iterate(Verdict::Regressed, 1, 5));
    }

    #[test]
    fn should_iterate_disabled_when_cap_is_one() {
        // With max_attempts = 1 the first marginal verdict is already
        // terminal; no further attempts allowed.
        assert!(!should_iterate(Verdict::Marginal, 1, 1));
    }

    #[test]
    fn parse_agent_response_unwraps_json_string() {
        let raw = serde_json::json!({
            "response": "{\"name\": \"cyberpunk\", \"app_id\": 1091500}"
        });
        let parsed = parse_agent_response(&raw);
        assert_eq!(parsed["name"], "cyberpunk");
        assert_eq!(parsed["app_id"], 1091500);
    }

    #[test]
    fn parse_agent_response_strips_markdown_fence() {
        let raw = serde_json::json!({
            "response": "```json\n{\"fps_avg\": 60.5}\n```"
        });
        let parsed = parse_agent_response(&raw);
        assert!((parsed["fps_avg"].as_f64().unwrap() - 60.5).abs() < f64::EPSILON);
    }

    #[test]
    fn parse_agent_response_finds_fenced_json_amid_prose() {
        // Real Claude profiler output: prose before AND after the fence.
        // The old prefix-only strip failed here, fell back to the raw
        // envelope, and json_f64 silently turned a 14k fps measurement
        // into 0.0 — a live-GPU e2e "passed" the cycle with zeros.
        let raw = serde_json::json!({
            "response": "Perfect! I've collected the measurements.\n\n\
                ```json\n{\"fps_avg\": 14463.5, \"fps_p1\": 10222.5}\n```\n\n\
                The benchmark ran for 30 seconds with excellent results."
        });
        let parsed = parse_agent_response(&raw);
        assert!((parsed["fps_avg"].as_f64().unwrap() - 14463.5).abs() < f64::EPSILON);
        assert!((parsed["fps_p1"].as_f64().unwrap() - 10222.5).abs() < f64::EPSILON);
    }

    #[test]
    fn parse_agent_response_falls_back_to_brace_extraction() {
        // No fence at all — prose with one embedded JSON object.
        let raw = serde_json::json!({
            "response": "Here are the results: {\"fps_avg\": 42.0} as requested."
        });
        let parsed = parse_agent_response(&raw);
        assert!((parsed["fps_avg"].as_f64().unwrap() - 42.0).abs() < f64::EPSILON);
    }

    #[test]
    fn parse_agent_response_rejects_ambiguous_multi_object_prose() {
        // Two JSON objects in prose: first-{ to last-} spans both, which
        // is not valid JSON, so extraction must NOT silently pick either
        // object. The raw envelope comes back and persist_measurement's
        // missing-fps_avg check turns it into a loud error.
        let raw = serde_json::json!({
            "response": "Old: {\"fps_avg\": 0.0} but new: {\"fps_avg\": 14463.5} done."
        });
        let parsed = parse_agent_response(&raw);
        assert!(
            parsed.get("fps_avg").is_none(),
            "ambiguous prose must not yield a metrics object: {parsed}"
        );
        assert!(parsed.get("response").is_some());
    }

    #[test]
    fn parse_agent_response_returns_input_when_no_response_key() {
        let raw = serde_json::json!({"echo": {"hello": "world"}});
        let parsed = parse_agent_response(&raw);
        assert_eq!(parsed["echo"]["hello"], "world");
    }

    #[test]
    fn parse_agent_response_returns_input_when_inner_not_json() {
        let raw = serde_json::json!({"response": "just plain text"});
        let parsed = parse_agent_response(&raw);
        assert_eq!(parsed["response"], "just plain text");
    }

    #[test]
    fn extract_patch_path_reads_top_level_field() {
        let raw = serde_json::json!({
            "layer": "kernel",
            "patch_path": "/tmp/p.diff",
            "response": "ignored",
        });
        assert_eq!(extract_patch_path(&raw), Some("/tmp/p.diff".to_string()));
    }

    #[test]
    fn extract_patch_path_falls_back_to_response_envelope() {
        let inner = serde_json::json!({
            "layer": "kernel",
            "patch_path": "/tmp/from-inner.diff",
        });
        let raw = serde_json::json!({"response": inner.to_string()});
        assert_eq!(
            extract_patch_path(&raw),
            Some("/tmp/from-inner.diff".to_string()),
        );
    }

    #[test]
    fn extract_patch_path_returns_none_on_empty_string() {
        let raw = serde_json::json!({"patch_path": ""});
        assert_eq!(extract_patch_path(&raw), None);
    }

    #[test]
    fn extract_patch_path_returns_none_when_missing() {
        let raw = serde_json::json!({"response": "no patch here"});
        assert_eq!(extract_patch_path(&raw), None);
    }

    #[test]
    fn persist_measurement_writes_row_from_wrapped_response() {
        use crate::agent_runner::AgentRunner;
        use crate::config::CrucibleConfig;
        use crate::db::Database;
        use std::path::PathBuf;
        use std::time::Duration;

        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/crucible-test.db"
            artifact_dir = "/tmp/crucible-artifacts"
            [vm]
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "00:00.0"
            [measurement]
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        let db = Database::open_in_memory().unwrap();
        let runner = AgentRunner::new(
            PathBuf::from("python3"),
            PathBuf::from("/tmp"),
            Duration::from_secs(1),
            std::env::temp_dir(),
        );
        let orch = Orchestrator::new(config, db, runner);

        let cycle_id = orch.db.create_cycle("test_game", 12345).unwrap();
        let agent_result = serde_json::json!({
            "response": "{\"fps_avg\": 60.0, \"fps_p1\": 45.0, \"frame_time_p99_ms\": 22.5, \"psi_cpu_avg\": 0.4, \"psi_memory_avg\": 1.1}"
        });
        orch.persist_measurement(cycle_id, "baseline", &agent_result)
            .unwrap();

        let rows = orch.db.get_measurements(cycle_id, "baseline").unwrap();
        assert_eq!(rows.len(), 1);
        assert!((rows[0].fps_avg - 60.0).abs() < f64::EPSILON);
        assert!((rows[0].fps_p1 - 45.0).abs() < f64::EPSILON);
        assert!((rows[0].frame_time_p99_ms - 22.5).abs() < f64::EPSILON);
        assert!((rows[0].psi_cpu_avg - 0.4).abs() < f64::EPSILON);
        assert!((rows[0].psi_memory_avg - 1.1).abs() < f64::EPSILON);
    }

    #[test]
    fn persist_measurement_defaults_missing_fields_to_zero() {
        use crate::agent_runner::AgentRunner;
        use crate::config::CrucibleConfig;
        use crate::db::Database;
        use std::path::PathBuf;
        use std::time::Duration;

        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/crucible-test2.db"
            artifact_dir = "/tmp/crucible-artifacts"
            [vm]
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "00:00.0"
            [measurement]
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        let db = Database::open_in_memory().unwrap();
        let runner = AgentRunner::new(
            PathBuf::from("python3"),
            PathBuf::from("/tmp"),
            Duration::from_secs(1),
            std::env::temp_dir(),
        );
        let orch = Orchestrator::new(config, db, runner);

        let cycle_id = orch.db.create_cycle("test_game", 12345).unwrap();
        // frame_time_p99_ms must be positive (the validity gate); the other
        // unspecified fields (psi_*) still default to 0.
        let agent_result =
            serde_json::json!({"response": "{\"fps_avg\": 60.0, \"frame_time_p99_ms\": 16.6}"});
        orch.persist_measurement(cycle_id, "baseline", &agent_result)
            .unwrap();

        let rows = orch.db.get_measurements(cycle_id, "baseline").unwrap();
        assert_eq!(rows.len(), 1);
        assert!((rows[0].fps_avg - 60.0).abs() < f64::EPSILON);
        assert_eq!(rows[0].psi_cpu_avg, 0.0);
    }

    #[test]
    fn persist_measurement_rejects_zero_frame_time_as_failed_run() {
        use crate::agent_runner::AgentRunner;
        use crate::config::CrucibleConfig;
        use crate::db::Database;
        use std::path::PathBuf;
        use std::time::Duration;

        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/crucible-test3.db"
            artifact_dir = "/tmp/crucible-artifacts"
            [vm]
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "00:00.0"
            [measurement]
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        let db = Database::open_in_memory().unwrap();
        let runner = AgentRunner::new(
            PathBuf::from("python3"),
            PathBuf::from("/tmp"),
            Duration::from_secs(1),
            std::env::temp_dir(),
        );
        let orch = Orchestrator::new(config, db, runner);
        let cycle_id = orch.db.create_cycle("test_game", 1).unwrap();
        // A run that produced no metrics: synthetic emits frame_time_p99_ms
        // = 0.0 when ops/sec came back 0. It must be rejected, not stored.
        let agent_result = serde_json::json!(
            {"response": "{\"fps_avg\": 0.0, \"frame_time_p99_ms\": 0.0}"}
        );
        assert!(orch
            .persist_measurement(cycle_id, "baseline", &agent_result)
            .is_err());
        assert_eq!(
            orch.db.get_measurements(cycle_id, "baseline").unwrap().len(),
            0
        );

        // Non-finite and negative frame times must never be stored either.
        // NaN/inf can't ride valid JSON, so for those the inner parse is the
        // rejecting layer ("NaN"/"inf" literals are malformed JSON); the
        // is_finite() guard is the in-process backstop should a future
        // parser accept them. -1.0 crosses the parse and pins the guard.
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -1.0] {
            let agent_result = serde_json::json!(
                {"response": format!("{{\"fps_avg\": 1.0, \"frame_time_p99_ms\": {bad}}}")}
            );
            assert!(
                orch.persist_measurement(cycle_id, "baseline", &agent_result)
                    .is_err(),
                "frame_time_p99_ms = {bad} must be rejected"
            );
        }
        assert_eq!(
            orch.db.get_measurements(cycle_id, "baseline").unwrap().len(),
            0
        );
    }

    fn make_orchestrator() -> Orchestrator {
        use crate::agent_runner::AgentRunner;
        use crate::config::CrucibleConfig;
        use crate::db::Database;
        use std::path::PathBuf;
        use std::time::Duration;

        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/crucible-eval.db"
            artifact_dir = "/tmp/crucible-artifacts"
            [vm]
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "00:00.0"
            [measurement]
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        let db = Database::open_in_memory().unwrap();
        let runner = AgentRunner::new(
            PathBuf::from("python3"),
            PathBuf::from("/tmp"),
            Duration::from_secs(1),
            std::env::temp_dir(),
        );
        Orchestrator::new(config, db, runner)
    }

    fn insert_phase(orch: &Orchestrator, cycle_id: i64, phase: &str, samples: &[(f64, f64, f64, f64, f64)]) {
        for (fps, fps_p1, ft, cpu, mem) in samples {
            orch.db
                .insert_measurement(cycle_id, phase, *fps, *fps_p1, *ft, *cpu, *mem, "{}")
                .unwrap();
        }
    }

    #[test]
    fn run_evaluation_accept_when_fps_significantly_higher() {
        let orch = make_orchestrator();
        let cycle_id = orch.db.create_cycle("g", 1).unwrap();
        insert_phase(
            &orch,
            cycle_id,
            "baseline",
            &[
                (60.0, 45.0, 25.0, 0.5, 1.2),
                (61.0, 46.0, 24.5, 0.5, 1.1),
                (59.5, 44.5, 25.5, 0.5, 1.2),
                (60.5, 45.5, 24.8, 0.5, 1.2),
                (60.2, 45.2, 25.1, 0.5, 1.2),
            ],
        );
        insert_phase(
            &orch,
            cycle_id,
            "comparison",
            &[
                (75.0, 60.0, 18.0, 0.4, 1.0),
                (76.0, 61.0, 17.5, 0.4, 1.0),
                (74.5, 59.5, 18.5, 0.4, 1.0),
                (75.5, 60.5, 17.8, 0.4, 1.0),
                (75.2, 60.2, 18.1, 0.4, 1.0),
            ],
        );

        let verdict = orch.run_evaluation(cycle_id).unwrap();
        assert_eq!(verdict, Verdict::Accept);
        let rows = orch.db.get_evaluations(cycle_id).unwrap();
        assert_eq!(rows.len(), METRIC_DEFS.len());
    }

    #[test]
    fn run_evaluation_regressed_when_fps_drops() {
        let orch = make_orchestrator();
        let cycle_id = orch.db.create_cycle("g", 1).unwrap();
        insert_phase(
            &orch,
            cycle_id,
            "baseline",
            &[
                (60.0, 45.0, 25.0, 0.5, 1.2),
                (61.0, 46.0, 24.5, 0.5, 1.1),
                (59.5, 44.5, 25.5, 0.5, 1.2),
                (60.5, 45.5, 24.8, 0.5, 1.2),
                (60.2, 45.2, 25.1, 0.5, 1.2),
            ],
        );
        insert_phase(
            &orch,
            cycle_id,
            "comparison",
            &[
                (45.0, 30.0, 35.0, 0.5, 1.2),
                (46.0, 31.0, 34.5, 0.5, 1.2),
                (44.5, 29.5, 35.5, 0.5, 1.2),
                (45.5, 30.5, 34.8, 0.5, 1.2),
                (45.2, 30.2, 35.1, 0.5, 1.2),
            ],
        );

        let verdict = orch.run_evaluation(cycle_id).unwrap();
        assert_eq!(verdict, Verdict::Regressed);
    }

    #[test]
    fn run_evaluation_neutral_with_single_sample_per_phase() {
        let orch = make_orchestrator();
        let cycle_id = orch.db.create_cycle("g", 1).unwrap();
        insert_phase(&orch, cycle_id, "baseline", &[(60.0, 45.0, 25.0, 0.5, 1.2)]);
        insert_phase(&orch, cycle_id, "comparison", &[(70.0, 55.0, 20.0, 0.4, 1.0)]);

        let verdict = orch.run_evaluation(cycle_id).unwrap();
        assert_eq!(verdict, Verdict::Neutral);
        let rows = orch.db.get_evaluations(cycle_id).unwrap();
        assert_eq!(rows.len(), METRIC_DEFS.len());
        for r in &rows {
            assert_eq!(r.verdict, "neutral");
        }
    }

    #[test]
    fn measurement_context_synthetic_omits_game_fields() {
        let orch = make_orchestrator();
        let ctx = measurement_context(&orch.config, "baseline", "synthetic", false);
        assert_eq!(ctx["phase"], "baseline");
        assert_eq!(ctx["workload_kind"], "synthetic");
        assert!(ctx.get("game_benchmark").is_none());
        assert!(ctx.get("mangohud_output").is_none());
    }

    #[test]
    fn measurement_context_game_threads_benchmark_and_log_path() {
        use crate::config::CrucibleConfig;

        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/x.db"
            artifact_dir = "/tmp/x"
            [vm]
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "none"
            [measurement]
            mode = "game"
            game_benchmark = "vkmark"
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        // capture_perfetto is now an explicit arg (the orchestrator sets it
        // only for the dedicated profiled run), never phase-derived.
        let ctx = measurement_context(&config, "comparison", "vkmark", false);
        assert_eq!(ctx["workload_kind"], "game");
        assert_eq!(ctx["game_benchmark"], "vkmark");
        assert_eq!(ctx["mangohud_output"], GUEST_MANGOHUD_OUTPUT);
        assert_eq!(ctx["phase"], "comparison");
        // stress-ng flags must not leak into the vkmark/glmark2 invocation,
        // but duration_secs is shared — it sizes the benchmark run and
        // MangoHud's finite log window.
        assert!(ctx.get("benchmark_args").is_none());
        assert_eq!(
            ctx["duration_secs"],
            serde_json::json!(config.measurement.benchmark_duration_secs)
        );
        // Analyze runs before ComparisonMeasurement, so the Perfetto trace
        // is captured during the BASELINE phase (as a separate profiled
        // repeat after the clean run) — not during comparison.
        assert!(ctx.get("capture_perfetto").is_none());

        // Passing capture_perfetto=false keeps a game baseline sample clean;
        // true adds the trace wiring (used only for the profiled capture run).
        let clean_baseline = measurement_context(&config, "baseline", "vkmark", false);
        assert!(clean_baseline.get("capture_perfetto").is_none());
        let baseline_ctx = measurement_context(&config, "baseline", "vkmark", true);
        assert_eq!(baseline_ctx["capture_perfetto"], serde_json::json!(true));
        assert_eq!(baseline_ctx["perfetto_output"], GUEST_PERFETTO_OUTPUT);
        assert_eq!(baseline_ctx["perfetto_host_dir"], "/tmp/x");
    }

    #[test]
    fn measurement_context_steam_threads_perfetto_for_profiled_run() {
        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/x.db"
            artifact_dir = "/tmp/x"
            [vm]
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "none"
            [measurement]
            mode = "steam"
            steam_app_id = 289070
            steam_launch_args = ["-benchmark", "graphicsbenchmark"]
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();

        let clean = measurement_context(&config, "baseline", "civ6", false);
        assert_eq!(clean["workload_kind"], "steam");
        assert_eq!(clean["steam_app_id"], 289070);
        assert!(clean.get("capture_perfetto").is_none());

        // Without this the steam analyzer gets no kernel trace and reasons
        // blind from summary metrics (observed cycle 2 of the Civ 6 grind).
        let profiled = measurement_context(&config, "baseline", "civ6", true);
        assert_eq!(profiled["capture_perfetto"], serde_json::json!(true));
        assert_eq!(profiled["perfetto_output"], GUEST_PERFETTO_OUTPUT);
        assert_eq!(profiled["perfetto_host_dir"], "/tmp/x");
    }

    #[test]
    fn orchestrator_constructs_vm_and_vsock_fields() {
        let orch = make_orchestrator();
        assert!(orch.current_kernel.is_none());
        assert_eq!(orch.vm_manager.state(), VmState::Stopped);
    }

    #[test]
    fn build_agent_task_envelope() {
        let task = build_task_envelope(
            AgentName::Analyzer,
            serde_json::json!({"game": "test"}),
            "claude-sonnet-4-20250514",
            8192,
            300,
            3,
        );
        assert_eq!(task.agent, AgentName::Analyzer);
        assert_eq!(task.context["game"], "test");
        assert_eq!(task.config.max_retries, 3);
    }

    #[test]
    fn determine_verdict_all_accept() {
        let evals = vec![MetricEvaluation {
            metric: "fps_avg".into(),
            baseline_mean: 60.0,
            comparison_mean: 70.0,
            delta_pct: 16.7,
            t_test: TTestResult {
                t_statistic: 5.0,
                degrees_of_freedom: 8.0,
                p_value: 0.001,
                significant: true,
            },
            cohens_d: 2.0,
            verdict: Verdict::Accept,
        }];
        assert_eq!(determine_overall_verdict(&evals), Verdict::Accept);
    }

    #[test]
    fn determine_verdict_any_regression_blocks() {
        let evals = vec![
            MetricEvaluation {
                metric: "fps_avg".into(),
                baseline_mean: 60.0,
                comparison_mean: 70.0,
                delta_pct: 16.7,
                t_test: TTestResult {
                    t_statistic: 5.0,
                    degrees_of_freedom: 8.0,
                    p_value: 0.001,
                    significant: true,
                },
                cohens_d: 2.0,
                verdict: Verdict::Accept,
            },
            MetricEvaluation {
                metric: "psi_memory".into(),
                baseline_mean: 1.0,
                comparison_mean: 5.0,
                delta_pct: 400.0,
                t_test: TTestResult {
                    t_statistic: 6.0,
                    degrees_of_freedom: 8.0,
                    p_value: 0.0001,
                    significant: true,
                },
                cohens_d: 3.0,
                verdict: Verdict::Regressed,
            },
        ];
        assert_eq!(determine_overall_verdict(&evals), Verdict::Regressed);
    }

    #[test]
    fn determine_verdict_all_neutral() {
        let evals = vec![MetricEvaluation {
            metric: "fps_avg".into(),
            baseline_mean: 60.0,
            comparison_mean: 60.1,
            delta_pct: 0.17,
            t_test: TTestResult {
                t_statistic: 0.2,
                degrees_of_freedom: 8.0,
                p_value: 0.85,
                significant: false,
            },
            cohens_d: 0.1,
            verdict: Verdict::Neutral,
        }];
        assert_eq!(determine_overall_verdict(&evals), Verdict::Neutral);
    }
}
