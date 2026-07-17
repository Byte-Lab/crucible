// SPDX-License-Identifier: GPL-2.0-only
// Copyright (C) 2026 David Vernet

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
    let artifact_dir = PathBuf::from(&config.orchestrator.artifact_dir);
    let agent_runner = crucible_orchestrator::agent_runner::AgentRunner::new(
        PathBuf::from("python3"),
        agents_dir,
        std::time::Duration::from_secs(config.agents.timeout_secs),
        artifact_dir,
    );

    let max_cycles = if cli.single_cycle { 1 } else { cli.max_cycles };

    let mut orchestrator = crucible_orchestrator::orchestrator::Orchestrator::new(
        config, db, agent_runner,
    );

    tracing::info!("crucible orchestrator starting");
    orchestrator.run_loop(max_cycles).await?;

    Ok(())
}
