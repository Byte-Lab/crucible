use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentName {
    GameSelector,
    GamePlayer,
    Profiler,
    Analyzer,
    Optimizer,
    Echo,
}

impl AgentName {
    pub fn as_str(&self) -> &'static str {
        match self {
            AgentName::GameSelector => "game_selector",
            AgentName::GamePlayer => "game_player",
            AgentName::Profiler => "profiler",
            AgentName::Analyzer => "analyzer",
            AgentName::Optimizer => "optimizer",
            AgentName::Echo => "echo",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentConfig {
    pub model: String,
    /// Advisory under the Claude Agent SDK — the bundled `claude` CLI has no
    /// per-call output-token cap. Tool loops are bounded by
    /// `ClaudeAgentBase.MAX_TOOL_ROUNDS` (Python side) and the orchestrator's
    /// subprocess timeout.
    pub max_tokens: u32,
    pub timeout_seconds: u64,
    /// Forwarded to the bundled `claude` CLI via `CLAUDE_CODE_MAX_RETRIES`.
    #[serde(default = "default_max_retries")]
    pub max_retries: u32,
}

fn default_max_retries() -> u32 {
    3
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskEnvelope {
    pub task_id: Uuid,
    pub agent: AgentName,
    pub context: serde_json::Value,
    pub config: AgentConfig,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Success,
    Failure,
    NeedsInput,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiUsage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub api_calls: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResultEnvelope {
    pub task_id: Uuid,
    pub status: TaskStatus,
    pub result: serde_json::Value,
    pub usage: ApiUsage,
    pub logs: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum GuestCommand {
    HealthCheck,
    SetupCgroups {
        groups: Vec<String>,
    },
    LaunchGame {
        app_id: u64,
        args: Vec<String>,
    },
    StopGame,
    StartProfiling {
        config: serde_json::Value,
    },
    StopProfiling,
    CaptureScreen,
    InjectInput {
        events: Vec<InputEvent>,
    },
    FetchFile {
        path: String,
    },
    GetMetrics,
    RunBenchmark {
        name: String,
        args: Vec<String>,
        duration_secs: u32,
    },
    /// Run a native GPU benchmark (vkmark/glmark2) under MangoHud, writing
    /// the frame-time CSV to `mangohud_output` inside the guest. The host
    /// profiler retrieves it afterwards via `FetchFile`.
    LaunchBenchmark {
        name: String,
        args: Vec<String>,
        mangohud_output: String,
        /// Expected benchmark runtime; the guest derives MangoHud's finite
        /// log window from it (log must stop before the app exits or the
        /// CSV is never flushed).
        duration_secs: u32,
    },
    /// Run a Steam title under weston-headless + MangoHud (milestone G3).
    /// `args` are extra launch options (e.g. Source 2 timedemo flags);
    /// frame data flows through the same mangohud_output → FetchFile path
    /// as LaunchBenchmark.
    LaunchSteamBenchmark {
        app_id: u32,
        args: Vec<String>,
        mangohud_output: String,
        duration_secs: u32,
    },
    /// Apply optimizer-proposed sysctl tunings in the guest before the
    /// comparison run. `config` = {"sysctls": {"kernel.x": "value", ...}};
    /// the guest reports applied/failed per key without failing the cycle.
    ApplySysctls {
        config: serde_json::Value,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputEvent {
    pub event_type: String,
    pub code: String,
    pub value: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum GuestResponse {
    Ok {
        data: serde_json::Value,
    },
    Error {
        message: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_envelope_roundtrip() {
        let task = TaskEnvelope {
            task_id: uuid::Uuid::new_v4(),
            agent: AgentName::Analyzer,
            context: serde_json::json!({"game_id": 1091500}),
            config: AgentConfig {
                model: "claude-sonnet-4-20250514".to_string(),
                max_tokens: 8192,
                timeout_seconds: 300,
                max_retries: 3,
            },
        };
        let json = serde_json::to_string(&task).unwrap();
        let parsed: TaskEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.task_id, task.task_id);
        assert_eq!(parsed.agent, AgentName::Analyzer);
        assert_eq!(parsed.config.max_tokens, 8192);
    }

    #[test]
    fn result_envelope_success() {
        let result = ResultEnvelope {
            task_id: uuid::Uuid::new_v4(),
            status: TaskStatus::Success,
            result: serde_json::json!({"bottleneck": "kcompactd"}),
            usage: ApiUsage {
                input_tokens: 1234,
                output_tokens: 567,
                api_calls: 3,
            },
            logs: vec!["analyzed trace".to_string()],
        };
        let json = serde_json::to_string(&result).unwrap();
        let parsed: ResultEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.status, TaskStatus::Success);
        assert_eq!(parsed.usage.api_calls, 3);
    }

    #[test]
    fn agent_name_serializes_as_snake_case() {
        let name = AgentName::GameSelector;
        let json = serde_json::to_value(name).unwrap();
        assert_eq!(json, serde_json::json!("game_selector"));
    }

    #[test]
    fn guest_command_health_check_serializes() {
        let cmd = GuestCommand::HealthCheck;
        let json = serde_json::to_value(&cmd).unwrap();
        assert_eq!(json["cmd"], "health_check");
    }

    #[test]
    fn guest_command_launch_game_roundtrip() {
        let cmd = GuestCommand::LaunchGame {
            app_id: 1091500,
            args: vec!["--benchmark".to_string()],
        };
        let json = serde_json::to_string(&cmd).unwrap();
        let parsed: GuestCommand = serde_json::from_str(&json).unwrap();
        if let GuestCommand::LaunchGame { app_id, args } = parsed {
            assert_eq!(app_id, 1091500);
            assert_eq!(args, vec!["--benchmark"]);
        } else {
            panic!("wrong variant");
        }
    }

    #[test]
    fn guest_command_run_benchmark_roundtrip() {
        let cmd = GuestCommand::RunBenchmark {
            name: "stress-ng".to_string(),
            args: vec!["--cpu".to_string(), "4".to_string()],
            duration_secs: 30,
        };
        let json = serde_json::to_value(&cmd).unwrap();
        assert_eq!(json["cmd"], "run_benchmark");
        assert_eq!(json["name"], "stress-ng");
        assert_eq!(json["duration_secs"], 30);
        let parsed: GuestCommand = serde_json::from_value(json).unwrap();
        if let GuestCommand::RunBenchmark {
            name,
            args,
            duration_secs,
        } = parsed
        {
            assert_eq!(name, "stress-ng");
            assert_eq!(args, vec!["--cpu", "4"]);
            assert_eq!(duration_secs, 30);
        } else {
            panic!("wrong variant");
        }
    }

    #[test]
    fn guest_command_launch_benchmark_roundtrip() {
        let cmd = GuestCommand::LaunchBenchmark {
            name: "vkmark".to_string(),
            args: vec!["--size".to_string(), "1920x1080".to_string()],
            mangohud_output: "/tmp/crucible_mangohud.csv".to_string(),
            duration_secs: 15,
        };
        let json = serde_json::to_value(&cmd).unwrap();
        assert_eq!(json["cmd"], "launch_benchmark");
        assert_eq!(json["name"], "vkmark");
        assert_eq!(json["mangohud_output"], "/tmp/crucible_mangohud.csv");
        assert_eq!(json["duration_secs"], 15);
        let parsed: GuestCommand = serde_json::from_value(json).unwrap();
        if let GuestCommand::LaunchBenchmark {
            name,
            args,
            mangohud_output,
            duration_secs,
        } = parsed
        {
            assert_eq!(name, "vkmark");
            assert_eq!(args, vec!["--size", "1920x1080"]);
            assert_eq!(mangohud_output, "/tmp/crucible_mangohud.csv");
            assert_eq!(duration_secs, 15);
        } else {
            panic!("wrong variant");
        }
    }

    #[test]
    fn guest_command_launch_steam_benchmark_roundtrip() {
        let cmd = GuestCommand::LaunchSteamBenchmark {
            app_id: 570,
            args: vec!["+timedemo".to_string(), "bench".to_string()],
            mangohud_output: "/tmp/crucible_mangohud.csv".to_string(),
            duration_secs: 90,
        };
        let json = serde_json::to_value(&cmd).unwrap();
        assert_eq!(json["cmd"], "launch_steam_benchmark");
        assert_eq!(json["app_id"], 570);
        assert_eq!(json["duration_secs"], 90);
        let parsed: GuestCommand = serde_json::from_value(json).unwrap();
        if let GuestCommand::LaunchSteamBenchmark { app_id, args, .. } = parsed {
            assert_eq!(app_id, 570);
            assert_eq!(args, vec!["+timedemo", "bench"]);
        } else {
            panic!("wrong variant");
        }
    }

    #[test]
    fn guest_command_apply_sysctls_roundtrip() {
        // Wire contract with guest/crucible_guest_agent.py
        // `_handle_apply_sysctls`: {"cmd": "apply_sysctls", "config":
        // {"sysctls": {...}}} — dotted keys, string values.
        let cmd = GuestCommand::ApplySysctls {
            config: serde_json::json!({
                "sysctls": {"kernel.sched_base_slice_ns": "1500000"}
            }),
        };
        let json = serde_json::to_value(&cmd).unwrap();
        assert_eq!(json["cmd"], "apply_sysctls");
        assert_eq!(
            json["config"]["sysctls"]["kernel.sched_base_slice_ns"],
            "1500000"
        );
        let parsed: GuestCommand = serde_json::from_value(json).unwrap();
        assert!(matches!(parsed, GuestCommand::ApplySysctls { .. }));
    }

    #[test]
    fn guest_response_fetch_file_carries_contents() {
        // Wire contract for fetch_file (guest/crucible_guest_agent.py
        // `_handle_fetch_file`): the response data must carry the file bytes
        // as base64, the true on-disk size, and a truncation flag — not just
        // the size. The profiler's game-mode tools depend on this to pull
        // MangoHud CSVs out of the guest.
        let raw = r#"{"status":"ok","data":{"path":"/tmp/mh.csv","size":24,"truncated":false,"contents_b64":"ZnJhbWV0aW1lLGZwcwoxNi42LDYwLjIK"}}"#;
        let parsed: GuestResponse = serde_json::from_str(raw).unwrap();
        if let GuestResponse::Ok { data } = parsed {
            assert_eq!(data["size"], 24);
            assert_eq!(data["truncated"], false);
            assert!(data["contents_b64"].is_string());
        } else {
            panic!("wrong variant");
        }
    }

    #[test]
    fn guest_response_ok_roundtrip() {
        let resp = GuestResponse::Ok {
            data: serde_json::json!({"pid": 4521, "cgroup": "crucible/game"}),
        };
        let json = serde_json::to_string(&resp).unwrap();
        let parsed: GuestResponse = serde_json::from_str(&json).unwrap();
        if let GuestResponse::Ok { data } = parsed {
            assert_eq!(data["pid"], 4521);
        } else {
            panic!("wrong variant");
        }
    }
}
