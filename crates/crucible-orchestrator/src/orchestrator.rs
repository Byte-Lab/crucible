use anyhow::Result;
use crucible_common::protocol::{AgentConfig, AgentName, TaskEnvelope};
use uuid::Uuid;

use crate::agent_runner::AgentRunner;
use crate::config::CrucibleConfig;
use crate::db::Database;
use crate::evaluator::{EvalConfig, MetricEvaluation, Verdict};
use crate::kernel_builder::KernelBuilder;
use crate::state_machine::{CycleState, StateMachine};

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
    #[allow(dead_code)]
    kernel_builder: KernelBuilder,
    state_machine: StateMachine,
}

impl Orchestrator {
    pub fn new(config: CrucibleConfig, db: Database, agent_runner: AgentRunner) -> Self {
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

    pub fn eval_config(&self) -> EvalConfig {
        EvalConfig {
            significance_threshold: self.config.measurement.significance_threshold,
            effect_size_threshold: self.config.measurement.effect_size_threshold,
        }
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

    pub async fn run_cycle(&mut self) -> Result<()> {
        // SelectGame
        self.state_machine
            .transition(CycleState::SelectGame)
            .map_err(|e| anyhow::anyhow!(e))?;
        tracing::info!(state = %self.state_machine.state(), "cycle state transition");

        let game_context = serde_json::json!({
            "action": "select_game",
        });
        let game_result = self.run_agent(AgentName::GameSelector, game_context).await?;

        // The agent returns {"response": "<json string>"} -- try to parse the inner JSON
        let game_info = game_result["response"]
            .as_str()
            .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok())
            .unwrap_or(game_result.clone());

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
        tracing::info!(state = %self.state_machine.state(), "provisioning VM (placeholder)");

        // BaselineMeasurement
        self.state_machine
            .transition(CycleState::BaselineMeasurement)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::BaselineMeasurement.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "running baseline measurement");

        let baseline_context = serde_json::json!({
            "action": "measure",
            "phase": "baseline",
            "game": game_name,
            "runs": self.config.measurement.runs_per_phase,
        });
        let _baseline_result = self
            .run_agent(AgentName::Profiler, baseline_context)
            .await?;

        // Analyze
        self.state_machine
            .transition(CycleState::Analyze)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::Analyze.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "analyzing performance data");

        let analyze_context = serde_json::json!({
            "action": "analyze",
            "game": game_name,
            "cycle_id": cycle_id,
        });
        let _analysis = self
            .run_agent(AgentName::Analyzer, analyze_context)
            .await?;

        // GenerateOptimization
        self.state_machine
            .transition(CycleState::GenerateOptimization)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::GenerateOptimization.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "generating optimization");

        let optimize_context = serde_json::json!({
            "action": "optimize",
            "game": game_name,
            "cycle_id": cycle_id,
        });
        let optimization = self
            .run_agent(AgentName::Optimizer, optimize_context)
            .await?;

        // ApplyChanges
        self.state_machine
            .transition(CycleState::ApplyChanges)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::ApplyChanges.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "applying changes (placeholder)");

        if let Some(patch_path) = optimization["patch_path"].as_str() {
            let layer = optimization["layer"].as_str().unwrap_or("kernel");
            self.db.insert_patch(cycle_id, layer, patch_path)?;
            tracing::info!(patch = patch_path, layer, "patch recorded");
        }

        // ComparisonMeasurement
        self.state_machine
            .transition(CycleState::ComparisonMeasurement)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::ComparisonMeasurement.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "running comparison measurement");

        let comparison_context = serde_json::json!({
            "action": "measure",
            "phase": "comparison",
            "game": game_name,
            "runs": self.config.measurement.runs_per_phase,
        });
        let _comparison_result = self
            .run_agent(AgentName::Profiler, comparison_context)
            .await?;

        // Evaluate
        self.state_machine
            .transition(CycleState::Evaluate)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::Evaluate.as_str())?;
        tracing::info!(state = %self.state_machine.state(), "evaluating results");

        // Accept/Reject (placeholder: accept for now)
        self.state_machine
            .transition(CycleState::Accept)
            .map_err(|e| anyhow::anyhow!(e))?;
        self.db
            .update_cycle_status(cycle_id, CycleState::Accept.as_str())?;
        tracing::info!(state = %self.state_machine.state(), cycle_id, "cycle completed");

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

        tracing::info!(total_cycles = cycles_completed, "orchestrator loop finished");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evaluator::TTestResult;

    #[test]
    fn build_agent_task_envelope() {
        let task = build_task_envelope(
            AgentName::Analyzer,
            serde_json::json!({"game": "test"}),
            "claude-sonnet-4-20250514",
            8192,
            300,
        );
        assert_eq!(task.agent, AgentName::Analyzer);
        assert_eq!(task.context["game"], "test");
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
