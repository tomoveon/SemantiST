// PrefixCachingExecutor wraps a LibAFL InProcessExecutor.  The wrapper owns the
// current canonical input and the engine-side snapshot cache; the wrapped
// harness calls back into `execute_with_cache`.  No target-specific code or
// Python process is involved in the hot path.

use libafl::executors::Executor;
use std::sync::{Arc, Mutex};

pub struct PrefixCachingExecutor<E> {
    inner: E,
    current: Arc<Mutex<Option<Vec<u8>>>>,
    cache: Arc<Mutex<PrefixCache>>,
    outcome: Arc<Mutex<Option<ExecOutcome>>>,
}

impl<E: HasObservers> HasObservers for PrefixCachingExecutor<E> {
    type Observers = E::Observers;

    fn observers(&self) -> libafl_bolts::tuples::RefIndexable<&Self::Observers, Self::Observers> {
        self.inner.observers()
    }

    fn observers_mut(&mut self) -> libafl_bolts::tuples::RefIndexable<&mut Self::Observers, Self::Observers> {
        self.inner.observers_mut()
    }
}

impl<E> PrefixCachingExecutor<E> {
    pub fn new(inner: E, current: Arc<Mutex<Option<Vec<u8>>>>, cache: Arc<Mutex<PrefixCache>>,
               outcome: Arc<Mutex<Option<ExecOutcome>>>) -> Self {
        Self { inner, current, cache, outcome }
    }

    pub fn cache_stats(&self) -> (u64, u64, usize, u64, u64, u64) {
        let cache = self.cache.lock().expect("prefix cache");
        (cache.hits, cache.saved_scans, cache.len(), cache.restore_attempts,
         cache.validations, cache.invalidations)
    }

    pub fn cache(&self) -> Arc<Mutex<PrefixCache>> {
        self.cache.clone()
    }

    pub fn current(&self) -> Arc<Mutex<Option<Vec<u8>>>> {
        self.current.clone()
    }

    pub fn take_outcome(&self) -> Option<ExecOutcome> {
        self.outcome.lock().expect("v3 outcome").take()
    }
}

impl<E, EM, I, S, Z> Executor<EM, I, S, Z> for PrefixCachingExecutor<E>
where
    E: Executor<EM, I, S, Z>,
    I: AsRef<Vec<u8>>,
{
    fn run_target(&mut self, fuzzer: &mut Z, state: &mut S, mgr: &mut EM, input: &I) -> Result<ExitKind, Error> {
        *self.current.lock().expect("v3 current") = Some(input.as_ref().clone());
        *self.outcome.lock().expect("v3 outcome") = None;
        let result = self.inner.run_target(fuzzer, state, mgr, input);
        if !matches!(result, Ok(ExitKind::Ok)) {
            let mut cache = self.cache.lock().expect("prefix cache");
            cache.invalidations += 1;
            cache.clear();
        }
        result
    }
}
