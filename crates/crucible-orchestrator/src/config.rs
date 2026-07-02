use anyhow::{Context, Result};
use serde::Deserialize;
use std::collections::HashMap;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
pub struct CrucibleConfig {
    pub orchestrator: OrchestratorConfig,
    pub vm: VmConfig,
    #[serde(default)]
    pub measurement: MeasurementConfig,
    #[serde(default)]
    pub agents: AgentsConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OrchestratorConfig {
    pub db_path: String,
    pub artifact_dir: String,
    #[serde(default)]
    pub max_cycles: u64,
    #[serde(default = "default_cooldown")]
    pub cycle_cooldown_secs: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct VmConfig {
    pub kernel_src: String,
    pub guest_rootfs: String,
    #[serde(default = "default_memory")]
    pub memory: String,
    #[serde(default = "default_cpus")]
    pub cpus: u32,
    /// Empty string or "none" skips GPU passthrough — needed for the
    /// synthetic loop on commodity hardware.
    #[serde(default)]
    pub vfio_device: String,
    /// Optional host path overlaid into the guest at /opt/crucible/guest
    /// via vng's --rodir. Lets the orchestrator drive an updated guest
    /// agent without rebuilding the rootfs every iteration. Empty = no
    /// overlay, guest uses whatever the rootfs has at /opt/crucible/guest.
    #[serde(default)]
    pub guest_payload: String,
    #[serde(default = "default_boot_timeout")]
    pub boot_timeout_secs: u64,
    #[serde(default = "default_vsock_cid")]
    pub vsock_cid: u32,
}

#[derive(Debug, Clone, Deserialize)]
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
    /// Workload kind threaded into the profiler agent context. `"synthetic"`
    /// drives stress-ng inside the guest; `"game"` runs the real Steam path.
    #[serde(default = "default_mode")]
    pub mode: String,
    /// Args forwarded to stress-ng when `mode = "synthetic"`. Ignored otherwise.
    #[serde(default = "default_benchmark_args")]
    pub benchmark_args: Vec<String>,
    /// Per-run duration for the synthetic benchmark, in seconds.
    #[serde(default = "default_benchmark_duration")]
    pub benchmark_duration_secs: u32,
    /// Native GPU benchmark driven when `mode = "game"`: `"vkmark"` or
    /// `"glmark2"`. The guest agent allow-lists these. Ignored otherwise.
    #[serde(default = "default_game_benchmark")]
    pub game_benchmark: String,
    /// Steam app id measured when `mode = "steam"` (milestone G3).
    /// Default 570 = Dota 2 (free license, native Linux, Vulkan).
    #[serde(default = "default_steam_app_id")]
    pub steam_app_id: u32,
    /// Launch arguments appended to `-applaunch <steam_app_id>` — the
    /// per-title benchmark invocation (e.g. Civ 6's
    /// `["-benchmark", "graphicsbenchmark"]`). Empty = no extra args.
    #[serde(default)]
    pub steam_launch_args: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AgentsConfig {
    #[serde(default = "default_model")]
    pub model: String,
    #[serde(default = "default_max_retries")]
    pub max_retries: u32,
    #[serde(default = "default_timeout")]
    pub timeout_secs: u64,
    /// Global per-call cap on Anthropic `max_tokens`. Per-agent overrides
    /// in `per_agent_max_tokens` win when present. Tuned to keep multi-agent
    /// cycles inside the org rate cap (30k input tokens/min as of 2026-05).
    #[serde(default = "default_max_tokens")]
    pub max_tokens: u32,
    /// Optional per-agent overrides keyed by the snake_case `AgentName`
    /// (e.g. "optimizer", "profiler"). Missing entries fall back to
    /// `max_tokens`.
    #[serde(default)]
    pub per_agent_max_tokens: HashMap<String, u32>,
    #[serde(default)]
    pub optimizer: OptimizerConfig,
    #[serde(default)]
    pub game_player: GamePlayerConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OptimizerConfig {
    #[serde(default = "default_max_attempts")]
    pub max_attempts_per_bottleneck: u32,
    #[serde(default = "default_allowed_layers")]
    pub allowed_layers: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct GamePlayerConfig {
    #[serde(default)]
    pub enabled: bool,
}

/// Benchmarks the guest agent's launch_benchmark RPC allow-lists. Kept in
/// sync with NATIVE_BENCHMARKS in guest/crucible_guest_agent.py.
const VALID_GAME_BENCHMARKS: &[&str] = &["vkmark", "glmark2"];

impl CrucibleConfig {
    pub fn from_file(path: &Path) -> Result<Self> {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read config: {}", path.display()))?;
        let config: Self = toml::from_str(&content)
            .with_context(|| format!("failed to parse config: {}", path.display()))?;
        config.validate()?;
        Ok(config)
    }

    /// Fail at startup on settings that would otherwise burn a full cycle
    /// before erroring inside the guest.
    pub fn validate(&self) -> Result<()> {
        if self.measurement.mode == "game"
            && !VALID_GAME_BENCHMARKS.contains(&self.measurement.game_benchmark.as_str())
        {
            anyhow::bail!(
                "[measurement] game_benchmark = {:?} is not supported (allowed: {})",
                self.measurement.game_benchmark,
                VALID_GAME_BENCHMARKS.join(", ")
            );
        }
        Ok(())
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
fn default_mode() -> String {
    "synthetic".to_string()
}
fn default_benchmark_args() -> Vec<String> {
    vec!["--cpu".to_string(), "2".to_string()]
}
fn default_benchmark_duration() -> u32 {
    30
}
fn default_game_benchmark() -> String {
    "vkmark".to_string()
}
fn default_steam_app_id() -> u32 {
    570
}
fn default_model() -> String {
    "claude-sonnet-5".to_string()
}
fn default_max_retries() -> u32 {
    3
}
fn default_timeout() -> u64 {
    // Sized for steam mode: the launch_steam_benchmark RPC blocks for
    // client settle + shader pre-processing + asset load + log window.
    1500
}
fn default_max_tokens() -> u32 {
    4096
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
            mode: default_mode(),
            benchmark_args: default_benchmark_args(),
            benchmark_duration_secs: default_benchmark_duration(),
            game_benchmark: default_game_benchmark(),
            steam_app_id: default_steam_app_id(),
            steam_launch_args: Vec::new(),
        }
    }
}

impl Default for AgentsConfig {
    fn default() -> Self {
        Self {
            model: default_model(),
            max_retries: default_max_retries(),
            timeout_secs: default_timeout(),
            max_tokens: default_max_tokens(),
            per_agent_max_tokens: HashMap::new(),
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
        assert_eq!(config.measurement.mode, "synthetic"); // default
        assert_eq!(config.measurement.benchmark_args, vec!["--cpu", "2"]); // default
        assert_eq!(config.measurement.benchmark_duration_secs, 30); // default
        assert_eq!(config.agents.model, "claude-sonnet-5"); // default
    }

    #[test]
    fn measurement_mode_can_be_overridden() {
        let toml_str = r#"
            [orchestrator]
            db_path = "/tmp/x.db"
            artifact_dir = "/tmp/x"
            [vm]
            kernel_src = "/tmp/k"
            guest_rootfs = "/tmp/r"
            vfio_device = "00:00.0"
            [measurement]
            mode = "game"
            benchmark_args = ["--vm", "1"]
            benchmark_duration_secs = 90
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.measurement.mode, "game");
        assert_eq!(config.measurement.benchmark_args, vec!["--vm", "1"]);
        assert_eq!(config.measurement.benchmark_duration_secs, 90);
        assert_eq!(config.measurement.game_benchmark, "vkmark"); // default
    }

    #[test]
    fn measurement_game_benchmark_can_be_overridden() {
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
            game_benchmark = "glmark2"
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.measurement.game_benchmark, "glmark2");
        assert!(config.validate().is_ok());
    }

    #[test]
    fn validate_rejects_unknown_game_benchmark_in_game_mode() {
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
            game_benchmark = "glmark2-drm"
            [agents]
        "#;
        let config: CrucibleConfig = toml::from_str(toml_str).unwrap();
        let err = config.validate().unwrap_err().to_string();
        assert!(err.contains("glmark2-drm"), "err: {err}");

        // Synthetic mode doesn't care about game_benchmark.
        let synthetic = toml_str.replace("mode = \"game\"", "mode = \"synthetic\"");
        let config: CrucibleConfig = toml::from_str(&synthetic).unwrap();
        assert!(config.validate().is_ok());
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
