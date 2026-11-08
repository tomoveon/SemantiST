// GoalStateObserver: read the fields named by goal-tasks.json from the state
// region after every execution and expose a per-task distance to GoalStage.

use libafl::observers::Observer;

#[derive(Clone, Debug)]
pub struct GoalFrameFeedback {
    pub frame_index: usize,
    pub logical_time_ns: u64,
    pub exit_kind: String,
    pub goal_hits: Vec<u32>,
    pub distances: std::collections::BTreeMap<String, f64>,
    pub abstractions: std::collections::BTreeMap<String, Vec<i64>>,
    pub polarities: std::collections::BTreeMap<String, String>,
}

#[derive(Debug)]
pub struct GoalStateObserver {
    name: Cow<'static, str>,
    tasks: V3Tasks,
    layout: V3Layout,
    state: Vec<u8>,
    distances: HashMap<String, f64>,
    abstractions: HashMap<String, Vec<i64>>,
    best_frames: HashMap<String, usize>,
    frame_feedback: Vec<GoalFrameFeedback>,
    active: bool,
}

impl GoalStateObserver {
    pub fn new(bundle: &V3Bundle) -> Self {
        Self {
            name: Cow::Borrowed("v3_goal_state"),
            tasks: bundle.tasks.clone(),
            layout: bundle.layout.clone(),
            state: Vec::new(),
            distances: HashMap::new(),
            abstractions: HashMap::new(),
            best_frames: HashMap::new(),
            frame_feedback: Vec::new(),
            active: true,
        }
    }

    pub fn distance(&self, task_id: &str) -> f64 {
        self.distances.get(task_id).copied().unwrap_or(f64::INFINITY)
    }

    pub fn abstraction(&self, task_id: &str) -> Option<&[i64]> {
        self.abstractions.get(task_id).map(Vec::as_slice)
    }

    pub fn best_frame(&self, task_id: &str) -> Option<usize> {
        self.best_frames.get(task_id).copied()
    }

    pub fn frame_feedback(&self) -> &[GoalFrameFeedback] {
        &self.frame_feedback
    }

    pub fn set_active(&mut self, active: bool) {
        self.active = active;
    }

    pub fn values(&self) -> std::collections::BTreeMap<String, i128> {
        let mut result = std::collections::BTreeMap::new();
        for task in &self.tasks.tasks {
            for entry in &task.state_slice {
                if let Some(value) = self.read_field(entry) {
                    result.insert(entry.name.clone(), value);
                }
            }
        }
        result
    }

    fn read_field(&self, entry: &StateSlice) -> Option<i128> {
        read_state_field(&self.layout, &self.state, entry)
    }

    pub fn observe_trace(&mut self, outcome: &ExecOutcome) {
        self.observe_trace_with_frames(outcome, &[], &[]);
    }

    pub fn observe_trace_with_frames(&mut self, outcome: &ExecOutcome,
                                     frames: &[Frame], input_fields: &[V3Field]) {
        if !outcome.final_state.is_empty() {
            self.state.clone_from(&outcome.final_state);
        }
        self.distances.clear();
        self.abstractions.clear();
        self.best_frames.clear();
        self.frame_feedback.clear();
        for observation in &outcome.observations {
            let mut values = observation.values.clone();
            if let Some(frame) = frames.get(observation.frame_index) {
                for field in input_fields {
                    if field.spec.kind != "scalar" { continue; }
                    if let Some(value) = frame.values.get(&field.name)
                        .and_then(|raw| raw.parse::<i128>().ok()) {
                        values.insert(field.name.clone(), value);
                    }
                }
            }
            let mut distances = std::collections::BTreeMap::new();
            let mut abstractions = std::collections::BTreeMap::new();
            let mut polarities = std::collections::BTreeMap::new();
            for task in &self.tasks.tasks {
                let (distance, buckets) = self.score_task_values(task, &values);
                distances.insert(task.task_id.clone(), distance);
                abstractions.insert(task.task_id.clone(), buckets.clone());
                polarities.insert(task.task_id.clone(), task.polarity.clone().unwrap_or_else(|| "unknown".to_string()));
                if distance < self.distance(&task.task_id) {
                    self.distances.insert(task.task_id.clone(), distance);
                    self.abstractions.insert(task.task_id.clone(), buckets);
                    self.best_frames.insert(task.task_id.clone(), observation.frame_index);
                }
            }
            self.frame_feedback.push(GoalFrameFeedback {
                frame_index: observation.frame_index,
                logical_time_ns: observation.logical_time_ns,
                exit_kind: if outcome.exit_kind.is_empty() { "Ok".to_string() } else { outcome.exit_kind.clone() },
                goal_hits: observation.goal_hits.clone(),
                distances,
                abstractions,
                polarities,
            });
        }
    }

