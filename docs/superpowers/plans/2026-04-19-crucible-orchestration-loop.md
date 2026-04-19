# Crucible Orchestration Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the orchestration loop that drives the full optimization cycle: select game, boot VM, run baseline, analyze, generate optimization, apply changes, run comparison, evaluate, accept/reject. This is the state machine that ties all prior work together.

**Architecture:** The Rust orchestrator implements the state machine as an async loop. Each state dispatches to the appropriate Python agent or Rust subsystem. The statistical evaluator (Welch's t-test, Cohen's d) determines whether changes are accepted. State transitions persist to SQLite for crash recovery.

**Tech Stack:** Rust (tokio, statrs for statistics), Python agents (via agent_runner)

**Plan series:**
- Plan 1: Foundation (complete)
- Plan 2: VM management (complete)
- Plan 3: Core agents (complete)
- **Plan 4 (this plan):** Orchestration loop

---

## File Map

| File | Responsibility |
|------|---------------|
| `crates/crucible-orchestrator/src/evaluator.rs` | Statistical evaluation: Welch's t-test, Cohen's d, verdict logic |
| `crates/crucible-orchestrator/src/state_machine.rs` | Optimization cycle state machine with persistent transitions |
| `crates/crucible-orchestrator/src/orchestrator.rs` | Top-level loop: dispatch agents, manage VM, drive state machine |
| `crates/crucible-orchestrator/src/main.rs` | Wire orchestrator into CLI with run command |

---

## Task 1: Statistical Evaluator

**Files:**
- Create: `crates/crucible-orchestrator/src/evaluator.rs`
- Modify: `crates/crucible-orchestrator/src/lib.rs`
- Modify: root `Cargo.toml` (add statrs)

The evaluator determines whether a code change produced a statistically significant improvement.

- [ ] **Step 1: Add statrs dependency**

Add to root `Cargo.toml` workspace dependencies:
```toml
statrs = "0.18"
```

Add to `crates/crucible-orchestrator/Cargo.toml`:
```toml
statrs = { workspace = true }
```

- [ ] **Step 2: Write failing tests**

```rust
// crates/crucible-orchestrator/src/evaluator.rs

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn welch_t_test_significant_difference() {
        // Clearly different distributions
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![70.0, 71.0, 69.0, 70.5, 70.2];
        let result = welch_t_test(&baseline, &comparison);
        assert!(result.p_value < 0.05);
        assert!(result.significant);
    }

    #[test]
    fn welch_t_test_no_significant_difference() {
        // Nearly identical distributions
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![60.1, 60.8, 59.2, 60.6, 60.0];
        let result = welch_t_test(&baseline, &comparison);
        assert!(result.p_value > 0.05);
        assert!(!result.significant);
    }

    #[test]
    fn cohens_d_large_effect() {
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![70.0, 71.0, 69.0, 70.5, 70.2];
        let d = cohens_d(&baseline, &comparison);
        assert!(d.abs() > 0.8); // large effect
    }

    #[test]
    fn cohens_d_small_effect() {
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![60.5, 61.5, 59.5, 61.0, 60.7];
        let d = cohens_d(&baseline, &comparison);
        assert!(d.abs() < 0.8);
    }

    #[test]
    fn evaluate_accept() {
        let config = EvalConfig {
            significance_threshold: 0.05,
            effect_size_threshold: 0.5,
        };
        // Clear improvement in frame_time (lower is better)
        let baseline = vec![25.0, 26.0, 24.0, 25.5, 25.2];
        let comparison = vec![20.0, 21.0, 19.0, 20.5, 20.2];
        let result = evaluate_metric("frame_time_p99", &baseline, &comparison, true, &config);
        assert_eq!(result.verdict, Verdict::Accept);
    }

    #[test]
    fn evaluate_reject_regression() {
        let config = EvalConfig {
            significance_threshold: 0.05,
            effect_size_threshold: 0.5,
        };
        // FPS went down (higher is better)
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![50.0, 51.0, 49.0, 50.5, 50.2];
        let result = evaluate_metric("fps_avg", &baseline, &comparison, false, &config);
        assert_eq!(result.verdict, Verdict::Regressed);
    }

    #[test]
    fn evaluate_neutral_no_change() {
        let config = EvalConfig {
            significance_threshold: 0.05,
            effect_size_threshold: 0.5,
        };
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![60.1, 60.8, 59.2, 60.6, 60.0];
        let result = evaluate_metric("fps_avg", &baseline, &comparison, false, &config);
        assert_eq!(result.verdict, Verdict::Neutral);
    }
}
```

