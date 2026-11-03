// In-process execution. build_target.sh links this library with one target's
// harness and instrumented object into that target's own fuzzer binary; each
// input is then one call of semantist_harness_execute. The target writes the
// static buffers below, which the observers read directly.

// Guard numbering as afl-compiler-rt.o assigns it (the first guard gets 6), so
// a target reports the same edge indices in-process as under the forkserver.
const AFL_FIRST_GUARD_OFFSET: u32 = 5;
const SEMANTIC_BUFFER_CAPACITY: usize = 1 << 20;

#[repr(C, align(8))]
struct InProcessBuffer<const N: usize>(std::cell::UnsafeCell<[u8; N]>);

// Written only by the target during an execution and read by observers in
// between, on one thread.
unsafe impl<const N: usize> Sync for InProcessBuffer<N> {}

impl<const N: usize> InProcessBuffer<N> {
    const fn new() -> Self {
        Self(std::cell::UnsafeCell::new([0; N]))
    }

    fn as_mut_ptr(&self) -> *mut u8 {
        self.0.get().cast()
    }
}

static EDGES_BUFFER: InProcessBuffer<MAP_SIZE> = InProcessBuffer::new();
static EDGES_ASSIGNED: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(AFL_FIRST_GUARD_OFFSET);

#[no_mangle]
static __semantist_semantic_buffer: InProcessBuffer<SEMANTIC_BUFFER_CAPACITY> = InProcessBuffer::new();
#[no_mangle]
static __semantist_semantic_buffer_size: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
#[no_mangle]
static __semantist_telemetry_buffer: InProcessBuffer<TELEMETRY_SIZE> = InProcessBuffer::new();

static MEMORY_MEMBER_ORACLE_ENABLED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
static MEMORY_MEMBER_VIOLATION: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
static ASAN_VIOLATION: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

extern "C" {
    fn __asan_set_error_report_callback(callback: unsafe extern "C" fn(*const std::ffi::c_char));
    fn __asan_get_report_access_type() -> std::ffi::c_int;
    fn __asan_get_report_description() -> *const std::ffi::c_char;
}

fn recoverable_asan_report(access_type: i32, description: &[u8]) -> bool {
    access_type == 0 && description == b"global-buffer-overflow"
}

#[cfg(test)]
#[test]
fn only_readonly_global_overflow_can_resume_inprocess() {
    assert!(recoverable_asan_report(0, b"global-buffer-overflow"));
    assert!(!recoverable_asan_report(1, b"global-buffer-overflow"));
    assert!(!recoverable_asan_report(0, b"heap-use-after-free"));
}

unsafe extern "C" fn v3_asan_report(_report: *const std::ffi::c_char) {
    let description = unsafe { __asan_get_report_description() };
    let description = if description.is_null() {
        &[][..]
    } else {
        unsafe { std::ffi::CStr::from_ptr(description) }.to_bytes()
    };
    if !recoverable_asan_report(unsafe { __asan_get_report_access_type() }, description) {
        std::process::abort();
    }
    ASAN_VIOLATION.store(true, std::sync::atomic::Ordering::Relaxed);
}

/// Weakly called by the compatibility memory runtime. Native forked targets
/// do not link this symbol and retain their ASan abort. The v3 in-process
/// engine enables it around one execution so an intra-object member overflow
/// becomes a recoverable Crash observation and can be cold-replay confirmed.
#[no_mangle]
pub extern "C" fn __semantist_memory_member_bounds_continue(
    _object_size: u64,
    _count: u64,
) -> u8 {
    use std::sync::atomic::Ordering;
    if MEMORY_MEMBER_ORACLE_ENABLED.load(Ordering::Relaxed) {
        MEMORY_MEMBER_VIOLATION.store(true, Ordering::Relaxed);
        1
    } else {
        0
    }
}

fn memory_member_oracle_reset() {
    use std::sync::atomic::Ordering;
    MEMORY_MEMBER_VIOLATION.store(false, Ordering::Relaxed);
}

fn memory_member_oracle_seen() -> bool {
    use std::sync::atomic::Ordering;
    MEMORY_MEMBER_VIOLATION.load(Ordering::Relaxed)
}