    fn update(&mut self) {
        let size = unsafe { semantist_state_size() };
        if size == 0 {
            return;
        }
        if self.state.len() != size {
            self.state.resize(size, 0);
        }
        unsafe { semantist_checkpoint(self.state.as_mut_ptr()) };
        self.distances.clear();
        self.abstractions.clear();
        self.best_frames.clear();
        for task in &self.tasks.tasks {
            let (best_distance, buckets) = self.score_task(task);
            self.distances.insert(task.task_id.clone(), best_distance);
            self.abstractions.insert(task.task_id.clone(), buckets);
        }
    }

    fn score_task(&self, task: &V3Task) -> (f64, Vec<i64>) {
        let values = task.state_slice.iter().filter_map(|entry|
            self.read_field(entry).map(|value| (entry.name.clone(), value))).collect();
        self.score_task_values(task, &values)
    }

    fn score_task_values(&self, task: &V3Task, values: &std::collections::BTreeMap<String, i128>) -> (f64, Vec<i64>) {
        let mut abstraction_distance = f64::INFINITY;
        let mut buckets = Vec::new();
        for entry in &task.abstraction {
            let Some(value) = value_for_field(values, &entry.field) else { continue };
            if !entry.thresholds.is_empty() {
                let mut best = f64::INFINITY;
                for threshold in &entry.thresholds {
                    best = best.min(((value - i128::from(*threshold)).abs() as f64)
                                    / (1.0 + threshold.unsigned_abs() as f64));
                }
                abstraction_distance = if abstraction_distance.is_infinite() { best } else { abstraction_distance.max(best) };
            }
            buckets.push(bucket(&entry.thresholds, value));
        }
        let mut path_distance = f64::INFINITY;
        for path in &task.candidate_paths {
            if path.conditions.is_empty() { continue; }
            let mut total = 0.0;
            let mut known = true;
            for condition in &path.conditions {
                if let Some(distance) = self.condition_distance(condition, values, task.polarity.as_deref()) {
                    total += distance;
                } else {
                    known = false;
                    break;
                }
            }
            if known { path_distance = path_distance.min(total); }
        }
        (path_distance.min(abstraction_distance), buckets)
    }

    fn condition_distance(&self, condition: &V3Condition, values: &std::collections::BTreeMap<String, i128>, polarity: Option<&str>) -> Option<f64> {
        let field = condition
            .lhs
            .get("field")
            .or_else(|| condition.lhs.get("name"))
            .and_then(serde_json::Value::as_str)?;
        let value = value_for_field(values, field)?;
        let rhs = condition
            .rhs
            .get("const")
            .and_then(json_i128)
            .or_else(|| json_i128(&condition.rhs))?;
        let op = effective_operator(&condition.op, polarity);
        let distance = match op.as_str() {
            "=" | "==" => (value - rhs).abs() as f64,
            "!=" | "<>" => { if value == rhs { 1.0 } else { 0.0 } },
            ">" => (rhs - value + 1).max(0) as f64,
            ">=" => (rhs - value).max(0) as f64,
            "<" => (value - rhs + 1).max(0) as f64,
            "<=" => (value - rhs).max(0) as f64,
            _ => return None,
        };
        Some(distance / (1.0 + rhs.unsigned_abs() as f64))
    }
}

fn value_for_field(values: &std::collections::BTreeMap<String, i128>, field: &str) -> Option<i128> {
    values.get(field).copied().or_else(|| values.iter()
        .find(|(name, _)| name.eq_ignore_ascii_case(field))
        .map(|(_, value)| *value))
}

fn effective_operator(operator: &str, polarity: Option<&str>) -> String {
    let inverse = polarity.is_some_and(|value| matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "negative" | "negated" | "false" | "not" | "inverse" | "inverted"
    ));
    if !inverse {
        return operator.to_string();
    }
    match operator {
        "=" | "==" => "!=".to_string(),
        "!=" | "<>" => "==".to_string(),
        ">" => "<=".to_string(),
        ">=" => "<".to_string(),
        "<" => ">=".to_string(),
        "<=" => ">".to_string(),
        _ => operator.to_string(),
    }
}

