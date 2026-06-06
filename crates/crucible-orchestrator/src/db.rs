use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use std::path::Path;

const SCHEMA: &str = r#"
    CREATE TABLE IF NOT EXISTS cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_name TEXT NOT NULL,
        game_app_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'select_game',
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS measurements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL REFERENCES cycles(id),
        phase TEXT NOT NULL,
        fps_avg REAL NOT NULL,
        fps_p1 REAL NOT NULL,
        frame_time_p99_ms REAL NOT NULL,
        psi_cpu_avg REAL NOT NULL,
        psi_memory_avg REAL NOT NULL,
        custom_json TEXT NOT NULL DEFAULT '{}',
        recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS patches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL REFERENCES cycles(id),
        layer TEXT NOT NULL,
        diff_path TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now')),
        reverted_at TEXT
    );

    CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL REFERENCES cycles(id),
        metric TEXT NOT NULL,
        baseline_value REAL NOT NULL,
        comparison_value REAL NOT NULL,
        delta_pct REAL NOT NULL,
        verdict TEXT NOT NULL,
        evaluated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_measurements_cycle ON measurements(cycle_id, phase);
    CREATE INDEX IF NOT EXISTS idx_patches_cycle ON patches(cycle_id);
    CREATE INDEX IF NOT EXISTS idx_evaluations_cycle ON evaluations(cycle_id);
"#;

pub struct Database {
    conn: Connection,
}

#[derive(Debug)]
pub struct Cycle {
    pub id: i64,
    pub game_name: String,
    pub game_app_id: i64,
    pub status: String,
    pub started_at: String,
    pub completed_at: Option<String>,
}

#[derive(Debug)]
pub struct Measurement {
    pub id: i64,
    pub cycle_id: i64,
    pub phase: String,
    pub fps_avg: f64,
    pub fps_p1: f64,
    pub frame_time_p99_ms: f64,
    pub psi_cpu_avg: f64,
    pub psi_memory_avg: f64,
}

#[derive(Debug)]
pub struct Patch {
    pub id: i64,
    pub cycle_id: i64,
    pub layer: String,
    pub diff_path: String,
    pub applied_at: String,
    pub reverted_at: Option<String>,
}

#[derive(Debug)]
pub struct Evaluation {
    pub id: i64,
    pub cycle_id: i64,
    pub metric: String,
    pub baseline_value: f64,
    pub comparison_value: f64,
    pub delta_pct: f64,
    pub verdict: String,
}