struct MemoryMemberOracleGuard;

impl MemoryMemberOracleGuard {
    fn enable() -> Self {
        use std::sync::atomic::Ordering;
        MEMORY_MEMBER_VIOLATION.store(false, Ordering::Relaxed);
        MEMORY_MEMBER_ORACLE_ENABLED.store(true, Ordering::Relaxed);
        Self
    }
}

impl Drop for MemoryMemberOracleGuard {
    fn drop(&mut self) {
        use std::sync::atomic::Ordering;
        MEMORY_MEMBER_ORACLE_ENABLED.store(false, Ordering::Relaxed);
        MEMORY_MEMBER_VIOLATION.store(false, Ordering::Relaxed);
    }
}

/// # Safety
/// Called by SanitizerCoverage module constructors with their guard range.
#[no_mangle]
pub unsafe extern "C" fn __sanitizer_cov_trace_pc_guard_init(mut start: *mut u32, stop: *mut u32) {
    use std::sync::atomic::Ordering;
    if start == stop || unsafe { *start } != 0 {
        return;
    }
    while start < stop {
        let guard = EDGES_ASSIGNED.fetch_add(1, Ordering::Relaxed) + 1;
        if guard as usize >= MAP_SIZE {
            eprintln!("[inprocess] target has more than {MAP_SIZE} coverage guards");
            std::process::abort();
        }
        unsafe {
            *start = guard;
            start = start.add(1);
        }
    }
}

/// # Safety
/// Called by instrumented code with a guard initialized above.
#[no_mangle]
pub unsafe extern "C" fn __sanitizer_cov_trace_pc_guard(guard: *mut u32) {
    // afl-compiler-rt.o 4.09c increments without NeverZero: 256 hits read 0.
    unsafe {
        let slot = EDGES_BUFFER.as_mut_ptr().add(*guard as usize);
        *slot = (*slot).wrapping_add(1);
    }
}

// The fuzzer process links ASan for the target, which would also serve every
// engine allocation, recording a stack trace per malloc and quarantining
// frees. Engine memory instead comes from glibc's own entry points, which
// ASan does not intercept (it does intercept __libc_memalign, so larger
// alignments are made by over-allocating). Target and harness memory still
// come from ASan's allocator.
extern "C" {
    fn __libc_malloc(size: usize) -> *mut std::ffi::c_void;
    fn __libc_calloc(count: usize, size: usize) -> *mut std::ffi::c_void;
    fn __libc_realloc(pointer: *mut std::ffi::c_void, size: usize) -> *mut std::ffi::c_void;
    fn __libc_free(pointer: *mut std::ffi::c_void);
}

// glibc's malloc alignment on x86-64.
const LIBC_MALLOC_ALIGN: usize = 16;

struct EngineAllocator;

unsafe impl std::alloc::GlobalAlloc for EngineAllocator {
    unsafe fn alloc(&self, layout: std::alloc::Layout) -> *mut u8 {
        unsafe {
            if layout.align() <= LIBC_MALLOC_ALIGN {
                return __libc_malloc(layout.size()).cast();
            }
            // Room to align, plus the original pointer just before the block.
            let raw = __libc_malloc(layout.size() + layout.align() + size_of::<usize>()).cast::<u8>();
            if raw.is_null() {
                return raw;
            }
            let start = raw as usize + size_of::<usize>();
            let aligned = (start + layout.align() - 1) & !(layout.align() - 1);
            *((aligned - size_of::<usize>()) as *mut usize) = raw as usize;
            aligned as *mut u8
        }
    }