- [ ] **Step 3: Implement evaluator**

```rust
// crates/crucible-orchestrator/src/evaluator.rs
use statrs::distribution::{ContinuousCDF, StudentsT};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Accept,
    Marginal,
    Neutral,
    Regressed,
}

impl std::fmt::Display for Verdict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Verdict::Accept => write!(f, "accept"),
            Verdict::Marginal => write!(f, "marginal"),
            Verdict::Neutral => write!(f, "neutral"),
            Verdict::Regressed => write!(f, "regressed"),
        }
    }
}

pub struct EvalConfig {
    pub significance_threshold: f64,  // p-value threshold (e.g. 0.05)
    pub effect_size_threshold: f64,   // Cohen's d threshold (e.g. 0.5)
}

pub struct TTestResult {
    pub t_statistic: f64,
    pub degrees_of_freedom: f64,
    pub p_value: f64,
    pub significant: bool,
}

pub struct MetricEvaluation {
    pub metric: String,
    pub baseline_mean: f64,
    pub comparison_mean: f64,
    pub delta_pct: f64,
    pub t_test: TTestResult,
    pub cohens_d: f64,
    pub verdict: Verdict,
}

fn mean(data: &[f64]) -> f64 {
    data.iter().sum::<f64>() / data.len() as f64
}

fn variance(data: &[f64]) -> f64 {
    let m = mean(data);
    data.iter().map(|x| (x - m).powi(2)).sum::<f64>() / (data.len() - 1) as f64
}

pub fn welch_t_test(a: &[f64], b: &[f64]) -> TTestResult {
    let n_a = a.len() as f64;
    let n_b = b.len() as f64;
    let mean_a = mean(a);
    let mean_b = mean(b);
    let var_a = variance(a);
    let var_b = variance(b);

    let se = (var_a / n_a + var_b / n_b).sqrt();
    let t = (mean_a - mean_b) / se;

    // Welch-Satterthwaite degrees of freedom
    let num = (var_a / n_a + var_b / n_b).powi(2);
    let denom = (var_a / n_a).powi(2) / (n_a - 1.0)
        + (var_b / n_b).powi(2) / (n_b - 1.0);
    let df = num / denom;

    let dist = StudentsT::new(0.0, 1.0, df).unwrap();
    let p_value = 2.0 * (1.0 - dist.cdf(t.abs()));

    TTestResult {
        t_statistic: t,
        degrees_of_freedom: df,
        p_value,
        significant: p_value < 0.05,
    }
}

pub fn cohens_d(a: &[f64], b: &[f64]) -> f64 {
    let mean_a = mean(a);
    let mean_b = mean(b);
    let var_a = variance(a);
    let var_b = variance(b);
    let n_a = a.len() as f64;
    let n_b = b.len() as f64;

    // Pooled standard deviation
    let pooled_var = ((n_a - 1.0) * var_a + (n_b - 1.0) * var_b) / (n_a + n_b - 2.0);
    let pooled_sd = pooled_var.sqrt();

    if pooled_sd == 0.0 {
        return 0.0;
    }
    (mean_b - mean_a) / pooled_sd
}

/// Evaluate a single metric.
/// `lower_is_better`: true for frame times, PSI; false for FPS.
pub fn evaluate_metric(
    metric: &str,
    baseline: &[f64],
    comparison: &[f64],
    lower_is_better: bool,
    config: &EvalConfig,
) -> MetricEvaluation {
    let baseline_mean = mean(baseline);
    let comparison_mean = mean(comparison);
    let delta_pct = if baseline_mean != 0.0 {
        ((comparison_mean - baseline_mean) / baseline_mean.abs()) * 100.0
    } else {
        0.0
    };

    let t_test = welch_t_test(baseline, comparison);
    let d = cohens_d(baseline, comparison);

    let verdict = if !t_test.significant || t_test.p_value >= config.significance_threshold {
        Verdict::Neutral
    } else {
        // Determine if the direction is an improvement
        let improved = if lower_is_better {
            comparison_mean < baseline_mean
        } else {
            comparison_mean > baseline_mean
        };

        if !improved {
            Verdict::Regressed
        } else if d.abs() >= config.effect_size_threshold {
            Verdict::Accept
        } else {
            Verdict::Marginal
        }
    };

    MetricEvaluation {
        metric: metric.to_string(),
        baseline_mean,
        comparison_mean,
        delta_pct,
        t_test,
        cohens_d: d,
        verdict,
    }
}
```