fn json_i128(value: &serde_json::Value) -> Option<i128> {
    value
        .as_i64()
        .map(i128::from)
        .or_else(|| value.as_u64().map(i128::from))
        .or_else(|| value.as_str().and_then(|text| text.parse::<i128>().ok()))
}

fn read_state_field(layout: &V3Layout, state: &[u8], entry: &StateSlice) -> Option<i128> {
        let base = layout.region_base(&entry.region)?;
        let offset = entry.offset? as usize;
        let index = base.checked_add(offset)?;
        let ctype = entry.ctype.as_deref().unwrap_or("int32_t");
        let bytes = state.get(index..)?;
        Some(match ctype {
            "uint8_t" | "bool" => i128::from(*bytes.first()?),
            "int8_t" => i128::from(*bytes.first()? as i8),
            "uint16_t" => i128::from(u16::from_le_bytes(bytes.get(..2)?.try_into().ok()?)),
            "int16_t" => i128::from(i16::from_le_bytes(bytes.get(..2)?.try_into().ok()?)),
            "uint32_t" => i128::from(u32::from_le_bytes(bytes.get(..4)?.try_into().ok()?)),
            "int32_t" => i128::from(i32::from_le_bytes(bytes.get(..4)?.try_into().ok()?)),
            "uint64_t" | "ULINT" | "LWORD" => i128::from(u64::from_le_bytes(bytes.get(..8)?.try_into().ok()?)),
            "int64_t" | "LINT" => i128::from(i64::from_le_bytes(bytes.get(..8)?.try_into().ok()?)),
            _ => return None,
        })
}

fn capture_frame_observation(bundle: &V3Bundle, frame_index: usize, logical_time_ns: u64) -> FrameObservation {
    let size = unsafe { semantist_state_size() };
    let mut state = vec![0u8; size];
    if size > 0 { unsafe { semantist_checkpoint(state.as_mut_ptr()) }; }
    let mut values = std::collections::BTreeMap::new();
    let mut goal_hits = Vec::new();
    for task in &bundle.tasks.tasks {
        for entry in &task.state_slice {
            if let Some(value) = read_state_field(&bundle.layout, &state, entry) {
                values.insert(entry.name.clone(), value);
            }
        }
        if let Some(runtime_id) = task.goal.runtime_id {
            if goal_map_hit(runtime_id) {
                goal_hits.push(runtime_id);
            }
        }
    }
    goal_hits.sort_unstable();
    goal_hits.dedup();
    FrameObservation { frame_index, logical_time_ns, values, goal_hits }
}

fn bucket(thresholds: &[i64], value: i128) -> i64 {
    thresholds.iter().filter(|threshold| value >= i128::from(**threshold)).count() as i64
}

impl Named for GoalStateObserver {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<I, S> Observer<I, S> for GoalStateObserver {
    fn pre_exec(&mut self, _state: &mut S, _input: &I) -> Result<(), Error> {
        self.distances.clear();
        self.abstractions.clear();
        self.best_frames.clear();
        self.frame_feedback.clear();
        self.state.clear();
        Ok(())
    }

    fn post_exec(&mut self, _state: &mut S, _input: &I, exit_kind: &libafl::executors::ExitKind) -> Result<(), Error> {
        if self.active && matches!(exit_kind, libafl::executors::ExitKind::Ok) {
            self.update();
        }
        Ok(())
    }
}

#[cfg(test)]
mod v3_observer_tests {
    use super::*;

    #[test]
    fn uint64_state_does_not_wrap_to_negative() {
        let observer = GoalStateObserver {
            name: Cow::Borrowed("test"),
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None, single_cycle: false, unknown_cycle_effects: false, tasks: vec![] },
            layout: V3Layout { total_size: 8, snapshot_schema_version: 1, harness_region_index: None,
                               regions: vec![V3Region { index: 0, name: "state".into(), size: 8,
                                                        source: "target".into(), fields: vec![],
                                                        pointer_offsets: vec![] }] },
            state: u64::MAX.to_le_bytes().to_vec(),
            distances: HashMap::new(),
            abstractions: HashMap::new(),
            best_frames: HashMap::new(),
            frame_feedback: Vec::new(),
            active: true,
        };
        let field = StateSlice { name: "Counter".into(), region: "state".into(), offset: Some(0),
                                 ctype: Some("uint64_t".into()), block_kind: None };
        assert_eq!(observer.read_field(&field), Some(u64::MAX as i128));
    }