    unsafe fn alloc_zeroed(&self, layout: std::alloc::Layout) -> *mut u8 {
        unsafe {
            if layout.align() <= LIBC_MALLOC_ALIGN {
                return __libc_calloc(1, layout.size()).cast();
            }
            let pointer = self.alloc(layout);
            if !pointer.is_null() {
                std::ptr::write_bytes(pointer, 0, layout.size());
            }
            pointer
        }
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: std::alloc::Layout) {
        unsafe {
            if layout.align() <= LIBC_MALLOC_ALIGN {
                __libc_free(pointer.cast());
            } else {
                __libc_free(*((pointer as usize - size_of::<usize>()) as *const usize) as *mut std::ffi::c_void);
            }
        }
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: std::alloc::Layout, new_size: usize) -> *mut u8 {
        unsafe {
            if layout.align() <= LIBC_MALLOC_ALIGN {
                return __libc_realloc(pointer.cast(), new_size).cast();
            }
            let grown = self.alloc(std::alloc::Layout::from_size_align_unchecked(new_size, layout.align()));
            if !grown.is_null() {
                std::ptr::copy_nonoverlapping(pointer, grown, layout.size().min(new_size));
                self.dealloc(pointer, layout);
            }
            grown
        }
    }
}

#[global_allocator]
static ENGINE_ALLOCATOR: EngineAllocator = EngineAllocator;

extern "C" {
    fn semantist_harness_execute(data: *const u8, len: usize) -> std::ffi::c_int;
    fn __sanitizer_set_report_path(path: *const std::ffi::c_char);
}

// Sanitizer defaults that the forkserver engine puts in the target's
// environment; a fuzzer process reads its options before main runs.
#[no_mangle]
extern "C" fn __asan_default_options() -> *const std::ffi::c_char {
    c"abort_on_error=1:symbolize=0:detect_leaks=0".as_ptr()
}

#[no_mangle]
extern "C" fn __ubsan_default_options() -> *const std::ffi::c_char {
    c"abort_on_error=1:symbolize=0:print_stacktrace=1:halt_on_error=1".as_ptr()
}

// The map size the forkserver reports for the same guards (afl-compiler-rt
// reports __afl_final_loc + 1, and LibAFL rounds it up to 64).
fn inprocess_edge_map_size() -> usize {
    let final_loc = EDGES_ASSIGNED.load(std::sync::atomic::Ordering::Relaxed) as usize;
    ((final_loc + 1 + 63) / 64 * 64).min(MAP_SIZE)
}

// The in-process campaign over a given event manager and, after a restart,
// the restored state.
macro_rules! inprocess_campaign {
    ($setup:expr, $scheduler:expr, $mgr:expr, $restored:expr, $edges:expr, $semantic:expr, $telemetry:expr) => {{
        let setup: &CampaignSetup = $setup;
        let config = &setup.config;
        let mut mgr = $mgr;
        let restored = $restored;
        let fresh = restored.is_none();
        let covered = restored.as_ref()
            .and_then(|state: &_| HasMetadata::metadata_map(state).get::<SemanticRuntimeMetadata>())
            .map(|runtime| runtime.covered_target_ids.clone());
        let (time_observer, edges_observer, semantic_observer, telemetry_observer, mut feedback, mut objective) =
            semantist_feedbacks!(setup, $edges, $semantic, $telemetry, covered.as_ref());
        let handles = (edges_observer.handle(), semantic_observer.handle(), telemetry_observer.handle());
        let mut state = match restored {
            Some(state) => state,
            None => semantist_new_state!(setup, &mut feedback, &mut objective),
        };
        let mut fuzzer = StdFuzzer::builder()
            .scheduler($scheduler)
            .feedback(feedback)
            .objective(objective)
            .build();

        let mut harness = |input: &BytesInput| {
            let bytes: &[u8] = input.as_ref();
            unsafe { semantist_harness_execute(bytes.as_ptr(), bytes.len()) };
            ExitKind::Ok
        };
        let executor = libafl::executors::inprocess::InProcessExecutor::with_timeout(
            &mut harness,
            tuple_list!(
                PhaseMarkerObserver::opening(),
                time_observer,
                edges_observer,
                semantic_observer,
                telemetry_observer,
                PhaseMarkerObserver::closing()
            ),
            &mut fuzzer,
            &mut state,
            &mut mgr,
            config.timeout,
        )?;
        let mut executor = ProfiledExecutor { inner: executor };
        // No AFL++ compiler pass runs, so there are no autotokens.
        let tokens = Tokens::new();
        semantist_campaign!(setup, fuzzer, executor, state, mgr, handles, tokens, fresh, "in-process")
    }};
}