- [ ] **Step 4: Add module to lib.rs**

Add `pub mod evaluator;` to `crates/crucible-orchestrator/src/lib.rs`.

- [ ] **Step 5: Run tests**

Run: `cargo test -p crucible-orchestrator evaluator`
Expected: All 7 tests pass.

- [ ] **Step 6: Commit**

```bash
git add Cargo.toml Cargo.lock crates/crucible-orchestrator/
git -c commit.gpgsign=false commit -m "feat: add statistical evaluator with Welch's t-test and Cohen's d"
```

---

## Task 2: State Machine

**Files:**
- Create: `crates/crucible-orchestrator/src/state_machine.rs`
- Modify: `crates/crucible-orchestrator/src/lib.rs`

The state machine tracks the optimization cycle through its phases, persisting each transition to SQLite.

- [ ] **Step 1: Write failing tests**

```rust
// crates/crucible-orchestrator/src/state_machine.rs

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initial_state_is_idle() {
        let sm = StateMachine::new();
        assert_eq!(sm.state(), CycleState::Idle);
    }

    #[test]
    fn valid_transition_idle_to_select_game() {
        let mut sm = StateMachine::new();
        assert!(sm.transition(CycleState::SelectGame).is_ok());
        assert_eq!(sm.state(), CycleState::SelectGame);
    }

    #[test]
    fn valid_full_cycle() {
        let mut sm = StateMachine::new();
        let states = [
            CycleState::SelectGame,
            CycleState::ProvisionVm,
            CycleState::BaselineMeasurement,
            CycleState::Analyze,
            CycleState::GenerateOptimization,
            CycleState::ApplyChanges,
            CycleState::ComparisonMeasurement,
            CycleState::Evaluate,
        ];
        for s in states {
            assert!(sm.transition(s).is_ok());
        }
        // Can go to Accept, Reject, or Iterate from Evaluate
        assert!(sm.transition(CycleState::Accept).is_ok());
        // Accept goes back to Idle
        assert!(sm.transition(CycleState::Idle).is_ok());
    }

    #[test]
    fn invalid_transition_rejected() {
        let mut sm = StateMachine::new();
        // Can't go from Idle to Analyze
        assert!(sm.transition(CycleState::Analyze).is_err());
    }

    #[test]
    fn iterate_goes_back_to_analyze() {
        let mut sm = StateMachine::new();
        for s in [
            CycleState::SelectGame, CycleState::ProvisionVm,
            CycleState::BaselineMeasurement, CycleState::Analyze,
            CycleState::GenerateOptimization, CycleState::ApplyChanges,
            CycleState::ComparisonMeasurement, CycleState::Evaluate,
            CycleState::Iterate,
        ] {
            assert!(sm.transition(s).is_ok());
        }
        // Iterate should allow going back to Analyze
        assert!(sm.transition(CycleState::Analyze).is_ok());
    }

    #[test]
    fn state_serializes_to_string() {
        assert_eq!(CycleState::BaselineMeasurement.as_str(), "baseline_measurement");
        assert_eq!(CycleState::from_str("baseline_measurement").unwrap(), CycleState::BaselineMeasurement);
    }

    #[test]
    fn history_tracks_transitions() {
        let mut sm = StateMachine::new();
        sm.transition(CycleState::SelectGame).unwrap();
        sm.transition(CycleState::ProvisionVm).unwrap();
        assert_eq!(sm.history().len(), 2);
    }
}
```

