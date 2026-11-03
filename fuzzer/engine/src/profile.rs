// Opt-in engine phase timing (SEMANTIST_PROFILE_PHASES=1). A disabled run only
// reads one cached flag per phase; an enabled run accumulates wall time and
// counts and writes the table next to events.jsonl every few seconds and at a
// clean exit, because experiment runs are stopped by signal.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Phase {
    SchedulerNext,
    SchedulerOnAdd,
    Mutate,
    ObserversPre,
    TargetExecution,
    ObserversPost,
    FeedbackSemanticNovelty,
    FeedbackEdges,
    FeedbackTime,
    ObjectiveSemanticEvidence,
    ObjectiveSemanticViolation,
    ObjectiveRuntimeFindings,
    StageSemanticBudget,
    StageMutational,
    IoSeedPersist,
    IoTaskState,
    IoEvents,
    IoFinding,
}

const PHASES: [Phase; 18] = [
    Phase::SchedulerNext,
    Phase::SchedulerOnAdd,
    Phase::Mutate,
    Phase::ObserversPre,
    Phase::TargetExecution,
    Phase::ObserversPost,
    Phase::FeedbackSemanticNovelty,
    Phase::FeedbackEdges,
    Phase::FeedbackTime,
    Phase::ObjectiveSemanticEvidence,
    Phase::ObjectiveSemanticViolation,
    Phase::ObjectiveRuntimeFindings,
    Phase::StageSemanticBudget,
    Phase::StageMutational,
    Phase::IoSeedPersist,
    Phase::IoTaskState,
    Phase::IoEvents,
    Phase::IoFinding,
];

impl Phase {
    fn label(self) -> &'static str {
        match self {
            Self::SchedulerNext => "scheduler next",
            Self::SchedulerOnAdd => "scheduler on_add",
            Self::Mutate => "mutate",
            Self::ObserversPre => "observers pre_exec",
            Self::TargetExecution => "target execution",
            Self::ObserversPost => "observers post_exec",
            Self::FeedbackSemanticNovelty => "feedback SemanticNovelty",
            Self::FeedbackEdges => "feedback MaxMap(edges)",
            Self::FeedbackTime => "feedback Time",
            Self::ObjectiveSemanticEvidence => "objective SemanticEvidence",
            Self::ObjectiveSemanticViolation => "objective SemanticViolation",
            Self::ObjectiveRuntimeFindings => "objective RuntimeFindings",
            Self::StageSemanticBudget => "stage SemanticTaskBudget",
            Self::StageMutational => "stage StdMutational (incl.)",
            Self::IoSeedPersist => "io seed metadata (on_add, next)",
            Self::IoTaskState => "io task state (in budget)",
            Self::IoEvents => "io events append (nested)",
            Self::IoFinding => "io finding write (in objective)",
        }
    }

    // Nested phases are already part of an enclosing phase; the table keeps
    // them separate from the exclusive sum.
    fn nested(self) -> bool {
        matches!(
            self,
            Self::StageMutational
                | Self::IoSeedPersist
                | Self::IoTaskState
                | Self::IoEvents
                | Self::IoFinding
        )
    }
}

#[derive(Debug)]
struct PhaseTotals {
    started: std::time::Instant,
    last_flush: std::time::Instant,
    output: Option<PathBuf>,
    nanos: [u128; PHASES.len()],
    counts: [u64; PHASES.len()],
    open: [Option<std::time::Instant>; 2],
    // Executed inputs equal to one of the last REPEAT_WINDOW executed inputs.
    // Only a profiling run hashes inputs; the engine itself never does.
    repeated_inputs: u64,
    recent_inputs: HashSet<u64>,
    recent_order: std::collections::VecDeque<u64>,
}

const REPEAT_WINDOW: usize = 65_536;

thread_local! {
    static PHASE_TOTALS: std::cell::RefCell<Option<PhaseTotals>> = const { std::cell::RefCell::new(None) };
}

