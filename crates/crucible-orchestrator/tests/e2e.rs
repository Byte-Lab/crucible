//! End-to-end smoke tests for the synthetic and game-mode loops.
//!
//! Skipped by default. Set `CRUCIBLE_E2E=1` to run the synthetic loop, or
//! `CRUCIBLE_E2E_GPU=1` for the game-mode loop. Both need:
//!   - the bundled `claude` CLI must have been logged in via `claude /login`
//!     once on this machine; Claude-backed agents now bill against the user's
//!     Pro/Max plan via `claude-agent-sdk` (no `ANTHROPIC_API_KEY` is read)
//!   - `vng` on PATH (virtme-ng)
//!   - `CRUCIBLE_KERNEL_SRC` (default `/home/void/upstream/linux`) — checked-out
//!     kernel source tree usable by `KernelBuilder::build_kernel`
//!
//! The synthetic loop also needs `CRUCIBLE_ROOTFS_PATH` (default
//! `~/.crucible/rootfs`, built by `scripts/setup-rootfs.sh`, `.crucible-built`
//! stamp). The game loop needs `CRUCIBLE_GAME_ROOTFS_PATH` (default
//! `~/.crucible/game-rootfs`, built by `scripts/setup-game-rootfs.sh`,
//! `.crucible-game-built` stamp) and optionally `CRUCIBLE_VFIO_DEVICE` for
//! real GPU passthrough — without it Mesa's lavapipe renders vkmark in
//! software, which still exercises the whole MangoHud → fetch_file →
//! metrics path.
//!
//! Each missing prerequisite produces one specific error before any
//! orchestrator code runs. A passing run leaves a row in `cycles` with a
//! terminal status, two rows in `measurements`, and at least one row in
//! `evaluations`.

use std::io::Write;
use std::path::{Path, PathBuf};

struct CycleOutcome {
    db_path: PathBuf,
    artifact_dir: PathBuf,
    // Keeps the tempdir (and thus db/artifacts) alive until dropped.
    _tmp_dir: tempfile::TempDir,
}

/// Drive one full cycle with the given `[measurement]` body and rootfs,
/// then verify the durable rows and scan agent stderr for tool leaks.
async fn run_cycle_and_verify(measurement_toml: &str, rootfs: &str, vfio_device: &str) -> CycleOutcome {
    let mut tmp_dir = tempfile::tempdir().unwrap();
    // Postmortems on a failed cycle need the agent stderr and the DB; the
    // TempDir guard deletes them with the panic unwinding past it.
    if std::env::var("CRUCIBLE_E2E_KEEP_ARTIFACTS").is_ok() {
        tmp_dir.disable_cleanup(true);
        // println: stdout survives without --nocapture on failure output.
        println!("e2e: artifacts kept at {}", tmp_dir.path().display());
    }
    let db_path = tmp_dir.path().join("e2e.db");
    let artifact_dir = tmp_dir.path().join("artifacts");
    let kernel_src = std::env::var("CRUCIBLE_KERNEL_SRC")
        .unwrap_or_else(|_| "/home/void/upstream/linux".to_string());
    let guest_payload = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("guest")
        .to_string_lossy()
        .to_string();

    let config_path = tmp_dir.path().join("e2e-config.toml");
    let mut f = std::fs::File::create(&config_path).unwrap();
    write!(
        f,
        r#"
        [orchestrator]
        db_path = "{db}"
        artifact_dir = "{art}"
        max_cycles = 1
        cycle_cooldown_secs = 0

        [vm]
        kernel_src = "{kernel}"
        guest_rootfs = "{rootfs}"
        guest_payload = "{guest_payload}"
        memory = "4G"
        cpus = 4
        vfio_device = "{vfio_device}"
        boot_timeout_secs = 180
        vsock_cid = 3

        [measurement]
        {measurement}

        [agents]
        timeout_secs = 600
        "#,
        db = db_path.display(),
        art = artifact_dir.display(),
        kernel = kernel_src,
        rootfs = rootfs,
        guest_payload = guest_payload,
        vfio_device = vfio_device,
        measurement = measurement_toml,
    )
    .unwrap();

    let config =
        crucible_orchestrator::config::CrucibleConfig::from_file(&config_path).unwrap();
    let db = crucible_orchestrator::db::Database::open(&db_path).unwrap();

    let agents_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("agents");

    let agent_runner = crucible_orchestrator::agent_runner::AgentRunner::new(
        PathBuf::from("python3"),
        agents_dir,
        std::time::Duration::from_secs(config.agents.timeout_secs),
        artifact_dir.clone(),
    );

    let mut orchestrator = crucible_orchestrator::orchestrator::Orchestrator::new(
        config, db, agent_runner,
    );

    orchestrator
        .run_cycle()
        .await
        .expect("cycle should complete end-to-end");

    // Reopen the DB to verify durable rows rather than peeking at the
    // orchestrator's in-memory handle. With a fresh DB this is the first
    // and only cycle, so its id is 1.
    let db = crucible_orchestrator::db::Database::open(&db_path).unwrap();
    let cycle = db
        .get_cycle(1)
        .expect("cycle row 1 should exist after one run");
    let terminal = ["accept", "marginal", "neutral", "reject", "idle"];
    assert!(
        terminal.contains(&cycle.status.as_str()),
        "cycle status {} not terminal",
        cycle.status
    );

    let baselines = db.get_measurements(cycle.id, "baseline").unwrap();
    let comparisons = db.get_measurements(cycle.id, "comparison").unwrap();
    assert!(!baselines.is_empty(), "no baseline measurement persisted");
    assert!(!comparisons.is_empty(), "no comparison measurement persisted");

    let evals = db.get_evaluations(cycle.id).unwrap();
    assert!(!evals.is_empty(), "no evaluation rows persisted");

    // Iterate-path breadcrumb. With runs_per_phase=1 the Welch's t-test
    // degenerates and most runs land Neutral, recording 0-1 patches. A
    // Marginal verdict (or >1 patch row) proves the Evaluate → Iterate →
    // Analyze loop fired this run.
    let patches = db.list_patches_for_cycle(cycle.id).unwrap();
    eprintln!(
        "e2e: cycle {} terminal={} patches={} evals={}",
        cycle.id,
        cycle.status,
        patches.len(),
        evals.len(),
    );
    if cycle.status == "marginal" || patches.len() > 1 {
        assert!(
            !patches.is_empty(),
            "marginal/iterated cycle must record at least one patch"
        );
    }

    scan_for_tool_leaks(&artifact_dir);

    CycleOutcome {
        db_path,
        artifact_dir,
        _tmp_dir: tmp_dir,
    }
}