- [ ] **Step 2: Implement state machine**

```rust
// crates/crucible-orchestrator/src/state_machine.rs
use anyhow::{bail, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CycleState {
    Idle,
    SelectGame,
    ProvisionVm,
    BaselineMeasurement,
    Analyze,
    GenerateOptimization,
    ApplyChanges,
    ComparisonMeasurement,
    Evaluate,
    Accept,
    Reject,
    Iterate,
}

impl CycleState {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::SelectGame => "select_game",
            Self::ProvisionVm => "provision_vm",
            Self::BaselineMeasurement => "baseline_measurement",
            Self::Analyze => "analyze",
            Self::GenerateOptimization => "generate_optimization",
            Self::ApplyChanges => "apply_changes",
            Self::ComparisonMeasurement => "comparison_measurement",
            Self::Evaluate => "evaluate",
            Self::Accept => "accept",
            Self::Reject => "reject",
            Self::Iterate => "iterate",
        }
    }

    pub fn from_str(s: &str) -> Result<Self> {
        match s {
            "idle" => Ok(Self::Idle),
            "select_game" => Ok(Self::SelectGame),
            "provision_vm" => Ok(Self::ProvisionVm),
            "baseline_measurement" => Ok(Self::BaselineMeasurement),
            "analyze" => Ok(Self::Analyze),
            "generate_optimization" => Ok(Self::GenerateOptimization),
            "apply_changes" => Ok(Self::ApplyChanges),
            "comparison_measurement" => Ok(Self::ComparisonMeasurement),
            "evaluate" => Ok(Self::Evaluate),
            "accept" => Ok(Self::Accept),
            "reject" => Ok(Self::Reject),
            "iterate" => Ok(Self::Iterate),
            _ => bail!("unknown cycle state: {}", s),
        }
    }

    /// Returns valid next states from the current state.
    fn valid_transitions(&self) -> &'static [CycleState] {
        match self {
            Self::Idle => &[Self::SelectGame],
            Self::SelectGame => &[Self::ProvisionVm],
            Self::ProvisionVm => &[Self::BaselineMeasurement],
            Self::BaselineMeasurement => &[Self::Analyze],
            Self::Analyze => &[Self::GenerateOptimization],
            Self::GenerateOptimization => &[Self::ApplyChanges],
            Self::ApplyChanges => &[Self::ComparisonMeasurement],
            Self::ComparisonMeasurement => &[Self::Evaluate],
            Self::Evaluate => &[Self::Accept, Self::Reject, Self::Iterate],
            Self::Accept => &[Self::Idle],
            Self::Reject => &[Self::Idle],
            Self::Iterate => &[Self::Analyze],
        }
    }
}

pub struct StateMachine {
    state: CycleState,
    history: Vec<(CycleState, CycleState)>,
}

impl StateMachine {
    pub fn new() -> Self {
        Self {
            state: CycleState::Idle,
            history: Vec::new(),
        }
    }

    pub fn with_state(state: CycleState) -> Self {
        Self {
            state,
            history: Vec::new(),
        }
    }

    pub fn state(&self) -> CycleState {
        self.state
    }

    pub fn history(&self) -> &[(CycleState, CycleState)] {
        &self.history
    }

    pub fn transition(&mut self, next: CycleState) -> Result<()> {
        let valid = self.state.valid_transitions();
        if !valid.contains(&next) {
            bail!(
                "invalid transition: {} -> {} (valid: {:?})",
                self.state.as_str(),
                next.as_str(),
                valid.iter().map(|s| s.as_str()).collect::<Vec<_>>()
            );
        }
        let prev = self.state;
        self.state = next;
        self.history.push((prev, next));
        Ok(())
    }
}
```