/// Semantic map snapshot used by V3GoalStage after an execution.
fn v3_semantic_snapshot() -> Vec<u8> {
    let size = __semantist_semantic_buffer_size.load(std::sync::atomic::Ordering::Relaxed);
    unsafe { std::slice::from_raw_parts(__semantist_semantic_buffer.as_mut_ptr(), size).to_vec() }
}

fn v3_preflight_hazard(kind: Option<&str>, modeling: Option<&str>) -> bool {
    kind == Some("hazard_violation") && modeling == Some("exact")
}

#[cfg(test)]
#[test]
fn preflight_only_blocks_replayable_exact_violations() {
    assert!(v3_preflight_hazard(Some("hazard_violation"), Some("exact")));
    assert!(!v3_preflight_hazard(Some("hazard_violation"), Some("conservative")));
    assert!(!v3_preflight_hazard(Some("hazard_reach"), Some("exact")));
}

fn v3_clear_maps() {
    let size = __semantist_semantic_buffer_size.load(std::sync::atomic::Ordering::Relaxed);
    unsafe {
        std::ptr::write_bytes(EDGES_BUFFER.as_mut_ptr(), 0, inprocess_edge_map_size());
        std::ptr::write_bytes(__semantist_semantic_buffer.as_mut_ptr(), 0, size);
    }
}

// Native cold execution keeps fatal target faults out of the long-lived
// in-process executor. The journal is also the exact input used for replay.
fn v3_hazard_hit(trace: &std::path::Path, hazard_ids: &std::collections::HashSet<u32>) -> Result<bool, Error> {
    use std::io::BufRead;
    let file = match std::fs::File::open(trace) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(Error::illegal_argument(format!("v3 preflight trace: {error}"))),
    };
    for line in std::io::BufReader::new(file).lines() {
        let line = line.map_err(|error| Error::illegal_argument(format!("v3 preflight trace: {error}")))?;
        let record: serde_json::Value = serde_json::from_str(&line)
            .map_err(|error| Error::illegal_argument(format!("v3 preflight trace JSON: {error}")))?;
        let runtime_id = record.get("runtime_id").and_then(serde_json::Value::as_u64)
            .ok_or_else(|| Error::illegal_argument("v3 preflight trace missing runtime_id"))?;
        if u32::try_from(runtime_id).ok().is_some_and(|id| hazard_ids.contains(&id)) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn v3_cold_preflight(target: &std::path::Path, input: &std::path::Path, timeout: std::time::Duration,
    hazard_ids: &std::collections::HashSet<u32>)
    -> Result<Option<libafl::executors::ExitKind>, Error>
{
    use std::process::{Command, Stdio};
    let trace = input.with_extension("preflight.hits.jsonl");
    let _ = std::fs::remove_file(&trace);
    let mut command = Command::new(target);
    command.arg(input)
        .env("ASAN_OPTIONS", "halt_on_error=1:abort_on_error=1")
        .env("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1")
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if !hazard_ids.is_empty() {
        command.env("SEMANTIST_SEMANTIC_TRACE_RUNTIME", "1")
            .env("SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE", &trace);
    }
    let mut child = command
        .spawn()
        .map_err(|error| Error::illegal_argument(format!("v3 cold preflight launch: {error}")))?;
    let started = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let hazard = status.success() && v3_hazard_hit(&trace, hazard_ids)?;
                let _ = std::fs::remove_file(&trace);
                return Ok((!status.success() || hazard).then_some(libafl::executors::ExitKind::Crash));
            }
            Ok(None) if started.elapsed() >= timeout => {
                child.kill().ok();
                child.wait().ok();
                let _ = std::fs::remove_file(&trace);
                return Ok(Some(libafl::executors::ExitKind::Timeout));
            }
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(1)),
            Err(error) => {
                child.kill().ok();
                child.wait().ok();
                let _ = std::fs::remove_file(&trace);
                return Err(Error::illegal_argument(format!("v3 cold preflight wait: {error}")));
            }
        }
    }
}