fn phase_profiling_enabled() -> bool {
    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ENABLED.get_or_init(|| {
        matches!(
            env::var("SEMANTIST_PROFILE_PHASES").unwrap_or_default().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes"
        )
    })
}

fn phase_profile_begin(output: Option<PathBuf>) {
    if !phase_profiling_enabled() {
        return;
    }
    let now = std::time::Instant::now();
    PHASE_TOTALS.with(|totals| {
        *totals.borrow_mut() = Some(PhaseTotals {
            started: now,
            last_flush: now,
            output,
            nanos: [0; PHASES.len()],
            counts: [0; PHASES.len()],
            open: [None; 2],
            repeated_inputs: 0,
            recent_inputs: HashSet::new(),
            recent_order: std::collections::VecDeque::new(),
        });
    });
}

fn phase_count_input(bytes: &[u8]) {
    use std::hash::{Hash, Hasher};
    PHASE_TOTALS.with(|totals| {
        let mut totals = totals.borrow_mut();
        let Some(totals) = totals.as_mut() else { return; };
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        bytes.hash(&mut hasher);
        let hash = hasher.finish();
        if !totals.recent_inputs.insert(hash) {
            totals.repeated_inputs += 1;
            return;
        }
        totals.recent_order.push_back(hash);
        if totals.recent_order.len() > REPEAT_WINDOW {
            if let Some(oldest) = totals.recent_order.pop_front() {
                totals.recent_inputs.remove(&oldest);
            }
        }
    });
}

fn phase_index(phase: Phase) -> usize {
    PHASES.iter().position(|item| *item == phase).unwrap_or(0)
}

fn phase_record(phase: Phase, elapsed: Duration) {
    PHASE_TOTALS.with(|totals| {
        if let Some(totals) = totals.borrow_mut().as_mut() {
            let index = phase_index(phase);
            totals.nanos[index] += elapsed.as_nanos();
            totals.counts[index] += 1;
        }
    });
}

// Drop guard, so early returns through `?` are still charged.
struct PhaseTimer {
    phase: Phase,
    started: Option<std::time::Instant>,
}

impl PhaseTimer {
    #[inline]
    fn start(phase: Phase) -> Self {
        Self {
            phase,
            started: phase_profiling_enabled().then(std::time::Instant::now),
        }
    }
}

impl Drop for PhaseTimer {
    #[inline]
    fn drop(&mut self) {
        if let Some(started) = self.started {
            phase_record(self.phase, started.elapsed());
            if self.phase == Phase::StageMutational {
                phase_profile_flush(false);
            }
        }
    }
}

fn phase_profile_table(totals: &PhaseTotals) -> String {
    let wall = totals.started.elapsed();
    let wall_nanos = wall.as_nanos().max(1);
    let executions = totals.counts[phase_index(Phase::TargetExecution)];
    let mut out = format!(
        "SemantiST phase profile: wall {:.3}s, target executions {executions}\n{:<34} {:>12} {:>12} {:>12} {:>9} {:>12}\n",
        wall.as_secs_f64(),
        "phase",
        "count",
        "total_s",
        "mean_us",
        "wall_%",
        "us_per_exec"
    );
    let mut exclusive = 0u128;
    for (index, phase) in PHASES.iter().enumerate() {
        let nanos = totals.nanos[index];
        let count = totals.counts[index];
        if !phase.nested() {
            exclusive += nanos;
        }
        out.push_str(&format!(
            "{:<34} {:>12} {:>12.3} {:>12.2} {:>9.2} {:>12.3}\n",
            phase.label(),
            count,
            nanos as f64 / 1e9,
            if count == 0 { 0.0 } else { nanos as f64 / count as f64 / 1e3 },
            nanos as f64 * 100.0 / wall_nanos as f64,
            if executions == 0 { 0.0 } else { nanos as f64 / executions as f64 / 1e3 },
        ));
    }
    out.push_str(&format!(
        "repeated inputs executed (within the last {REPEAT_WINDOW}): {} ({:.1}% of executions)\n",
        totals.repeated_inputs,
        if executions == 0 { 0.0 } else { totals.repeated_inputs as f64 * 100.0 / executions as f64 },
    ));
    let other = wall_nanos.saturating_sub(exclusive);
    out.push_str(&format!(
        "{:<34} {:>12} {:>12.3} {:>12} {:>9.2} {:>12.3}\n",
        "unattributed (startup, LibAFL)",
        "",
        other as f64 / 1e9,
        "",
        other as f64 * 100.0 / wall_nanos as f64,
        if executions == 0 { 0.0 } else { other as f64 / executions as f64 / 1e3 },
    ));
    out
}