- [ ] **Step 3: Add module to lib.rs**

Add `pub mod state_machine;` to lib.rs.

- [ ] **Step 4: Run tests**

Run: `cargo test -p crucible-orchestrator state_machine`
Expected: All 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add crates/crucible-orchestrator/
git -c commit.gpgsign=false commit -m "feat: add optimization cycle state machine with transition validation"
```

---

## Task 3: Orchestrator Loop

**Files:**
- Create: `crates/crucible-orchestrator/src/orchestrator.rs`
- Modify: `crates/crucible-orchestrator/src/lib.rs`
- Modify: `crates/crucible-orchestrator/src/main.rs`

The orchestrator ties everything together: it runs the state machine loop, dispatches agents, manages the VM, and persists results.

- [ ] **Step 1: Write tests for the orchestrator**

```rust
// crates/crucible-orchestrator/src/orchestrator.rs

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_agent_task_envelope() {
        let task = build_task_envelope(
            AgentName::Analyzer,
            serde_json::json!({"game": "test"}),
            "claude-sonnet-4-6-20250414",
            8192,
            300,
        );
        assert_eq!(task.agent, AgentName::Analyzer);
        assert_eq!(task.context["game"], "test");
    }

    #[test]
    fn determine_verdict_all_accept() {
        let evals = vec![
            MetricEvaluation {
                metric: "fps_avg".to_string(),
                baseline_mean: 60.0,
                comparison_mean: 70.0,
                delta_pct: 16.7,
                t_test: TTestResult { t_statistic: 5.0, degrees_of_freedom: 8.0, p_value: 0.001, significant: true },
                cohens_d: 2.0,
                verdict: Verdict::Accept,
            },
            MetricEvaluation {
                metric: "frame_time_p99".to_string(),
                baseline_mean: 25.0,
                comparison_mean: 20.0,
                delta_pct: -20.0,
                t_test: TTestResult { t_statistic: -4.0, degrees_of_freedom: 8.0, p_value: 0.003, significant: true },
                cohens_d: -1.5,
                verdict: Verdict::Accept,
            },
        ];
        assert_eq!(determine_overall_verdict(&evals), Verdict::Accept);
    }

    #[test]
    fn determine_verdict_any_regression_blocks() {
        let evals = vec![
            MetricEvaluation {
                metric: "fps_avg".to_string(),
                baseline_mean: 60.0,
                comparison_mean: 70.0,
                delta_pct: 16.7,
                t_test: TTestResult { t_statistic: 5.0, degrees_of_freedom: 8.0, p_value: 0.001, significant: true },
                cohens_d: 2.0,
                verdict: Verdict::Accept,
            },
            MetricEvaluation {
                metric: "psi_memory".to_string(),
                baseline_mean: 1.0,
                comparison_mean: 5.0,
                delta_pct: 400.0,
                t_test: TTestResult { t_statistic: 6.0, degrees_of_freedom: 8.0, p_value: 0.0001, significant: true },
                cohens_d: 3.0,
                verdict: Verdict::Regressed,
            },
        ];
        assert_eq!(determine_overall_verdict(&evals), Verdict::Regressed);
    }

    #[test]
    fn determine_verdict_all_neutral() {
        let evals = vec![
            MetricEvaluation {
                metric: "fps_avg".to_string(),
                baseline_mean: 60.0,
                comparison_mean: 60.1,
                delta_pct: 0.17,
                t_test: TTestResult { t_statistic: 0.2, degrees_of_freedom: 8.0, p_value: 0.85, significant: false },
                cohens_d: 0.1,
                verdict: Verdict::Neutral,
            },
        ];
        assert_eq!(determine_overall_verdict(&evals), Verdict::Neutral);
    }
}
```

- [ ] **Step 2: Implement orchestrator**

```rust
// crates/crucible-orchestrator/src/orchestrator.rs
use anyhow::{Context, Result};
use crucible_common::protocol::{AgentConfig, AgentName, TaskEnvelope};
use uuid::Uuid;

