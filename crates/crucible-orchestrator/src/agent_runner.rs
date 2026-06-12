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
    artifact_dir: PathBuf,
}

impl AgentRunner {
    pub fn new(
        python_path: PathBuf,
        agents_dir: PathBuf,
        timeout: Duration,
        artifact_dir: PathBuf,
    ) -> Self {
        Self {
            python_path,
            agents_dir,
            timeout,
            artifact_dir,
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
            // Without this, a timed-out agent's Anthropic-API request keeps
            // running in the background (and racking up tokens) until the
            // python process exits on its own. kill_on_drop ensures the
            // subprocess dies as soon as the timeout future drops it.
            .kill_on_drop(true)
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

        // Tee stderr to an artifact file regardless of agent exit status so
        // post-run inspection can grep for built-in tool calls escaping the
        // `_BUILTIN_TOOLS_TO_DISALLOW` lockdown in claude_agent.py. Timed-out
        // agents skip this path because the timeout future returns before
        // wait_with_output() resolves; the timeout itself is the diagnostic.
        let agents_artifact_dir = self.artifact_dir.join("agents");
        tokio::fs::create_dir_all(&agents_artifact_dir)
            .await
            .with_context(|| {
                format!(
                    "failed to create agent stderr dir {}",
                    agents_artifact_dir.display()
                )
            })?;
        let stderr_path = agents_artifact_dir.join(format!("{}.stderr", task.task_id));
        tokio::fs::write(&stderr_path, &output.stderr)
            .await
            .with_context(|| {
                format!("failed to write agent stderr to {}", stderr_path.display())
            })?;

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

        // Persist the full envelope next to the stderr artifact: when a
        // cycle produces wrong-but-parseable data (fps_avg=0 from a live
        // GPU), the stderr tool-name mirror alone can't show what the
        // agent actually returned or logged.
        let result_path = agents_artifact_dir.join(format!("{}.result.json", task.task_id));
        if let Err(err) = tokio::fs::write(&result_path, &output.stdout).await {
            tracing::warn!(path = %result_path.display(), %err, "failed to persist agent result");
        }

        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crucible_common::protocol::{AgentConfig, AgentName, TaskEnvelope};
    use std::path::PathBuf;

    fn test_runner_with_artifact_dir(artifact_dir: PathBuf) -> AgentRunner {
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
            artifact_dir,
        )
    }

    #[tokio::test]
    async fn run_echo_agent_returns_context() {
        let tmp = tempfile::tempdir().unwrap();
        let runner = test_runner_with_artifact_dir(tmp.path().to_path_buf());
        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Echo,
            context: serde_json::json!({"message": "hello from rust"}),
            config: AgentConfig {
                model: "test".to_string(),
                max_tokens: 100,
                timeout_seconds: 10,
                max_retries: 3,
            },
        };

        let result = runner.run_agent(task.clone()).await.unwrap();
        assert_eq!(result.task_id, task.task_id);
        assert_eq!(
            result.status,
            crucible_common::protocol::TaskStatus::Success
        );
        assert_eq!(result.result["echo"]["message"], "hello from rust");

        // Stderr tee always produces a file, even when the agent emits
        // nothing on stderr (echo agent here). Empty file is fine; the
        // presence of the path is the contract.
        let stderr_path = tmp
            .path()
            .join("agents")
            .join(format!("{}.stderr", task.task_id));
        assert!(
            stderr_path.exists(),
            "expected stderr artifact at {}",
            stderr_path.display()
        );
    }

    #[tokio::test]
    async fn run_agent_timeout() {
        let agents_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("agents");

        let tmp = tempfile::tempdir().unwrap();
        let runner = AgentRunner::new(
            PathBuf::from("python3"),
            agents_dir,
            std::time::Duration::from_millis(1),
            tmp.path().to_path_buf(),
        );

        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Echo,
            context: serde_json::json!({}),
            config: AgentConfig {
                model: "test".to_string(),
                max_tokens: 100,
                timeout_seconds: 0,
                max_retries: 3,
            },
        };

        let result = runner.run_agent(task).await;
        assert!(result.is_err());
    }
}