impl Database {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("failed to create db directory: {}", parent.display()))?;
        }
        let conn = Connection::open(path)
            .with_context(|| format!("failed to open database: {}", path.display()))?;
        let db = Self { conn };
        db.migrate()?;
        Ok(db)
    }

    pub fn open_in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory()?;
        let db = Self { conn };
        db.migrate()?;
        Ok(db)
    }

    fn migrate(&self) -> Result<()> {
        self.conn
            .execute_batch(SCHEMA)
            .context("failed to run schema migration")
    }

    pub fn create_cycle(&self, game_name: &str, game_app_id: i64) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO cycles (game_name, game_app_id) VALUES (?1, ?2)",
            params![game_name, game_app_id],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_cycle(&self, id: i64) -> Result<Cycle> {
        self.conn
            .query_row(
                "SELECT id, game_name, game_app_id, status, started_at, completed_at \
                 FROM cycles WHERE id = ?1",
                params![id],
                |row| {
                    Ok(Cycle {
                        id: row.get(0)?,
                        game_name: row.get(1)?,
                        game_app_id: row.get(2)?,
                        status: row.get(3)?,
                        started_at: row.get(4)?,
                        completed_at: row.get(5)?,
                    })
                },
            )
            .context("failed to get cycle")
    }

    pub fn update_cycle_status(&self, id: i64, status: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE cycles SET status = ?1 WHERE id = ?2",
            params![status, id],
        )?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)] // mirrors the measurements row shape
    pub fn insert_measurement(
        &self,
        cycle_id: i64,
        phase: &str,
        fps_avg: f64,
        fps_p1: f64,
        frame_time_p99_ms: f64,
        psi_cpu_avg: f64,
        psi_memory_avg: f64,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO measurements \
             (cycle_id, phase, fps_avg, fps_p1, frame_time_p99_ms, psi_cpu_avg, psi_memory_avg) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![cycle_id, phase, fps_avg, fps_p1, frame_time_p99_ms, psi_cpu_avg, psi_memory_avg],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_measurements(&self, cycle_id: i64, phase: &str) -> Result<Vec<Measurement>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, cycle_id, phase, fps_avg, fps_p1, frame_time_p99_ms, \
             psi_cpu_avg, psi_memory_avg \
             FROM measurements WHERE cycle_id = ?1 AND phase = ?2 ORDER BY id",
        )?;
        let rows = stmt.query_map(params![cycle_id, phase], |row| {
            Ok(Measurement {
                id: row.get(0)?,
                cycle_id: row.get(1)?,
                phase: row.get(2)?,
                fps_avg: row.get(3)?,
                fps_p1: row.get(4)?,
                frame_time_p99_ms: row.get(5)?,
                psi_cpu_avg: row.get(6)?,
                psi_memory_avg: row.get(7)?,
            })
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .context("failed to collect measurements")
    }

    pub fn insert_patch(&self, cycle_id: i64, layer: &str, diff_path: &str) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO patches (cycle_id, layer, diff_path) VALUES (?1, ?2, ?3)",
            params![cycle_id, layer, diff_path],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_patch(&self, id: i64) -> Result<Patch> {
        self.conn
            .query_row(
                "SELECT id, cycle_id, layer, diff_path, applied_at, reverted_at \
                 FROM patches WHERE id = ?1",
                params![id],
                |row| {
                    Ok(Patch {
                        id: row.get(0)?,
                        cycle_id: row.get(1)?,
                        layer: row.get(2)?,
                        diff_path: row.get(3)?,
                        applied_at: row.get(4)?,
                        reverted_at: row.get(5)?,
                    })
                },
            )
            .context("failed to get patch")
    }

    pub fn list_patches_for_cycle(&self, cycle_id: i64) -> Result<Vec<Patch>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, cycle_id, layer, diff_path, applied_at, reverted_at \
             FROM patches WHERE cycle_id = ?1 ORDER BY id ASC",
        )?;
        let rows = stmt.query_map(params![cycle_id], |row| {
            Ok(Patch {
                id: row.get(0)?,
                cycle_id: row.get(1)?,
                layer: row.get(2)?,
                diff_path: row.get(3)?,
                applied_at: row.get(4)?,
                reverted_at: row.get(5)?,
            })
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .context("failed to collect patches")
    }

    pub fn mark_patch_reverted(&self, id: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE patches SET reverted_at = datetime('now') WHERE id = ?1",
            params![id],
        )?;
        Ok(())
    }

    pub fn insert_evaluation(
        &self,
        cycle_id: i64,
        metric: &str,
        baseline_value: f64,
        comparison_value: f64,
        delta_pct: f64,
        verdict: &str,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO evaluations \
             (cycle_id, metric, baseline_value, comparison_value, delta_pct, verdict) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![cycle_id, metric, baseline_value, comparison_value, delta_pct, verdict],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn get_evaluations(&self, cycle_id: i64) -> Result<Vec<Evaluation>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, cycle_id, metric, baseline_value, comparison_value, delta_pct, verdict \
             FROM evaluations WHERE cycle_id = ?1 ORDER BY id",
        )?;
        let rows = stmt.query_map(params![cycle_id], |row| {
            Ok(Evaluation {
                id: row.get(0)?,
                cycle_id: row.get(1)?,
                metric: row.get(2)?,
                baseline_value: row.get(3)?,
                comparison_value: row.get(4)?,
                delta_pct: row.get(5)?,
                verdict: row.get(6)?,
            })
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()
            .context("failed to collect evaluations")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_db() -> Database {
        Database::open_in_memory().unwrap()
    }

    #[test]
    fn create_and_get_cycle() {
        let db = test_db();
        let id = db.create_cycle("shadow_of_the_tomb_raider", 1091500).unwrap();
        let cycle = db.get_cycle(id).unwrap();
        assert_eq!(cycle.game_name, "shadow_of_the_tomb_raider");
        assert_eq!(cycle.game_app_id, 1091500);
        assert_eq!(cycle.status, "select_game");
    }

    #[test]
    fn update_cycle_status() {
        let db = test_db();
        let id = db.create_cycle("cyberpunk_2077", 1091500).unwrap();
        db.update_cycle_status(id, "baseline_measurement").unwrap();
        let cycle = db.get_cycle(id).unwrap();
        assert_eq!(cycle.status, "baseline_measurement");
    }

    #[test]
    fn insert_and_query_measurement() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        db.insert_measurement(cycle_id, "baseline", 60.0, 45.0, 25.0, 0.5, 1.2)
            .unwrap();
        db.insert_measurement(cycle_id, "baseline", 62.0, 47.0, 24.0, 0.4, 1.1)
            .unwrap();
        let measurements = db.get_measurements(cycle_id, "baseline").unwrap();
        assert_eq!(measurements.len(), 2);
        assert!((measurements[0].fps_avg - 60.0).abs() < f64::EPSILON);
        assert!((measurements[1].fps_avg - 62.0).abs() < f64::EPSILON);
    }

    #[test]
    fn insert_and_get_patch() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        let patch_id = db
            .insert_patch(cycle_id, "kernel", "/tmp/patches/001.diff")
            .unwrap();
        let patch = db.get_patch(patch_id).unwrap();
        assert_eq!(patch.layer, "kernel");
        assert_eq!(patch.diff_path, "/tmp/patches/001.diff");
        assert!(patch.reverted_at.is_none());
    }

    #[test]
    fn list_patches_for_cycle_returns_all_in_insertion_order() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        db.insert_patch(cycle_id, "kernel", "/tmp/patches/001.diff")
            .unwrap();
        db.insert_patch(cycle_id, "userspace", "/tmp/patches/002.diff")
            .unwrap();
        let other_cycle = db.create_cycle("other_game", 67890).unwrap();
        db.insert_patch(other_cycle, "tuning", "/tmp/patches/003.diff")
            .unwrap();
        let patches = db.list_patches_for_cycle(cycle_id).unwrap();
        assert_eq!(patches.len(), 2);
        assert_eq!(patches[0].diff_path, "/tmp/patches/001.diff");
        assert_eq!(patches[0].layer, "kernel");
        assert_eq!(patches[1].diff_path, "/tmp/patches/002.diff");
        assert_eq!(patches[1].layer, "userspace");
    }

    #[test]
    fn list_patches_for_cycle_empty_for_unknown_cycle() {
        let db = test_db();
        let patches = db.list_patches_for_cycle(999).unwrap();
        assert!(patches.is_empty());
    }

    #[test]
    fn mark_patch_reverted() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        let patch_id = db
            .insert_patch(cycle_id, "kernel", "/tmp/patches/001.diff")
            .unwrap();
        db.mark_patch_reverted(patch_id).unwrap();
        let patch = db.get_patch(patch_id).unwrap();
        assert!(patch.reverted_at.is_some());
    }

    #[test]
    fn insert_evaluation() {
        let db = test_db();
        let cycle_id = db.create_cycle("test_game", 12345).unwrap();
        db.insert_evaluation(cycle_id, "frame_time_p99", 25.0, 22.0, -12.0, "accept")
            .unwrap();
        let evals = db.get_evaluations(cycle_id).unwrap();
        assert_eq!(evals.len(), 1);
        assert_eq!(evals[0].metric, "frame_time_p99");
        assert_eq!(evals[0].verdict, "accept");
    }
}
