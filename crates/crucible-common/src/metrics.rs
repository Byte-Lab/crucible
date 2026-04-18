use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FrameMetrics {
    pub fps_avg: f64,
    pub fps_p1: f64,
    pub frame_time_p50_ms: f64,
    pub frame_time_p95_ms: f64,
    pub frame_time_p99_ms: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PsiMetrics {
    /// PSI avg10 value (percentage of time stalled)
    pub cpu_avg: f64,
    pub memory_avg: f64,
    pub io_avg: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CgroupPsiMetrics {
    pub cgroup_path: String,
    pub psi: PsiMetrics,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemMetrics {
    pub context_switches_per_sec: f64,
    pub page_faults_per_sec: f64,
    pub gpu_utilization_pct: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunMeasurement {
    pub frame: FrameMetrics,
    pub system_psi: PsiMetrics,
    pub cgroup_psi: Vec<CgroupPsiMetrics>,
    pub system: SystemMetrics,
    pub custom: serde_json::Value,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_measurement_roundtrip() {
        let m = RunMeasurement {
            frame: FrameMetrics {
                fps_avg: 60.0,
                fps_p1: 45.0,
                frame_time_p50_ms: 16.6,
                frame_time_p95_ms: 20.0,
                frame_time_p99_ms: 25.0,
            },
            system_psi: PsiMetrics {
                cpu_avg: 0.5,
                memory_avg: 1.2,
                io_avg: 0.1,
            },
            cgroup_psi: vec![CgroupPsiMetrics {
                cgroup_path: "crucible/game".to_string(),
                psi: PsiMetrics {
                    cpu_avg: 2.0,
                    memory_avg: 3.5,
                    io_avg: 0.0,
                },
            }],
            system: SystemMetrics {
                context_switches_per_sec: 5000.0,
                page_faults_per_sec: 120.0,
                gpu_utilization_pct: 85.0,
            },
            custom: serde_json::json!({}),
        };
        let json = serde_json::to_string(&m).unwrap();
        let parsed: RunMeasurement = serde_json::from_str(&json).unwrap();
        assert!((parsed.frame.fps_avg - 60.0).abs() < f64::EPSILON);
        assert_eq!(parsed.cgroup_psi.len(), 1);
        assert_eq!(parsed.cgroup_psi[0].cgroup_path, "crucible/game");
    }
}