fn phase_profile_flush(force: bool) {
    PHASE_TOTALS.with(|totals| {
        let mut totals = totals.borrow_mut();
        let Some(totals) = totals.as_mut() else { return; };
        if !force && totals.last_flush.elapsed() < Duration::from_secs(5) {
            return;
        }
        totals.last_flush = std::time::Instant::now();
        let table = phase_profile_table(totals);
        if let Some(path) = totals.output.as_deref() {
            let temporary = path.with_extension("txt.tmp");
            if fs::write(&temporary, &table).is_ok() {
                let _ = fs::rename(temporary, path);
            }
        }
        if force {
            println!("{table}");
        }
    });
}

// Pre/post observer cost is spent in LibAFL's observer tuple walk. One marker
// opens a window at the front of the tuple and one closes it at the back.
#[derive(Debug, Deserialize, Serialize)]
struct PhaseMarkerObserver {
    name: Cow<'static, str>,
    closes: bool,
}

impl PhaseMarkerObserver {
    fn opening() -> Self {
        Self { name: Cow::Borrowed("phase_marker_open"), closes: false }
    }

    fn closing() -> Self {
        Self { name: Cow::Borrowed("phase_marker_close"), closes: true }
    }

    fn mark(&self, slot: usize, phase: Phase) {
        if !phase_profiling_enabled() {
            return;
        }
        let elapsed = PHASE_TOTALS.with(|totals| {
            let mut totals = totals.borrow_mut();
            let totals = totals.as_mut()?;
            if self.closes {
                totals.open[slot].take().map(|started| started.elapsed())
            } else {
                totals.open[slot] = Some(std::time::Instant::now());
                None
            }
        });
        if let Some(elapsed) = elapsed {
            phase_record(phase, elapsed);
        }
    }
}

impl Named for PhaseMarkerObserver {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<I, S> libafl::observers::Observer<I, S> for PhaseMarkerObserver {
    fn pre_exec(&mut self, _state: &mut S, _input: &I) -> Result<(), Error> {
        self.mark(0, Phase::ObserversPre);
        Ok(())
    }

    fn post_exec(&mut self, _state: &mut S, _input: &I, _exit_kind: &ExitKind) -> Result<(), Error> {
        self.mark(1, Phase::ObserversPost);
        Ok(())
    }
}

#[derive(Debug)]
struct ProfiledFeedback<F> {
    inner: F,
    phase: Phase,
}

impl<F> ProfiledFeedback<F> {
    fn new(inner: F, phase: Phase) -> Self {
        Self { inner, phase }
    }
}

impl<F: Named> Named for ProfiledFeedback<F> {
    fn name(&self) -> &Cow<'static, str> {
        self.inner.name()
    }
}

impl<F: StateInitializer<S>, S> StateInitializer<S> for ProfiledFeedback<F> {
    fn init_state(&mut self, state: &mut S) -> Result<(), Error> {
        self.inner.init_state(state)
    }
}

impl<F: Feedback<EM, I, OT, S>, EM, I, OT, S> Feedback<EM, I, OT, S> for ProfiledFeedback<F> {
    fn is_interesting(&mut self, state: &mut S, manager: &mut EM, input: &I,
        observers: &OT, exit_kind: &ExitKind) -> Result<bool, Error>
    {
        let _timer = PhaseTimer::start(self.phase);
        self.inner.is_interesting(state, manager, input, observers, exit_kind)
    }