fn run_v3_inprocess(setup: CampaignSetup) -> Result<(), Error> {
    use libafl::events::{SimpleEventManager, SendExiting};
    use libafl_bolts::rands::StdRand;
    use libafl::schedulers::QueueScheduler;
    use std::sync::{Arc, Mutex};

    let config = &setup.config;
    let run_dir = config
        .run_dir
        .clone()
        .ok_or_else(|| Error::illegal_argument("v3 mode requires --run-dir"))?;
    let bundle_dir = config
        .v3_bundle
        .clone()
        .unwrap_or_else(|| run_dir.join("bundle"));
    let bundle = V3Bundle::load(&bundle_dir, &config.target).map_err(Error::illegal_argument)?;
    let hazard_ids = bundle.tasks.tasks.iter()
        .filter(|task| v3_preflight_hazard(task.goal.kind.as_deref(),
                                           task.goal.modeling.as_deref()))
        .filter_map(|task| task.goal.runtime_id)
        .collect::<std::collections::HashSet<_>>();
    if setup.semantic_map_size > SEMANTIC_BUFFER_CAPACITY {
        return Err(Error::illegal_argument("semantic map exceeds in-process buffer"));
    }
    let _memory_member_oracle = MemoryMemberOracleGuard::enable();
    unsafe { __asan_set_error_report_callback(v3_asan_report) };
    __semantist_semantic_buffer_size.store(setup.semantic_map_size, std::sync::atomic::Ordering::Relaxed);
    let (edges, semantic_map, telemetry) = unsafe {
        (
            std::slice::from_raw_parts_mut(EDGES_BUFFER.as_mut_ptr(), inprocess_edge_map_size()),
            std::slice::from_raw_parts_mut(__semantist_semantic_buffer.as_mut_ptr(), setup.semantic_map_size),
            std::slice::from_raw_parts_mut(__semantist_telemetry_buffer.as_mut_ptr(), TELEMETRY_SIZE),
        )
    };
    let (time_observer, edges_observer, semantic_observer, telemetry_observer, mut feedback, mut objective) =
        semantist_feedbacks!(&setup, edges, semantic_map, telemetry, None);
    let goal_observer = GoalStateObserver::new(&bundle);
    let goal_handle = goal_observer.handle();
    let mut state = semantist_new_state!(&setup, &mut feedback, &mut objective);
    let mut fuzzer = StdFuzzer::builder()
        .scheduler(QueueScheduler::new())
        .feedback(feedback)
        .objective(objective)
        .build();
    let cache = Arc::new(Mutex::new(PrefixCache::new(!config.v3_no_snapshot)));
    let current: Arc<Mutex<Option<Vec<u8>>>> = Arc::new(Mutex::new(None));
    let outcome: Arc<Mutex<Option<ExecOutcome>>> = Arc::new(Mutex::new(None));
    let harness_current = current.clone();
    let harness_cache = cache.clone();
    let harness_outcome = outcome.clone();
    let harness_bundle = bundle.clone();
    let mut harness = move |_input: &BytesInput| {
        let data = harness_current
            .lock()
            .expect("v3 current")
            .take()
            .unwrap_or_default();
        v3_clear_maps();
        memory_member_oracle_reset();
        ASAN_VIOLATION.store(false, std::sync::atomic::Ordering::Relaxed);
        let mut cache = harness_cache.lock().expect("v3 cache");
        match execute_with_cache(&data, &harness_bundle, &mut cache) {
            Ok(executed) => {
                let member_violation = memory_member_oracle_seen();
                let asan_violation = ASAN_VIOLATION.load(std::sync::atomic::Ordering::Relaxed);
                *harness_outcome.lock().expect("v3 outcome") = Some(executed);
                if member_violation || asan_violation { ExitKind::Crash } else { ExitKind::Ok }
            },
            Err(error) => {
                eprintln!("[v3] execution error: {error}");
                cache.clear();
                ExitKind::Crash
            }
        }
    };
    let mut manager = SimpleEventManager::new(SimpleMonitor::new(|s| println!("{s}")));
    let executor = libafl::executors::inprocess::InProcessExecutor::with_timeout(
        &mut harness,
        tuple_list!(
            PhaseMarkerObserver::opening(),
            goal_observer,
            time_observer,
            edges_observer,
            semantic_observer,
            telemetry_observer,
            PhaseMarkerObserver::closing()
        ),
        &mut fuzzer,
        &mut state,
        &mut manager,
        config.timeout.max(std::time::Duration::from_secs(5)),
    )?;
    let mut executor = PrefixCachingExecutor::new(executor, current, cache.clone(), outcome);
    let (literals, floats) = collect_literals(&bundle);
    let mut stage = V3GoalStage::new(
        bundle.clone(),
        run_dir.clone(),
        &config.seed_dir,
        literals,
        floats,
        config.rng_seed.unwrap_or_else(current_nanos),
        config.v3_no_distance,
        config.v3_no_goal_prefix,
        config.v3_no_time_guidance,
        config.v3_no_state_copy,
        config.v3_no_snapshot,
    );
    let budget = config.v3_time_budget;
    let pending_path = run_dir.join("metrics").join("engine-pending.txt");
    println!("[v3] in-process engine: {} tasks, budget {:?}", bundle.tasks.tasks.len(), budget);
    let started = std::time::Instant::now();
    while started.elapsed() < budget && (config.v3_full_duration
        || config.fuzz_iterations.is_none_or(|limit| stage.stats.executions < limit)) {
        let scheduled_task = stage.select_task();
        if !config.v3_full_duration && !bundle.tasks.tasks.is_empty() && scheduled_task.is_none() { break; }
        let bytes = stage.next_input();
        let task = stage.current_solver_task_id().map(str::to_owned).or(scheduled_task);
        std::fs::write(&pending_path, &bytes)
            .map_err(|error| Error::illegal_argument(format!("pending input journal: {error}")))?;
        if let Some(exit_kind) = v3_cold_preflight(&config.target, &pending_path, config.timeout, &hazard_ids)? {
            stage.on_result(&exit_kind, &[], f64::INFINITY, None, task.as_deref(),
                std::collections::BTreeMap::new(), None, &[]).map_err(Error::illegal_argument)?;
            let _ = std::fs::remove_file(&pending_path);
            cache.lock().expect("v3 cache").clear();
            continue;
        }
        let input = BytesInput::new(bytes);
        let exit_kind = fuzzer.execute_input(&mut state, &mut executor, &mut manager, &input)?;
        let semantic = v3_semantic_snapshot();
        let mut executed = executor.take_outcome();
        if let Some(ref mut outcome) = executed {
            outcome.exit_kind = match exit_kind {
                libafl::executors::ExitKind::Ok => "Ok",
                libafl::executors::ExitKind::Crash => "Crash",
                libafl::executors::ExitKind::Timeout => "Timeout",
                _ => "Other",
            }.to_string();
        }
        let mut observers = executor.observers_mut();
        let observer = &mut observers[&goal_handle];
        if let Some(ref executed) = executed {
            observer.observe_trace_with_frames(executed,
                stage.current_input_frames().unwrap_or(&[]), &bundle.abi.action_fields());
        }
        stage.record_execution(task.as_deref(), executed.as_ref());
        let distance = task
            .as_deref()
            .map(|task_id| observer.distance(task_id))
            .unwrap_or(f64::INFINITY);
        let state_values = observer.values();
        let best_frame = task.as_deref().and_then(|task_id| observer.best_frame(task_id));
        stage.on_result(&exit_kind, &semantic, distance, best_frame, task.as_deref(), state_values, executed.as_ref(), observer.frame_feedback())
            .map_err(Error::illegal_argument)?;
        let _ = std::fs::remove_file(&pending_path);
        let (hits, saved, _, attempts, validations, invalidations) = executor.cache_stats();
        stage.record_cache(hits, saved, attempts, validations, invalidations);
        if !config.v3_full_duration && stage.all_goals_reached() { break; }
    }
    stage.ended = if !config.v3_full_duration && stage.all_goals_reached() {
        "all_goals_reached".to_string()
    } else if config.fuzz_iterations.is_some_and(|limit| stage.stats.executions >= limit) {
        "evaluations".to_string()
    } else {
        "budget".to_string()
    };
    near_budget(&stage, started, budget);
    stage.write_summary(started.elapsed(), budget).map_err(Error::illegal_argument)?;
    manager.send_exiting()?;
    println!("[v3] ended {}: executions={} scans={}", stage.ended, stage.stats.executions, stage.stats.scans);
    Ok(())
}

