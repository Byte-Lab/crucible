use anyhow::{Context, Result};
use crucible_common::protocol::{AgentName, ResultEnvelope, TaskEnvelope};
use std::path::PathBuf;
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

pub struct AgentRunner {
    python_path: PathBuf,
    agents_dir: PathBuf,
    timeout: Duration,
}

impl AgentRunner {
    pub fn new(python_path: PathBuf, agents_dir: PathBuf, timeout: Duration) -> Self {
        Self {
            python_path,
            agents_dir,
            timeout,
        }
    }

    fn agent_module(&self, name: AgentName) -> String {
        let module = match name {
            AgentName::GameSelector => "game_selector",
            AgentName::GamePlayer => "game_player",
            AgentName::Profiler => "profiler",
            AgentName::Analyzer => "analyzer",
            AgentName::Optimizer => "optimizer",
            AgentName::Echo => "echo",
        };
        format!("agents.{}.agent", module)
    }

    pub async fn run_agent(&self, task: TaskEnvelope) -> Result<ResultEnvelope> {
        let module = self.agent_module(task.agent);
        let task_json =
            serde_json::to_string(&task).context("failed to serialize task envelope")?;

        let workspace_root = self.agents_dir.parent().unwrap_or(&self.agents_dir);

        let mut child = Command::new(&self.python_path)
            .arg("-m")
            .arg(&module)
            .env("PYTHONPATH", workspace_root)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .with_context(|| format!("failed to spawn agent: {}", module))?;

        let mut stdin = child.stdin.take().context("failed to open agent stdin")?;
        stdin
            .write_all(task_json.as_bytes())
            .await
            .context("failed to write task to agent stdin")?;
        drop(stdin);

        let output = tokio::time::timeout(self.timeout, child.wait_with_output())
            .await
            .with_context(|| {
                format!("agent {} timed out after {:?}", module, self.timeout)
            })?
            .with_context(|| format!("agent {} failed to execute", module))?;

        let stderr = String::from_utf8_lossy(&output.stderr);
        if !stderr.is_empty() {
            tracing::debug!(agent = %module, stderr = %stderr, "agent stderr");
        }

        if !output.status.success() {
            anyhow::bail!(
                "agent {} exited with status {}: {}",
                module,
                output.status,
                stderr
            );
        }

        let result: ResultEnvelope = serde_json::from_slice(&output.stdout)
            .with_context(|| {
                format!(
                    "failed to parse agent {} output: {}",
                    module,
                    String::from_utf8_lossy(&output.stdout)
                )
            })?;

        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crucible_common::protocol::{AgentConfig, AgentName, TaskEnvelope};
    use std::path::PathBuf;

    fn test_runner() -> AgentRunner {
        let agents_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("agents");

        AgentRunner::new(
            PathBuf::from("python3"),
            agents_dir,
            std::time::Duration::from_secs(10),
        )
    }

    #[tokio::test]
    async fn run_echo_agent_returns_context() {
        let runner = test_runner();
        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Echo,
            context: serde_json::json!({"message": "hello from rust"}),
            config: AgentConfig {
                model: "test".to_string(),
                max_tokens: 100,
                timeout_seconds: 10,
            },
        };

        let result = runner.run_agent(task.clone()).await.unwrap();
        assert_eq!(result.task_id, task.task_id);
        assert_eq!(
            result.status,
            crucible_common::protocol::TaskStatus::Success
        );
        assert_eq!(result.result["echo"]["message"], "hello from rust");
    }

    #[tokio::test]
    async fn run_agent_timeout() {
        let agents_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("agents");

        let runner = AgentRunner::new(
            PathBuf::from("python3"),
            agents_dir,
            std::time::Duration::from_millis(1),
        );

        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Echo,
            context: serde_json::json!({}),
            config: AgentConfig {
                model: "test".to_string(),
                max_tokens: 100,
                timeout_seconds: 0,
            },
        };

        let result = runner.run_agent(task).await;
        assert!(result.is_err());
    }
}
