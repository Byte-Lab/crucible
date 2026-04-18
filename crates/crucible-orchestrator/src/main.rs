use clap::Parser;
use std::path::PathBuf;

use crucible_orchestrator::config;
use crucible_orchestrator::db;

#[derive(Parser)]
#[command(name = "crucible-orchestrator")]
#[command(about = "Agentic Linux gaming performance optimization")]
struct Cli {
    /// Path to configuration file
    #[arg(short, long, default_value = "config/crucible.toml")]
    config: PathBuf,
}

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "crucible_orchestrator=info".into()),
        )
        .init();

    let cli = Cli::parse();
    let config = config::CrucibleConfig::from_file(&cli.config)?;
    tracing::info!(db = %config.orchestrator.db_path, "loaded configuration");

    let _db = db::Database::open(std::path::Path::new(&config.orchestrator.db_path))?;
    tracing::info!("database initialized");

    tracing::info!("crucible orchestrator ready");

    Ok(())
}
