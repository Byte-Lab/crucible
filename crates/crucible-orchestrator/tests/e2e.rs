//! End-to-end smoke test for the synthetic loop.
//!
//! Skipped by default. Set `CRUCIBLE_E2E=1` to run, plus:
//!   - `ANTHROPIC_API_KEY` for the Claude-backed agents
//!   - `vng` on PATH (virtme-ng)
//!   - `CRUCIBLE_KERNEL_SRC` (default `/home/void/upstream/linux`) — checked-out
//!     kernel source tree usable by `KernelBuilder::build_kernel`
//!   - `CRUCIBLE_ROOTFS_PATH` (default `~/.crucible/rootfs`) — built by
//!     `scripts/setup-rootfs.sh` (must contain a `.crucible-built` stamp file)
//!
//! Each missing prerequisite produces one specific error before any
//! orchestrator code runs. A passing run leaves a row in `cycles` with a
//! terminal status, two rows in `measurements`, and at least one row in
//! `evaluations`.

use std::io::Write;
use std::path::{Path, PathBuf};

#[tokio::test]
async fn synthetic_cycle_writes_measurements_and_evaluation() {
    if std::env::var("CRUCIBLE_E2E").is_err() {
        eprintln!("e2e skipped: set CRUCIBLE_E2E=1 to run (requires vng, kernel src, rootfs)");
        return;
    }

    check_prerequisites().expect("e2e prerequisites unmet");

    let tmp_dir = tempfile::tempdir().unwrap();
    let db_path = tmp_dir.path().join("e2e.db");
    let artifact_dir = tmp_dir.path().join("artifacts");
    let kernel_src = std::env::var("CRUCIBLE_KERNEL_SRC")
        .unwrap_or_else(|_| "/home/void/upstream/linux".to_string());
    let rootfs = std::env::var("CRUCIBLE_ROOTFS_PATH")
        .unwrap_or_else(|_| format!("{}/.crucible/rootfs", std::env::var("HOME").unwrap()));

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
        memory = "4G"
        cpus = 4
        vfio_device = "00:00.0"
        boot_timeout_secs = 180
        vsock_cid = 3

        [measurement]
        mode = "synthetic"
        benchmark_args = ["--cpu", "2"]
        benchmark_duration_secs = 10
        runs_per_phase = 1
        warmup_runs = 0

        [agents]
        timeout_secs = 600
        "#,
        db = db_path.display(),
        art = artifact_dir.display(),
        kernel = kernel_src,
        rootfs = rootfs,
    )
    .unwrap();

    let config =
        crucible_orchestrator::config::CrucibleConfig::from_file(&config_path).unwrap();
    let db = crucible_orchestrator::db::Database::open(&db_path).unwrap();

    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let workspace_root = manifest_dir.parent().unwrap().parent().unwrap();
    let agents_dir = workspace_root.join("agents");

    let agent_runner = crucible_orchestrator::agent_runner::AgentRunner::new(
        PathBuf::from("python3"),
        agents_dir,
        std::time::Duration::from_secs(config.agents.timeout_secs),
    );

    let mut orchestrator = crucible_orchestrator::orchestrator::Orchestrator::new(
        config, db, agent_runner,
    );

    orchestrator
        .run_cycle()
        .await
        .expect("synthetic cycle should complete end-to-end");

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
}

fn check_prerequisites() -> Result<(), String> {
    if std::env::var("ANTHROPIC_API_KEY").is_err() {
        return Err("ANTHROPIC_API_KEY is not set".to_string());
    }
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
    let rootfs = std::env::var("CRUCIBLE_ROOTFS_PATH")
        .unwrap_or_else(|_| format!("{}/.crucible/rootfs", std::env::var("HOME").unwrap()));
    if !Path::new(&rootfs).join(".crucible-built").exists() {
        return Err(format!(
            "rootfs at {} missing .crucible-built stamp (run scripts/setup-rootfs.sh)",
            rootfs
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