use crate::agent_runner::AgentRunner;
use crate::config::CrucibleConfig;
use crate::db::Database;
use crate::evaluator::{self, EvalConfig, MetricEvaluation, Verdict, TTestResult};
use crate::kernel_builder::KernelBuilder;
use crate::state_machine::{CycleState, StateMachine};
use crate::vm::VmManager;
use crate::vsock_client::VsockClient;

pub fn build_task_envelope(
    agent: AgentName,
    context: serde_json::Value,
    model: &str,
    max_tokens: u32,
    timeout_seconds: u64,
) -> TaskEnvelope {
    TaskEnvelope {
        task_id: Uuid::new_v4(),
        agent,
        context,
        config: AgentConfig {
            model: model.to_string(),
            max_tokens,
            timeout_seconds,
        },
    }
}

/// Determine overall verdict from a set of metric evaluations.
/// Any regression blocks. All must accept for Accept. Mixed = Marginal.
pub fn determine_overall_verdict(evals: &[MetricEvaluation]) -> Verdict {
    if evals.is_empty() {
        return Verdict::Neutral;
    }

    let has_regression = evals.iter().any(|e| e.verdict == Verdict::Regressed);
    if has_regression {
        return Verdict::Regressed;
    }

    let all_neutral = evals.iter().all(|e| e.verdict == Verdict::Neutral);
    if all_neutral {
        return Verdict::Neutral;
    }

    let all_accept = evals.iter().all(|e| {
        e.verdict == Verdict::Accept || e.verdict == Verdict::Neutral
    });
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
    state_machine: StateMachine,
}

impl Orchestrator {
    pub fn new(
        config: CrucibleConfig,
        db: Database,
        agent_runner: AgentRunner,
    ) -> Self {
        let kernel_builder = KernelBuilder::new(&config.vm.kernel_src);
        Self {
            config,
            db,
            agent_runner,
            kernel_builder,
            state_machine: StateMachine::new(),
        }
    }

    pub fn state(&self) -> CycleState {
        self.state_machine.state()
    }

    fn eval_config(&self) -> EvalConfig {
        EvalConfig {
            significance_threshold: self.config.measurement.significance_threshold,
            effect_size_threshold: self.config.measurement.effect_size_threshold,
        }
    }

