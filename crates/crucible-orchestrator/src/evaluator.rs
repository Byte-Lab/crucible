use statrs::distribution::{ContinuousCDF, StudentsT};
use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    Accept,
    Marginal,
    Neutral,
    Regressed,
}

impl fmt::Display for Verdict {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            Verdict::Accept => "accept",
            Verdict::Marginal => "marginal",
            Verdict::Neutral => "neutral",
            Verdict::Regressed => "regressed",
        };
        write!(f, "{}", s)
    }
}

#[derive(Debug, Clone)]
pub struct EvalConfig {
    pub significance_threshold: f64,
    pub effect_size_threshold: f64,
}

#[derive(Debug, Clone)]
pub struct TTestResult {
    pub t_statistic: f64,
    pub degrees_of_freedom: f64,
    pub p_value: f64,
    pub significant: bool,
}

#[derive(Debug, Clone)]
pub struct MetricEvaluation {
    pub metric: String,
    pub baseline_mean: f64,
    pub comparison_mean: f64,
    pub delta_pct: f64,
    pub t_test: TTestResult,
    pub cohens_d: f64,
    pub verdict: Verdict,
}

fn mean(data: &[f64]) -> f64 {
    let n = data.len() as f64;
    data.iter().sum::<f64>() / n
}

fn variance(data: &[f64]) -> f64 {
    let m = mean(data);
    let n = data.len() as f64;
    data.iter().map(|x| (x - m).powi(2)).sum::<f64>() / (n - 1.0)
}

pub fn welch_t_test(a: &[f64], b: &[f64]) -> TTestResult {
    let mean_a = mean(a);
    let mean_b = mean(b);
    let var_a = variance(a);
    let var_b = variance(b);
    let n_a = a.len() as f64;
    let n_b = b.len() as f64;

    let se = (var_a / n_a + var_b / n_b).sqrt();
    let t_statistic = (mean_a - mean_b) / se;

    // Satterthwaite degrees of freedom
    let numerator = (var_a / n_a + var_b / n_b).powi(2);
    let denominator = (var_a / n_a).powi(2) / (n_a - 1.0)
        + (var_b / n_b).powi(2) / (n_b - 1.0);
    let df = numerator / denominator;

    let dist = StudentsT::new(0.0, 1.0, df).unwrap();
    let p_value = 2.0 * (1.0 - dist.cdf(t_statistic.abs()));

    TTestResult {
        t_statistic,
        degrees_of_freedom: df,
        p_value,
        significant: p_value < 0.05,
    }
}

pub fn cohens_d(a: &[f64], b: &[f64]) -> f64 {
    let mean_a = mean(a);
    let mean_b = mean(b);
    let var_a = variance(a);
    let var_b = variance(b);
    let n_a = a.len() as f64;
    let n_b = b.len() as f64;

    let pooled_sd = ((( n_a - 1.0) * var_a + (n_b - 1.0) * var_b) / (n_a + n_b - 2.0)).sqrt();
    (mean_a - mean_b) / pooled_sd
}

pub fn evaluate_metric(
    metric: &str,
    baseline: &[f64],
    comparison: &[f64],
    lower_is_better: bool,
    config: &EvalConfig,
) -> MetricEvaluation {
    let baseline_mean = mean(baseline);
    let comparison_mean = mean(comparison);
    let delta_pct = (comparison_mean - baseline_mean) / baseline_mean * 100.0;
    let t_test = welch_t_test(baseline, comparison);
    let d = cohens_d(baseline, comparison);

    let verdict = if t_test.p_value >= config.significance_threshold {
        Verdict::Neutral
    } else {
        // Determine if the change is in the right direction
        let improved = if lower_is_better {
            comparison_mean < baseline_mean
        } else {
            comparison_mean > baseline_mean
        };

        if !improved {
            Verdict::Regressed
        } else if d.abs() >= config.effect_size_threshold {
            Verdict::Accept
        } else {
            Verdict::Marginal
        }
    };

    MetricEvaluation {
        metric: metric.to_string(),
        baseline_mean,
        comparison_mean,
        delta_pct,
        t_test,
        cohens_d: d,
        verdict,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn welch_t_test_significant_difference() {
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![70.0, 71.0, 69.0, 70.5, 70.2];
        let result = welch_t_test(&baseline, &comparison);
        assert!(result.p_value < 0.05);
        assert!(result.significant);
    }

    #[test]
    fn welch_t_test_no_significant_difference() {
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![60.1, 60.8, 59.2, 60.6, 60.0];
        let result = welch_t_test(&baseline, &comparison);
        assert!(result.p_value > 0.05);
        assert!(!result.significant);
    }

    #[test]
    fn cohens_d_large_effect() {
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![70.0, 71.0, 69.0, 70.5, 70.2];
        let d = cohens_d(&baseline, &comparison);
        assert!(d.abs() > 0.8);
    }

    #[test]
    fn cohens_d_small_effect() {
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![60.5, 61.5, 59.5, 61.0, 60.7];
        let d = cohens_d(&baseline, &comparison);
        assert!(d.abs() < 0.8);
    }

    #[test]
    fn evaluate_accept() {
        let config = EvalConfig {
            significance_threshold: 0.05,
            effect_size_threshold: 0.5,
        };
        let baseline = vec![25.0, 26.0, 24.0, 25.5, 25.2];
        let comparison = vec![20.0, 21.0, 19.0, 20.5, 20.2];
        let result = evaluate_metric("frame_time_p99", &baseline, &comparison, true, &config);
        assert_eq!(result.verdict, Verdict::Accept);
    }

    #[test]
    fn evaluate_reject_regression() {
        let config = EvalConfig {
            significance_threshold: 0.05,
            effect_size_threshold: 0.5,
        };
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![50.0, 51.0, 49.0, 50.5, 50.2];
        let result = evaluate_metric("fps_avg", &baseline, &comparison, false, &config);
        assert_eq!(result.verdict, Verdict::Regressed);
    }

    #[test]
    fn evaluate_neutral_no_change() {
        let config = EvalConfig {
            significance_threshold: 0.05,
            effect_size_threshold: 0.5,
        };
        let baseline = vec![60.0, 61.0, 59.0, 60.5, 60.2];
        let comparison = vec![60.1, 60.8, 59.2, 60.6, 60.0];
        let result = evaluate_metric("fps_avg", &baseline, &comparison, false, &config);
        assert_eq!(result.verdict, Verdict::Neutral);
    }
}