/// Tool-leak verification. Every agent's stderr is teed to
/// <artifact_dir>/agents/<task_id>.stderr. Each `tool_call:` line emitted
/// by claude_agent.py must reference a tool prefixed with
/// `mcp__crucible__`; anything else means a built-in tool slipped past
/// `_BUILTIN_TOOLS_TO_DISALLOW` and is potentially being billed against
/// the user's plan. Skip silently if the dir doesn't exist (echo-only
/// cycles wouldn't produce one).
fn scan_for_tool_leaks(artifact_dir: &Path) {
    let agents_artifact_dir = artifact_dir.join("agents");
    let mut scanned = 0usize;
    let mut leaks: Vec<String> = Vec::new();
    if agents_artifact_dir.is_dir() {
        for entry in std::fs::read_dir(&agents_artifact_dir).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) != Some("stderr") {
                continue;
            }
            scanned += 1;
            let contents = std::fs::read_to_string(&path).unwrap();
            for (lineno, line) in contents.lines().enumerate() {
                let Some(rest) = line.strip_prefix("tool_call: ") else {
                    continue;
                };
                if !rest.starts_with("mcp__crucible__") {
                    leaks.push(format!(
                        "{}:{}: {}",
                        path.display(),
                        lineno + 1,
                        line
                    ));
                }
            }
        }
    }
    eprintln!(
        "e2e: tool-leak scan agents_dir={} files={} leaks={}",
        agents_artifact_dir.display(),
        scanned,
        leaks.len(),
    );
    assert!(
        leaks.is_empty(),
        "non-crucible tool calls leaked past _BUILTIN_TOOLS_TO_DISALLOW:\n{}",
        leaks.join("\n")
    );
}

#[tokio::test]
async fn synthetic_cycle_writes_measurements_and_evaluation() {
    if std::env::var("CRUCIBLE_E2E").is_err() {
        eprintln!("e2e skipped: set CRUCIBLE_E2E=1 to run (requires vng, kernel src, rootfs)");
        return;
    }

    check_common_prerequisites().expect("e2e prerequisites unmet");
    let rootfs = std::env::var("CRUCIBLE_ROOTFS_PATH")
        .unwrap_or_else(|_| format!("{}/.crucible/rootfs", std::env::var("HOME").unwrap()));
    assert!(
        Path::new(&rootfs).join(".crucible-built").exists(),
        "rootfs at {} missing .crucible-built stamp (run scripts/setup-rootfs.sh)",
        rootfs
    );

    run_cycle_and_verify(
        r#"mode = "synthetic"
        benchmark_args = ["--cpu", "2"]
        benchmark_duration_secs = 10
        runs_per_phase = 1
        warmup_runs = 0"#,
        &rootfs,
        "",
    )
    .await;
}