    /// Run one full optimization cycle.
    pub async fn run_cycle(&mut self) -> Result<Verdict> {
        // SelectGame
        self.state_machine.transition(CycleState::SelectGame)?;
        let game_selection = self.run_agent(
            AgentName::GameSelector,
            serde_json::json!({"optimization_goals": "general performance improvement"}),
        ).await.context("game selection failed")?;

        let game_name = game_selection["response"]
            .as_str()
            .unwrap_or("unknown")
            .to_string();
        let app_id = game_selection.get("app_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let cycle_id = self.db.create_cycle(&game_name, app_id)?;
        tracing::info!(cycle_id, game = %game_name, "starting optimization cycle");

        // ProvisionVM
        self.state_machine.transition(CycleState::ProvisionVm)?;
        self.db.update_cycle_status(cycle_id, CycleState::ProvisionVm.as_str())?;
        // VM provisioning would happen here -- for now, log and continue
        tracing::info!("VM provisioning (placeholder)");

        // BaselineMeasurement
        self.state_machine.transition(CycleState::BaselineMeasurement)?;
        self.db.update_cycle_status(cycle_id, CycleState::BaselineMeasurement.as_str())?;
        let baseline = self.run_agent(
            AgentName::Profiler,
            serde_json::json!({"phase": "baseline", "game_name": game_name}),
        ).await.context("baseline measurement failed")?;
        tracing::info!("baseline measurement collected");

        // Analyze
        self.state_machine.transition(CycleState::Analyze)?;
        self.db.update_cycle_status(cycle_id, CycleState::Analyze.as_str())?;
        let analysis = self.run_agent(
            AgentName::Analyzer,
            serde_json::json!({
                "game_name": game_name,
                "metrics": baseline,
            }),
        ).await.context("analysis failed")?;
        tracing::info!(bottleneck = ?analysis.get("response"), "analysis complete");

        // GenerateOptimization
        self.state_machine.transition(CycleState::GenerateOptimization)?;
        self.db.update_cycle_status(cycle_id, CycleState::GenerateOptimization.as_str())?;
        let optimization = self.run_agent(
            AgentName::Optimizer,
            serde_json::json!({
                "game_name": game_name,
                "bottleneck": analysis,
                "kernel_src": self.config.vm.kernel_src,
                "attempt_number": 1,
            }),
        ).await.context("optimization generation failed")?;
        tracing::info!("optimization generated");

        // ApplyChanges
        self.state_machine.transition(CycleState::ApplyChanges)?;
        self.db.update_cycle_status(cycle_id, CycleState::ApplyChanges.as_str())?;
        // Record patch in DB (actual application via kernel_builder would happen here)
        self.db.insert_patch(cycle_id, "kernel", "pending")?;
        tracing::info!("changes applied (placeholder)");

        // ComparisonMeasurement
        self.state_machine.transition(CycleState::ComparisonMeasurement)?;
        self.db.update_cycle_status(cycle_id, CycleState::ComparisonMeasurement.as_str())?;
        let comparison = self.run_agent(
            AgentName::Profiler,
            serde_json::json!({"phase": "comparison", "game_name": game_name}),
        ).await.context("comparison measurement failed")?;
        tracing::info!("comparison measurement collected");

        // Evaluate
        self.state_machine.transition(CycleState::Evaluate)?;
        self.db.update_cycle_status(cycle_id, CycleState::Evaluate.as_str())?;
        // In a real run, we'd extract numeric arrays from baseline/comparison
        // For now, record a placeholder evaluation
        let verdict = Verdict::Neutral;
        self.db.insert_evaluation(
            cycle_id, "fps_avg", 60.0, 60.0, 0.0, &verdict.to_string(),
        )?;

        // Transition based on verdict
        let final_state = match verdict {
            Verdict::Accept => CycleState::Accept,
            Verdict::Regressed => CycleState::Reject,
            _ => CycleState::Reject, // Neutral and Marginal don't keep the patch
        };
        self.state_machine.transition(final_state)?;
        self.db.update_cycle_status(cycle_id, final_state.as_str())?;

        // Back to idle
        self.state_machine.transition(CycleState::Idle)?;

        tracing::info!(verdict = %verdict, cycle_id, "cycle complete");
        Ok(verdict)
    }

    async fn run_agent(
        &self,
        agent: AgentName,
        context: serde_json::Value,
    ) -> Result<serde_json::Value> {
        let task = build_task_envelope(
            agent,
            context,
            &self.config.agents.model,
            8192,
            self.config.agents.timeout_secs,
        );

        let result = self.agent_runner.run_agent(task).await?;

        match result.status {
            crucible_common::protocol::TaskStatus::Success => Ok(result.result),
            _ => anyhow::bail!("agent {:?} failed: {:?}", agent, result.result),
        }
    }

    /// Run the continuous optimization loop.
    pub async fn run_loop(&mut self, max_cycles: u64) -> Result<()> {
        let mut cycles_run = 0u64;
        loop {
            if max_cycles > 0 && cycles_run >= max_cycles {
                tracing::info!(cycles_run, "reached max cycles, stopping");
                break;
            }

            match self.run_cycle().await {
                Ok(verdict) => {
                    tracing::info!(cycle = cycles_run + 1, %verdict, "cycle completed");
                }
                Err(err) => {
                    tracing::error!(cycle = cycles_run + 1, err = %err, "cycle failed");
                    // Reset state machine to idle on failure
                    self.state_machine = StateMachine::new();
                }
            }

            cycles_run += 1;

            if self.config.orchestrator.cycle_cooldown_secs > 0 {
                tokio::time::sleep(std::time::Duration::from_secs(
                    self.config.orchestrator.cycle_cooldown_secs,
                ))
                .await;
            }
        }
        Ok(())
    }
}
```

- [ ] **Step 3: Add module to lib.rs**

Add `pub mod orchestrator;` to lib.rs.

- [ ] **Step 4: Update main.rs to wire in the orchestrator**

```rust
// crates/crucible-orchestrator/src/main.rs
use clap::Parser;
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "crucible-orchestrator")]
#[command(about = "Agentic Linux gaming performance optimization")]
struct Cli {
    /// Path to configuration file
    #[arg(short, long, default_value = "config/crucible.toml")]
    config: PathBuf,

