use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::Path;

#[derive(Debug, Deserialize)]
pub struct CrucibleConfig {
    pub orchestrator: OrchestratorConfig,
    pub vm: VmConfig,
    #[serde(default)]
    pub measurement: MeasurementConfig,
    #[serde(default)]
    pub agents: AgentsConfig,
}

#[derive(Debug, Deserialize)]
pub struct OrchestratorConfig {
    pub db_path: String,
    pub artifact_dir: String,
    #[serde(default)]
    pub max_cycles: u64,
    #[serde(default = "default_cooldown")]
    pub cycle_cooldown_secs: u64,
}

#[derive(Debug, Deserialize)]
pub struct VmConfig {
    pub kernel_src: String,
    pub guest_rootfs: String,
    #[serde(default = "default_memory")]
    pub memory: String,
    #[serde(default = "default_cpus")]
    pub cpus: u32,
    pub vfio_device: String,
    #[serde(default = "default_boot_timeout")]
    pub boot_timeout_secs: u64,
    #[serde(default = "default_vsock_cid")]
    pub vsock_cid: u32,
}

#[derive(Debug, Deserialize)]
pub struct MeasurementConfig {
    #[serde(default = "default_runs_per_phase")]
    pub runs_per_phase: u32,
    #[serde(default = "default_warmup_runs")]
    pub warmup_runs: u32,
    #[serde(default = "default_significance")]
    pub significance_threshold: f64,
    #[serde(default = "default_effect_size")]
    pub effect_size_threshold: f64,
    #[serde(default = "default_max_stddev")]
    pub max_stddev_pct: f64,
}

#[derive(Debug, Deserialize)]
pub struct AgentsConfig {
    #[serde(default = "default_model")]
    pub model: String,
    #[serde(default = "default_max_retries")]
    pub max_retries: u32,
    #[serde(default = "default_timeout")]
    pub timeout_secs: u64,
    #[serde(default)]
    pub optimizer: OptimizerConfig,
    #[serde(default)]
    pub game_player: GamePlayerConfig,
}

#[derive(Debug, Deserialize)]
pub struct OptimizerConfig {
    #[serde(default = "default_max_attempts")]
    pub max_attempts_per_bottleneck: u32,
    #[serde(default = "default_allowed_layers")]
    pub allowed_layers: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct GamePlayerConfig {
    #[serde(default)]
    pub enabled: bool,
}

impl CrucibleConfig {
    pub fn from_file(path: &Path) -> Result<Self> {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read config: {}", path.display()))?;
        toml::from_str(&content)
            .with_context(|| format!("failed to parse config: {}", path.display()))
    }
}

fn default_cooldown() -> u64 {
    60
}
fn default_memory() -> String {
    "16G".to_string()
}
fn default_cpus() -> u32 {
    8
}
fn default_boot_timeout() -> u64 {
    60
}
fn default_vsock_cid() -> u32 {
    3
}
fn default_runs_per_phase() -> u32 {
    5
}
fn default_warmup_runs() -> u32 {
    1
}
fn default_significance() -> f64 {
    0.05
}
fn default_effect_size() -> f64 {
    0.5
}
fn default_max_stddev() -> f64 {
    10.0
}
fn default_model() -> String {
    "claude-sonnet-4-20250514".to_string()
}
fn default_max_retries() -> u32 {
    3
}
fn default_timeout() -> u64 {
    300
}
fn default_max_attempts() -> u32 {
    3
}
fn default_allowed_layers() -> Vec<String> {
    vec![
        "kernel".to_string(),
        "userspace".to_string(),
        "tuning".to_string(),
    ]
}

impl Default for MeasurementConfig {
    fn default() -> Self {
        Self {
            runs_per_phase: default_runs_per_phase(),
            warmup_runs: default_warmup_runs(),
            significance_threshold: default_significance(),
            effect_size_threshold: default_effect_size(),
            max_stddev_pct: default_max_stddev(),
        }
    }
}

impl Default for AgentsConfig {
    fn default() -> Self {
        Self {
            model: default_model(),
            max_retries: default_max_retries(),
            timeout_secs: default_timeout(),
            optimizer: OptimizerConfig::default(),
            game_player: GamePlayerConfig::default(),
        }
    }
}

impl Default for OptimizerConfig {
    fn default() -> Self {
        Self {
            max_attempts_per_bottleneck: default_max_attempts(),
            allowed_layers: default_allowed_layers(),
        }
    }
}

impl Default for GamePlayerConfig {
    fn default() -> Self {
        Self { enabled: false }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn parse_minimal_config() {
        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/crucible/state.db"
            artifact_dir = "/tmp/crucible/artifacts"

            [vm]
            kernel_src = "/home/void/upstream/linux"
            guest_rootfs = "/home/void/.crucible/rootfs"
            vfio_device = "03:00.0"

            [measurement]

            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.orchestrator.db_path, "/tmp/crucible/state.db");
        assert_eq!(config.vm.kernel_src, "/home/void/upstream/linux");
        assert_eq!(config.vm.memory, "16G"); // default
        assert_eq!(config.measurement.runs_per_phase, 5); // default
        assert_eq!(config.agents.model, "claude-sonnet-4-20250514"); // default
    }

    #[test]
    fn parse_full_config_from_file() {
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        write!(
            tmp,
            r#"
            [orchestrator]
            db_path = "~/.crucible/state.db"
            artifact_dir = "~/.crucible/artifacts"
            max_cycles = 10
            cycle_cooldown_secs = 120

            [vm]
            kernel_src = "/home/void/upstream/linux"
            guest_rootfs = "/home/void/.crucible/rootfs"
            memory = "32G"
            cpus = 16
            vfio_device = "03:00.0"
            boot_timeout_secs = 120
            vsock_cid = 5

            [measurement]
            runs_per_phase = 10
            warmup_runs = 2
            significance_threshold = 0.01
            effect_size_threshold = 0.8
            max_stddev_pct = 5

            [agents]
            model = "claude-opus-4-6-20250414"
            max_retries = 5
            timeout_secs = 600

            [agents.optimizer]
            max_attempts_per_bottleneck = 5
            allowed_layers = ["kernel", "tuning"]

            [agents.game_player]
            enabled = true
            "#
        )
        .unwrap();

        let config = CrucibleConfig::from_file(tmp.path()).unwrap();
        assert_eq!(config.vm.memory, "32G");
        assert_eq!(config.vm.cpus, 16);
        assert_eq!(config.measurement.runs_per_phase, 10);
        assert_eq!(config.agents.model, "claude-opus-4-6-20250414");
        assert_eq!(config.agents.optimizer.max_attempts_per_bottleneck, 5);
        assert_eq!(
            config.agents.optimizer.allowed_layers,
            vec!["kernel", "tuning"]
        );
        assert!(config.agents.game_player.enabled);
    }
}