    #[test]
    fn isolated_trace_uses_worker_final_state() {
        let mut observer = GoalStateObserver {
            name: Cow::Borrowed("test"),
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None,
                             single_cycle: false, unknown_cycle_effects: false, tasks: vec![] },
            layout: V3Layout { total_size: 4, snapshot_schema_version: 1, harness_region_index: None,
                               regions: vec![V3Region { index: 0, name: "state".into(), size: 4,
                                                        source: "target".into(), fields: vec![], pointer_offsets: vec![] }] },
            state: 0i32.to_le_bytes().to_vec(),
            distances: HashMap::new(), abstractions: HashMap::new(),
            best_frames: HashMap::new(), frame_feedback: Vec::new(), active: true,
        };
        let outcome = ExecOutcome { final_state: 7i32.to_le_bytes().to_vec(), ..ExecOutcome::default() };
        observer.observe_trace_with_frames(&outcome, &[], &[]);
        let field = StateSlice { name: "X".into(), region: "state".into(), offset: Some(0),
                                 ctype: Some("int32_t".into()), block_kind: None };
        assert_eq!(observer.read_field(&field), Some(7));
    }

    #[test]
    fn conjunction_requires_every_condition_and_unknown_is_not_zero_distance() {
        let mut observer = GoalStateObserver {
            name: Cow::Borrowed("test"),
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None, single_cycle: false, unknown_cycle_effects: false, tasks: vec![] },
            layout: V3Layout { total_size: 8, snapshot_schema_version: 1, harness_region_index: None,
                               regions: vec![V3Region { index: 0, name: "state".into(), size: 8,
                                                      source: "target".into(), fields: vec![], pointer_offsets: vec![] }] },
            state: [6i32.to_le_bytes(), 0i32.to_le_bytes()].concat(),
            distances: HashMap::new(), abstractions: HashMap::new(), best_frames: HashMap::new(), frame_feedback: Vec::new(), active: true,
        };
        let task: V3Task = serde_json::from_value(serde_json::json!({
            "task_id": "both", "goal": {"runtime_id": null, "stable_id": null, "kind": null, "hazard": null},
            "state_slice": [
                {"name": "X", "region": "state", "offset": 0, "ctype": "int32_t"},
                {"name": "Y", "region": "state", "offset": 4, "ctype": "int32_t"}
            ],
            "candidate_paths": [{"path_id": "p", "conditions": [
                {"lhs": {"field": "X"}, "op": ">", "rhs": {"const": 5}},
                {"lhs": {"field": "Y"}, "op": ">", "rhs": {"const": 5}}
            ]}]
        })).unwrap();
        assert!(observer.score_task(&task).0 > 0.0);
        observer.state[4..8].copy_from_slice(&6i32.to_le_bytes());
        assert_eq!(observer.score_task(&task).0, 0.0);
        let mut unknown = task.clone();
        unknown.state_slice.pop();
        assert!(observer.score_task(&unknown).0.is_infinite());
    }

    #[test]
    fn polarity_reverses_the_target_direction_without_making_unknown_zero() {
        let observer = GoalStateObserver {
            name: Cow::Borrowed("test"),
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None, single_cycle: false, unknown_cycle_effects: false, tasks: vec![] },
            layout: V3Layout { total_size: 0, snapshot_schema_version: 1, harness_region_index: None, regions: vec![] },
            state: Vec::new(), distances: HashMap::new(), abstractions: HashMap::new(),
            best_frames: HashMap::new(), frame_feedback: Vec::new(), active: true,
        };
        let values = [("X".to_string(), 0i128)].into();
        let condition: V3Condition = serde_json::from_value(serde_json::json!({
            "lhs": {"field": "X"}, "op": ">", "rhs": {"const": 5}
        })).unwrap();
        let positive = observer.condition_distance(&condition, &values, Some("positive")).unwrap();
        let negative = observer.condition_distance(&condition, &values, Some("negative")).unwrap();
        assert!(positive > 0.0);
        assert_eq!(negative, 0.0);
        assert!(observer.condition_distance(&condition, &values, Some("unknown")).unwrap() > 0.0);
    }

    #[test]
    fn unsigned_condition_rhs_keeps_values_above_i64() {
        let observer = GoalStateObserver {
            name: Cow::Borrowed("test"),
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None, single_cycle: false, unknown_cycle_effects: false, tasks: vec![] },
            layout: V3Layout { total_size: 0, snapshot_schema_version: 1, harness_region_index: None, regions: vec![] },
            state: Vec::new(), distances: HashMap::new(), abstractions: HashMap::new(),
            best_frames: HashMap::new(), frame_feedback: Vec::new(), active: true,
        };
        let value = 18_446_744_073_709_551_615i128;
        let values = [("X".to_string(), value)].into();
        let condition: V3Condition = serde_json::from_value(serde_json::json!({
            "lhs": {"field": "X"}, "op": "==", "rhs": {"const": "18446744073709551615"}
        })).unwrap();
        assert_eq!(observer.condition_distance(&condition, &values, None), Some(0.0));
    }

    #[test]
    fn scalar_frame_input_scores_case_insensitive_path_condition() {
        let task: V3Task = serde_json::from_value(serde_json::json!({
            "task_id": "renamed", "goal": {},
            "candidate_paths": [{"path_id": "p", "conditions": [
                {"lhs": {"field": "n"}, "op": ">", "rhs": {"const": 0}}
            ]}],
            "abstraction": [{"field": "n", "thresholds": [0, 7]}]
        })).unwrap();
        let mut observer = GoalStateObserver {
            name: Cow::Borrowed("test"),
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None,
                single_cycle: true, unknown_cycle_effects: false, tasks: vec![task] },
            layout: V3Layout { total_size: 0, snapshot_schema_version: 1,
                harness_region_index: None, regions: vec![] },
            state: Vec::new(), distances: HashMap::new(), abstractions: HashMap::new(),
            best_frames: HashMap::new(), frame_feedback: Vec::new(), active: true,
        };
        let outcome = ExecOutcome { frames: 1, observations: vec![FrameObservation {
            frame_index: 0, logical_time_ns: 10, values: BTreeMap::new(), goal_hits: vec![],
        }], ..ExecOutcome::default() };
        let field: V3Field = serde_json::from_value(serde_json::json!({
            "name": "N", "block_kind": "VAR_INPUT", "spec": {"kind": "scalar", "name": "INT"}
        })).unwrap();
        let frame = Frame::new(0, [("N".into(), "3".into())].into(), None);
        observer.observe_trace_with_frames(&outcome, &[frame], &[field]);
        assert_eq!(observer.distance("renamed"), 0.0);
        assert_eq!(observer.best_frame("renamed"), Some(0));
        assert_eq!(observer.abstraction("renamed"), Some([1].as_slice()));
    }

    #[test]
    fn earlier_frame_goal_survives_a_later_non_goal_state() {
        let task: V3Task = serde_json::from_value(serde_json::json!({
            "task_id": "early", "goal": {"runtime_id": 9, "stable_id": null, "kind": null, "hazard": null},
            "state_slice": [{"name": "X", "region": "state", "offset": 0, "ctype": "int32_t"}],
            "candidate_paths": [{"path_id": "p", "conditions": [
                {"lhs": {"field": "X"}, "op": "==", "rhs": {"const": 7}}
            ]}],
            "abstraction": [{"field": "X", "thresholds": [5]}]
        })).unwrap();
        let mut observer = GoalStateObserver {
            name: Cow::Borrowed("test"),
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None, single_cycle: false, unknown_cycle_effects: false, tasks: vec![task] },
            layout: V3Layout { total_size: 4, snapshot_schema_version: 1, harness_region_index: None,
                regions: vec![V3Region { index: 0, name: "state".into(), size: 4,
                    source: "target".into(), fields: vec![], pointer_offsets: vec![] }] },
            state: 0i32.to_le_bytes().to_vec(),
            distances: HashMap::new(), abstractions: HashMap::new(), best_frames: HashMap::new(),
            frame_feedback: Vec::new(), active: true,
        };
        let outcome = ExecOutcome {
            frames: 2,
            observations: vec![
                FrameObservation { frame_index: 0, logical_time_ns: 10, values: [("X".into(), 7)].into(), goal_hits: vec![9] },
                FrameObservation { frame_index: 1, logical_time_ns: 25, values: [("X".into(), 0)].into(), goal_hits: vec![9] },
            ],
            ..ExecOutcome::default()
        };
        observer.observe_trace(&outcome);
        assert_eq!(observer.distance("early"), 0.0);
        assert_eq!(observer.best_frame("early"), Some(0));
        assert_eq!(observer.abstraction("early"), Some([1].as_slice()));
        assert_eq!(observer.frame_feedback()[0].logical_time_ns, 10);
        assert_eq!(observer.frame_feedback()[0].exit_kind, "Ok");
        assert_eq!(observer.frame_feedback()[0].goal_hits, vec![9]);
        assert!(observer.frame_feedback()[1].distances["early"] > 0.0);
    }
}