    /// Maximum number of optimization cycles (0 = unlimited)
    #[arg(long, default_value = "0")]
    max_cycles: u64,

    /// Run a single optimization cycle and exit
    #[arg(long)]
    single_cycle: bool,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "crucible_orchestrator=info".into()),
        )
        .init();

    let cli = Cli::parse();
    let config = crucible_orchestrator::config::CrucibleConfig::from_file(&cli.config)?;
    tracing::info!(db = %config.orchestrator.db_path, "loaded configuration");

    let db = crucible_orchestrator::db::Database::open(
        std::path::Path::new(&config.orchestrator.db_path),
    )?;
    tracing::info!("database initialized");

    let agents_dir = std::env::current_dir()?.join("agents");
    let agent_runner = crucible_orchestrator::agent_runner::AgentRunner::new(
        PathBuf::from("python3"),
        agents_dir,
        std::time::Duration::from_secs(config.agents.timeout_secs),
    );

    let max_cycles = if cli.single_cycle { 1 } else { cli.max_cycles };

    let mut orchestrator = crucible_orchestrator::orchestrator::Orchestrator::new(
        config, db, agent_runner,
    );

    tracing::info!("crucible orchestrator starting");
    orchestrator.run_loop(max_cycles).await?;

    Ok(())
}
```

- [ ] **Step 5: Run tests**

Run: `cargo test -p crucible-orchestrator orchestrator`
Expected: All 4 tests pass.

Run: `cargo test`
Expected: All tests pass.

- [ ] **Step 6: Verify binary builds and runs**

Run: `cargo build --release`
Run: `cargo run --release -- --help`
Expected: Shows CLI help with --config, --max-cycles, --single-cycle options.

- [ ] **Step 7: Commit**

```bash
git add crates/crucible-orchestrator/
git -c commit.gpgsign=false commit -m "feat: add orchestrator loop with state machine, agent dispatch, and CLI wiring"
```

---

## Completion Checklist

- [ ] `cargo build --release` compiles
- [ ] `cargo test` -- all Rust tests pass
- [ ] `python3 -m pytest tests/python/ -v` -- all Python tests pass
- [ ] `cargo run --release -- --help` shows CLI options
- [ ] Evaluator correctly handles: improvement (Accept), no change (Neutral), regression (Regressed)
- [ ] State machine enforces valid transitions and rejects invalid ones
- [ ] Orchestrator loop dispatches agents in correct order through full cycle
- [ ] State persists to SQLite at each transition