fn near_budget(_stage: &V3GoalStage, _started: std::time::Instant, _budget: std::time::Duration) {}

fn run_inprocess() -> Result<(), Error> {
    use libafl::events::SimpleRestartingEventManager;
    use std::{os::unix::ffi::OsStrExt, sync::atomic::Ordering};

    // Set before launch(): every restarted client is forked from this
    // process, so the start time (time to exposure) stays continuous.
    let started = std::time::Instant::now();
    let mut setup = prepare_campaign(Config::from_args()?, started)?;
    if setup.config.v3 {
        return run_v3_inprocess(setup);
    }
    // __ubsan_default_options halts unless UBSAN_OPTIONS overrides it.
    setup.sanitizer_reports_end_execution = sanitizer_env_flag("UBSAN_OPTIONS", "halt_on_error") != Some(false);
    let config = &setup.config;
    if env::var("ASAN_OPTIONS").map_or(true, |options| !options.contains("log_path")) {
        // Same log directory and file prefix the forkserver engine configures.
        let path = std::ffi::CString::new(setup.sanitizer_log_dir.join("asan").as_os_str().as_bytes())
            .map_err(|error| Error::illegal_argument(format!("sanitizer log path: {error}")))?;
        unsafe { __sanitizer_set_report_path(path.as_ptr()) };
    }
    if setup.semantic_map_size > SEMANTIC_BUFFER_CAPACITY {
        return Err(Error::illegal_argument(format!(
            "semantic map of {} entries exceeds the in-process buffer of {SEMANTIC_BUFFER_CAPACITY}",
            setup.semantic_map_size
        )));
    }
    __semantist_semantic_buffer_size.store(setup.semantic_map_size, Ordering::Relaxed);
    // The target and the observers share these buffers; executions never
    // overlap with observer access.
    let (edges, semantic_map, telemetry) = unsafe {
        (
            std::slice::from_raw_parts_mut(EDGES_BUFFER.as_mut_ptr(), inprocess_edge_map_size()),
            std::slice::from_raw_parts_mut(__semantist_semantic_buffer.as_mut_ptr(), setup.semantic_map_size),
            std::slice::from_raw_parts_mut(__semantist_telemetry_buffer.as_mut_ptr(), TELEMETRY_SIZE),
        )
    };
    // Loaded once here, so restarted clients inherit earlier runs' records.
    let scheduler = TargetAwareScheduler::new(config.metadata_bootstrap.clone(), config.ablation);

    // A consistency dump runs its inputs once; a crash simply ends it. A
    // campaign uses the restarting manager: a crash or timeout ends the
    // client, and the restarter forks a new one that resumes from the state
    // the crash handler saved.
    let monitor = SimpleMonitor::new(|s| println!("{s}"));
    if config.consistency_dump.is_some() {
        let mgr = libafl::events::SimpleEventManager::new(monitor);
        return inprocess_campaign!(&setup, scheduler, mgr, None, edges, semantic_map, telemetry);
    }
    let mut shmem_provider = UnixShMemProvider::new()?;
    let (restored, mgr) = match SimpleRestartingEventManager::launch(monitor, &mut shmem_provider) {
        Ok(launched) => launched,
        Err(Error::ShuttingDown) => return Ok(()),
        Err(error) => return Err(error),
    };
    inprocess_campaign!(&setup, scheduler, mgr, restored, edges, semantic_map, telemetry)
}

/// C entry point of a per-target in-process fuzzer; the harness defines no
/// main when built with SEMANTIST_IN_PROCESS.
#[cfg(not(test))]
#[export_name = "main"]
pub extern "C" fn semantist_inprocess_main(_argc: std::ffi::c_int, _argv: *const *const std::ffi::c_char) -> std::ffi::c_int {
    match run_inprocess() {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("Error: {error:?}");
            1
        }
    }
}
