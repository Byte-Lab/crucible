use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CycleState {
    Idle,
    SelectGame,
    ProvisionVm,
    BaselineMeasurement,
    Analyze,
    GenerateOptimization,
    ApplyChanges,
    ComparisonMeasurement,
    Evaluate,
    Accept,
    Reject,
    Iterate,
}

impl CycleState {
    pub fn as_str(&self) -> &'static str {
        match self {
            CycleState::Idle => "idle",
            CycleState::SelectGame => "select_game",
            CycleState::ProvisionVm => "provision_vm",
            CycleState::BaselineMeasurement => "baseline_measurement",
            CycleState::Analyze => "analyze",
            CycleState::GenerateOptimization => "generate_optimization",
            CycleState::ApplyChanges => "apply_changes",
            CycleState::ComparisonMeasurement => "comparison_measurement",
            CycleState::Evaluate => "evaluate",
            CycleState::Accept => "accept",
            CycleState::Reject => "reject",
            CycleState::Iterate => "iterate",
        }
    }

    #[allow(clippy::should_implement_trait)] // Option-returning, unlike FromStr
    pub fn from_str(s: &str) -> Option<CycleState> {
        match s {
            "idle" => Some(CycleState::Idle),
            "select_game" => Some(CycleState::SelectGame),
            "provision_vm" => Some(CycleState::ProvisionVm),
            "baseline_measurement" => Some(CycleState::BaselineMeasurement),
            "analyze" => Some(CycleState::Analyze),
            "generate_optimization" => Some(CycleState::GenerateOptimization),
            "apply_changes" => Some(CycleState::ApplyChanges),
            "comparison_measurement" => Some(CycleState::ComparisonMeasurement),
            "evaluate" => Some(CycleState::Evaluate),
            "accept" => Some(CycleState::Accept),
            "reject" => Some(CycleState::Reject),
            "iterate" => Some(CycleState::Iterate),
            _ => None,
        }
    }

    pub fn valid_transitions(&self) -> &'static [CycleState] {
        match self {
            CycleState::Idle => &[CycleState::SelectGame],
            CycleState::SelectGame => &[CycleState::ProvisionVm],
            CycleState::ProvisionVm => &[CycleState::BaselineMeasurement],
            CycleState::BaselineMeasurement => &[CycleState::Analyze],
            CycleState::Analyze => &[CycleState::GenerateOptimization],
            CycleState::GenerateOptimization => &[CycleState::ApplyChanges],
            CycleState::ApplyChanges => &[CycleState::ComparisonMeasurement],
            CycleState::ComparisonMeasurement => &[CycleState::Evaluate],
            CycleState::Evaluate => &[CycleState::Accept, CycleState::Reject, CycleState::Iterate],
            CycleState::Accept => &[CycleState::Idle],
            CycleState::Reject => &[CycleState::Idle],
            CycleState::Iterate => &[CycleState::Analyze],
        }
    }
}

impl fmt::Display for CycleState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

#[derive(Debug)]
pub struct StateMachine {
    current: CycleState,
    history: Vec<(CycleState, CycleState)>,
}

impl StateMachine {
    pub fn new() -> Self {
        Self {
            current: CycleState::Idle,
            history: Vec::new(),
        }
    }

    pub fn with_state(state: CycleState) -> Self {
        Self {
            current: state,
            history: Vec::new(),
        }
    }

    pub fn state(&self) -> CycleState {
        self.current
    }

    pub fn transition(&mut self, next: CycleState) -> Result<(), String> {
        let valid = self.current.valid_transitions();
        if valid.contains(&next) {
            let from = self.current;
            self.current = next;
            self.history.push((from, next));
            Ok(())
        } else {
            Err(format!(
                "invalid transition from {} to {}",
                self.current.as_str(),
                next.as_str()
            ))
        }
    }

    pub fn history(&self) -> &[(CycleState, CycleState)] {
        &self.history
    }
}

impl Default for StateMachine {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initial_state_is_idle() {
        let sm = StateMachine::new();
        assert_eq!(sm.state(), CycleState::Idle);
    }

    #[test]
    fn valid_transition_idle_to_select_game() {
        let mut sm = StateMachine::new();
        assert!(sm.transition(CycleState::SelectGame).is_ok());
        assert_eq!(sm.state(), CycleState::SelectGame);
    }

    #[test]
    fn valid_full_cycle() {
        let mut sm = StateMachine::new();
        for s in [
            CycleState::SelectGame,
            CycleState::ProvisionVm,
            CycleState::BaselineMeasurement,
            CycleState::Analyze,
            CycleState::GenerateOptimization,
            CycleState::ApplyChanges,
            CycleState::ComparisonMeasurement,
            CycleState::Evaluate,
        ] {
            assert!(sm.transition(s).is_ok());
        }
        assert!(sm.transition(CycleState::Accept).is_ok());
        assert!(sm.transition(CycleState::Idle).is_ok());
    }

    #[test]
    fn invalid_transition_rejected() {
        let mut sm = StateMachine::new();
        assert!(sm.transition(CycleState::Analyze).is_err());
    }

    #[test]
    fn iterate_goes_back_to_analyze() {
        let mut sm = StateMachine::new();
        for s in [
            CycleState::SelectGame,
            CycleState::ProvisionVm,
            CycleState::BaselineMeasurement,
            CycleState::Analyze,
            CycleState::GenerateOptimization,
            CycleState::ApplyChanges,
            CycleState::ComparisonMeasurement,
            CycleState::Evaluate,
            CycleState::Iterate,
        ] {
            assert!(sm.transition(s).is_ok());
        }
        assert!(sm.transition(CycleState::Analyze).is_ok());
    }

    #[test]
    fn state_serializes_to_string() {
        assert_eq!(CycleState::BaselineMeasurement.as_str(), "baseline_measurement");
        assert_eq!(
            CycleState::from_str("baseline_measurement").unwrap(),
            CycleState::BaselineMeasurement
        );
    }

    #[test]
    fn history_tracks_transitions() {
        let mut sm = StateMachine::new();
        sm.transition(CycleState::SelectGame).unwrap();
        sm.transition(CycleState::ProvisionVm).unwrap();
        assert_eq!(sm.history().len(), 2);
    }
}
