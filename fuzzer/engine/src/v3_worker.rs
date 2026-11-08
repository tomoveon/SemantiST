use std::os::unix::net::UnixStream;

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
struct V3WorkerCacheStats {
    hits: u64,
    saved_scans: u64,
    restore_attempts: u64,
    validations: u64,
    invalidations: u64,
}

impl From<&PrefixCache> for V3WorkerCacheStats {
    fn from(cache: &PrefixCache) -> Self {
        Self {
            hits: cache.hits,
            saved_scans: cache.saved_scans,
            restore_attempts: cache.restore_attempts,
            validations: cache.validations,
            invalidations: cache.invalidations,
        }
    }
}

#[derive(Deserialize, Serialize)]
struct V3WorkerResponse {
    outcome: Option<ExecOutcome>,
    error: Option<String>,
    crashed: bool,
    parser_rejected: bool,
    edges: Vec<(u16, u8)>,
    semantic: Vec<u8>,
    telemetry: Vec<u8>,
    cache: V3WorkerCacheStats,
}

struct V3Worker {
    pid: libc::pid_t,
    stream: UnixStream,
    last_cache: V3WorkerCacheStats,
}

fn v3_worker_read(stream: &mut UnixStream) -> std::io::Result<Vec<u8>> {
    let mut header = [0u8; 4];
    stream.read_exact(&mut header)?;
    let length = u32::from_le_bytes(header) as usize;
    if length > 32 * 1024 * 1024 {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "worker message too large"));
    }
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes)?;
    Ok(bytes)
}

fn v3_worker_write(stream: &mut UnixStream, bytes: &[u8]) -> std::io::Result<()> {
    let length = u32::try_from(bytes.len()).map_err(|_| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "worker message too large")
    })?;
    stream.write_all(&length.to_le_bytes())?;
    stream.write_all(bytes)
}

fn v3_worker_loop(mut stream: UnixStream, bundle: &V3Bundle, cache_enabled: bool,
                  semantic_size: usize, hazard_ids: &std::collections::HashSet<u32>) -> ! {
    // The supervisor's LibAFL signal handlers must not handle a target fault.
    unsafe {
        for signal in [libc::SIGSEGV, libc::SIGBUS, libc::SIGABRT, libc::SIGILL,
                       libc::SIGFPE, libc::SIGALRM] {
            libc::signal(signal, libc::SIG_DFL);
        }
    }
    let mut cache = PrefixCache::new(cache_enabled);
    while let Ok(bytes) = v3_worker_read(&mut stream) {
        v3_clear_maps();
        unsafe { std::ptr::write_bytes(__semantist_telemetry_buffer.as_mut_ptr(), 0, TELEMETRY_SIZE) };
        memory_member_oracle_reset();
        ASAN_VIOLATION.store(false, std::sync::atomic::Ordering::Relaxed);
        let result = execute_with_cache(&bytes, bundle, &mut cache);
        let error = result.as_ref().err().cloned();
        let semantic = v3_semantic_snapshot();
        debug_assert_eq!(semantic.len(), semantic_size);
        let parser_rejected = error.as_deref().is_some_and(|value| value.starts_with("parser reject:"));
        let crashed = error.is_some() && !parser_rejected
            || memory_member_oracle_seen()
            || ASAN_VIOLATION.load(std::sync::atomic::Ordering::Relaxed)
            || hazard_ids.iter().any(|id| semantic.get(*id as usize).copied().unwrap_or(0) > 0);
        let mut outcome = result.ok();
        if let Some(ref mut executed) = outcome {
            let size = unsafe { semantist_state_size() };
            executed.final_state.resize(size, 0);
            if size != 0 {
                unsafe { semantist_checkpoint(executed.final_state.as_mut_ptr()) };
            }
        }
        let response = V3WorkerResponse {
            outcome,
            error,
            crashed,
            parser_rejected,
            edges: unsafe {
                std::slice::from_raw_parts(EDGES_BUFFER.as_mut_ptr(), inprocess_edge_map_size())
                    .iter().enumerate().filter(|(_, value)| **value != 0)
                    .map(|(index, value)| (index as u16, *value)).collect()
            },
            semantic,
            telemetry: unsafe {
                std::slice::from_raw_parts(__semantist_telemetry_buffer.as_mut_ptr(), TELEMETRY_SIZE).to_vec()
            },
            cache: V3WorkerCacheStats::from(&cache),
        };
        let encoded = match bincode::serde::encode_to_vec(&response, bincode::config::standard()) {
            Ok(encoded) => encoded,
            Err(_) => break,
        };
        if v3_worker_write(&mut stream, &encoded).is_err() { break; }
        if crashed { cache.clear(); }
        // A crash candidate invalidates all exported state before the next input.
        // The parent removes the index after it observes ExitKind::Crash.
    }
    unsafe { libc::_exit(0) }
}

impl V3Worker {
    fn spawn(bundle: &V3Bundle, cache_enabled: bool, semantic_size: usize,
             hazard_ids: &std::collections::HashSet<u32>, timeout: std::time::Duration)
             -> std::io::Result<Self> {
        let (parent, child) = UnixStream::pair()?;
        let pid = unsafe { libc::fork() };
        if pid < 0 { return Err(std::io::Error::last_os_error()); }
        if pid == 0 {
            drop(parent);
            v3_worker_loop(child, bundle, cache_enabled, semantic_size, hazard_ids);
        }
        drop(child);
        parent.set_read_timeout(Some(timeout))?;
        parent.set_write_timeout(Some(timeout))?;
        Ok(Self { pid, stream: parent, last_cache: V3WorkerCacheStats::default() })
    }

    fn execute(&mut self, bytes: &[u8]) -> std::io::Result<V3WorkerResponse> {
        v3_worker_write(&mut self.stream, bytes)?;
        let encoded = v3_worker_read(&mut self.stream)?;
        bincode::serde::decode_from_slice::<V3WorkerResponse, _>(&encoded, bincode::config::standard())
            .map(|(response, _)| response)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    }

    fn record_cache(&mut self, cache: &mut PrefixCache, current: V3WorkerCacheStats) {
        cache.hits += current.hits.saturating_sub(self.last_cache.hits);
        cache.saved_scans += current.saved_scans.saturating_sub(self.last_cache.saved_scans);
        cache.restore_attempts += current.restore_attempts.saturating_sub(self.last_cache.restore_attempts);
        cache.validations += current.validations.saturating_sub(self.last_cache.validations);
        cache.invalidations += current.invalidations.saturating_sub(self.last_cache.invalidations);
        self.last_cache = current;
    }
}

impl Drop for V3Worker {
    fn drop(&mut self) {
        unsafe {
            libc::kill(self.pid, libc::SIGKILL);
            libc::waitpid(self.pid, std::ptr::null_mut(), 0);
        }
    }
}
