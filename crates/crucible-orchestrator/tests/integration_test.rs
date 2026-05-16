use std::io::Write;
use std::path::PathBuf;

#[tokio::test]
async fn full_round_trip_echo_agent_to_db() {
    // 1. Create a temp config
    let tmp_dir = tempfile::tempdir().unwrap();
    let db_path = tmp_dir.path().join("test.db");
    let config_path = tmp_dir.path().join("config.toml");

    let mut config_file = std::fs::File::create(&config_path).unwrap();
    write!(
        config_file,
        r#"
        [orchestrator]
        db_path = "{}"
        artifact_dir = "{}"

        [vm]
        kernel_src = "/tmp/linux"
        guest_rootfs = "/tmp/rootfs"
        vfio_device = "03:00.0"

        [measurement]

        [agents]
        "#,
        db_path.display(),
        tmp_dir.path().join("artifacts").display(),
    )
    .unwrap();

    // 2. Load config
    let config =
        crucible_orchestrator::config::CrucibleConfig::from_file(&config_path).unwrap();

    // 3. Open database
    let db = crucible_orchestrator::db::Database::open(&db_path).unwrap();
    let cycle_id = db.create_cycle("test_game", 12345).unwrap();

    // 4. Run echo agent
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let workspace_root = manifest_dir.parent().unwrap().parent().unwrap();

    let runner = crucible_orchestrator::agent_runner::AgentRunner::new(
        PathBuf::from("python3"),
        workspace_root.join("agents"),
        std::time::Duration::from_secs(10),
        std::env::temp_dir(),
    );

    let task = crucible_common::protocol::TaskEnvelope {
        task_id: uuid::Uuid::new_v4(),
        agent: crucible_common::protocol::AgentName::Echo,
        context: serde_json::json!({"game": "test_game", "cycle_id": cycle_id}),
        config: crucible_common::protocol::AgentConfig {
            model: config.agents.model.clone(),
            max_tokens: 100,
            timeout_seconds: config.agents.timeout_secs,
            max_retries: config.agents.max_retries,
        },
    };

    let result = runner.run_agent(task).await.unwrap();
    assert_eq!(
        result.status,
        crucible_common::protocol::TaskStatus::Success
    );
    assert_eq!(result.result["echo"]["game"], "test_game");

    // 5. Store a measurement as if the agent had produced one
    db.insert_measurement(cycle_id, "baseline", 60.0, 45.0, 25.0, 0.5, 1.2)
        .unwrap();
    db.update_cycle_status(cycle_id, "baseline_measurement")
        .unwrap();

    let cycle = db.get_cycle(cycle_id).unwrap();
    assert_eq!(cycle.status, "baseline_measurement");
    let measurements = db.get_measurements(cycle_id, "baseline").unwrap();
    assert_eq!(measurements.len(), 1);
}
