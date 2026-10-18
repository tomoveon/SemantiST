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

fn run_inprocess() -> Result<(), Error> {
    use libafl::events::SimpleRestartingEventManager;
    use std::{os::unix::ffi::OsStrExt, sync::atomic::Ordering};

    // Set before launch(): every restarted client is forked from this
    // process, so the start time (time to exposure) stays continuous.
    let started = std::time::Instant::now();
    let setup = prepare_campaign(Config::from_args()?, started)?;
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