    fn append_metadata(&mut self, state: &mut S, manager: &mut EM, observers: &OT,
        testcase: &mut Testcase<I>) -> Result<(), Error>
    {
        self.inner.append_metadata(state, manager, observers, testcase)
    }
}

#[derive(Debug)]
struct ProfiledStage<T> {
    inner: T,
    phase: Phase,
}

impl<T> ProfiledStage<T> {
    fn new(inner: T, phase: Phase) -> Self {
        Self { inner, phase }
    }
}

impl<T: Stage<E, EM, S, Z>, E, EM, S, Z> Stage<E, EM, S, Z> for ProfiledStage<T> {
    fn perform(&mut self, fuzzer: &mut Z, executor: &mut E, state: &mut S, manager: &mut EM) -> Result<(), Error> {
        let _timer = PhaseTimer::start(self.phase);
        self.inner.perform(fuzzer, executor, state, manager)
    }
}

impl<T: Restartable<S>, S> Restartable<S> for ProfiledStage<T> {
    fn should_restart(&mut self, state: &mut S) -> Result<bool, Error> {
        self.inner.should_restart(state)
    }

    fn clear_progress(&mut self, state: &mut S) -> Result<(), Error> {
        self.inner.clear_progress(state)
    }
}

#[derive(Debug)]
struct ProfiledExecutor<E> {
    inner: E,
}

impl<E: HasObservers> HasObservers for ProfiledExecutor<E> {
    type Observers = E::Observers;

    fn observers(&self) -> libafl_bolts::tuples::RefIndexable<&Self::Observers, Self::Observers> {
        self.inner.observers()
    }

    fn observers_mut(&mut self) -> libafl_bolts::tuples::RefIndexable<&mut Self::Observers, Self::Observers> {
        self.inner.observers_mut()
    }
}

impl<E: libafl::executors::Executor<EM, I, S, Z>, EM, I: AsRef<Vec<u8>>, S, Z> libafl::executors::Executor<EM, I, S, Z>
    for ProfiledExecutor<E>
{
    fn run_target(&mut self, fuzzer: &mut Z, state: &mut S, mgr: &mut EM, input: &I) -> Result<ExitKind, Error> {
        if phase_profiling_enabled() {
            phase_count_input(input.as_ref());
        }
        let _timer = PhaseTimer::start(Phase::TargetExecution);
        self.inner.run_target(fuzzer, state, mgr, input)
    }
}

#[cfg(test)]
mod phase_profile_tests {
    use super::*;

    #[test]
    fn nested_phases_are_reported_but_not_double_counted() {
        let mut totals = PhaseTotals {
            started: std::time::Instant::now(),
            last_flush: std::time::Instant::now(),
            output: None,
            nanos: [0; PHASES.len()],
            counts: [0; PHASES.len()],
            open: [None; 2],
            repeated_inputs: 1,
            recent_inputs: HashSet::new(),
            recent_order: std::collections::VecDeque::new(),
        };
        totals.nanos[phase_index(Phase::TargetExecution)] = 2_000;
        totals.counts[phase_index(Phase::TargetExecution)] = 2;
        totals.nanos[phase_index(Phase::StageMutational)] = 5_000;
        totals.counts[phase_index(Phase::StageMutational)] = 1;
        let table = phase_profile_table(&totals);
        assert!(table.contains("target executions 2"));
        assert!(table.contains("repeated inputs executed (within the last 65536): 1 (50.0% of executions)"));
        let line = table.lines().find(|line| line.starts_with("target execution ")).unwrap();
        assert!(line.contains("1.00"), "{line}");
        assert!(PHASES.iter().all(|phase| table.contains(phase.label())));
        assert!(Phase::StageMutational.nested() && !Phase::TargetExecution.nested());
    }
}