#[tokio::test]
async fn gpu_game_cycle_produces_nonzero_fps() {
    if std::env::var("CRUCIBLE_E2E_GPU").is_err() {
        eprintln!(
            "e2e-gpu skipped: set CRUCIBLE_E2E_GPU=1 to run (requires vng, kernel src, game rootfs)"
        );
        return;
    }

    check_common_prerequisites().expect("e2e-gpu prerequisites unmet");
    let rootfs = std::env::var("CRUCIBLE_GAME_ROOTFS_PATH").unwrap_or_else(|_| {
        format!("{}/.crucible/game-rootfs", std::env::var("HOME").unwrap())
    });
    assert!(
        Path::new(&rootfs).join(".crucible-game-built").exists(),
        "game rootfs at {} missing .crucible-game-built stamp (run scripts/setup-game-rootfs.sh)",
        rootfs
    );
    // Optional real passthrough; empty string renders via lavapipe.
    let vfio_device = std::env::var("CRUCIBLE_VFIO_DEVICE").unwrap_or_default();

    let outcome = run_cycle_and_verify(
        r#"mode = "game"
        game_benchmark = "vkmark"
        runs_per_phase = 1
        warmup_runs = 0"#,
        &rootfs,
        &vfio_device,
    )
    .await;

    // The discriminator against the synthetic path: synthetic runs emit
    // fps_avg = 0, so non-zero fps proves real frames flowed MangoHud →
    // fetch_file → parse_mangohud_csv → persist_measurement.
    let db = crucible_orchestrator::db::Database::open(&outcome.db_path).unwrap();
    for phase in ["baseline", "comparison"] {
        let rows = db.get_measurements(1, phase).unwrap();
        assert!(
            rows.iter().any(|m| m.fps_avg > 0.0),
            "{phase} measurements all have fps_avg == 0 — MangoHud frame data \
             did not flow (artifacts at {})",
            outcome.artifact_dir.display()
        );
    }
}

#[tokio::test]
async fn steam_cycle_produces_nonzero_fps() {
    if std::env::var("CRUCIBLE_E2E_GAME").is_err() {
        eprintln!(
            "e2e-game skipped: set CRUCIBLE_E2E_GAME=1 to run (requires vng, kernel src, \
             steam rootfs with a seeded login + game, bound VFIO GPU)"
        );
        return;
    }

    check_common_prerequisites().expect("e2e-game prerequisites unmet");
    let rootfs = std::env::var("CRUCIBLE_STEAM_ROOTFS_PATH").unwrap_or_else(|_| {
        format!("{}/.crucible/steam-rootfs", std::env::var("HOME").unwrap())
    });
    assert!(
        Path::new(&rootfs).join(".crucible-steam-built").exists(),
        "steam rootfs at {} missing .crucible-steam-built stamp (run scripts/setup-steam-rootfs.sh)",
        rootfs
    );
    // A real GPU is required: Steam titles won't produce meaningful frames
    // on llvmpipe, and the weston GL renderer wants real EGL.
    let vfio_device = std::env::var("CRUCIBLE_VFIO_DEVICE")
        .expect("CRUCIBLE_E2E_GAME requires CRUCIBLE_VFIO_DEVICE (bound via setup-host.sh)");

    let outcome = run_cycle_and_verify(
        r#"mode = "steam"
        steam_app_id = 570
        benchmark_duration_secs = 60
        runs_per_phase = 1
        warmup_runs = 0"#,
        &rootfs,
        &vfio_device,
    )
    .await;

    let db = crucible_orchestrator::db::Database::open(&outcome.db_path).unwrap();
    for phase in ["baseline", "comparison"] {
        let rows = db.get_measurements(1, phase).unwrap();
        assert!(
            rows.iter().any(|m| m.fps_avg > 0.0),
            "{phase} measurements all have fps_avg == 0 — Steam frame data \
             did not flow (artifacts at {})",
            outcome.artifact_dir.display()
        );
    }
}

fn check_common_prerequisites() -> Result<(), String> {
    if which("vng").is_none() {
        return Err("vng (virtme-ng) is not on PATH".to_string());
    }
    let kernel_src = std::env::var("CRUCIBLE_KERNEL_SRC")
        .unwrap_or_else(|_| "/home/void/upstream/linux".to_string());
    if !Path::new(&kernel_src).join(".git").exists() {
        return Err(format!(
            "CRUCIBLE_KERNEL_SRC ({}) is not a git checkout",
            kernel_src
        ));
    }
    Ok(())
}

fn which(cmd: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let full = dir.join(cmd);
        if full.is_file() {
            return Some(full);
        }
    }
    None
}
