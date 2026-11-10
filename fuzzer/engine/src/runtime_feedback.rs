fn is_valid_structured_seed(bytes: &[u8]) -> bool {
    let Ok(text) = std::str::from_utf8(bytes) else {
        return false;
    };
    let mut found_record = false;
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let mut parts = line.splitn(3, ',');
        let Some(name) = parts.next() else {
            return false;
        };
        let Some(ty) = parts.next() else {
            return false;
        };
        if parts.next().is_none() || name.trim().is_empty() || ty.trim().is_empty() {
            return false;
        }
        found_record = true;
    }
    found_record
}

fn infer_seed_cycle_metadata(bytes: &[u8]) -> SeedCycleMetadata {
    let mut cycle_ids = bytes
        .split(|byte| *byte == b'\n')
        .filter_map(|line| std::str::from_utf8(line).ok())
        .filter_map(|line| line.split_once(',').map(|(name, _)| name.trim()))
        .filter_map(|name| name.split_once('.'))
        .filter_map(|(prefix, rest)| {
            (!rest.is_empty() && prefix.chars().all(|ch| ch.is_ascii_digit()))
                .then(|| prefix.parse::<usize>().ok())
                .flatten()
        })
        .collect::<Vec<_>>();
    cycle_ids.sort_unstable();
    cycle_ids.dedup();
    SeedCycleMetadata {
        target_kind: if cycle_ids.is_empty() {
            "FUNCTION"
        } else {
            "FUNCTION_BLOCK"
        }
        .to_string(),
        cycle_count: cycle_ids.len(),
        cycle_ids,
    }
}

fn read_fb_state_trace_metadata() -> TraceCycleMetadata {
    let Ok(path) = env::var("SEMANTIST_FB_STATE_TRACE_FILE") else {
        return TraceCycleMetadata::default();
    };
    let Ok(contents) = fs::read_to_string(path) else {
        return TraceCycleMetadata::default();
    };
    let mut out = TraceCycleMetadata::default();
    for line in contents.lines() {
        let mut cycle = None;
        let mut before = None;
        let mut after = None;
        let mut changed = false;
        let mut stale = false;
        for item in line.split_whitespace() {
            let Some((key, value)) = item.split_once('=') else {
                continue;
            };
            match key {
                "cycle" => cycle = value.parse::<i32>().ok(),
                "state_hash_before" => before = Some(value.to_string()),
                "state_hash_after" => after = Some(value.to_string()),
                "changed" => changed = value != "0",
                "stale" => stale = value != "0",
                _ => {}
            }
        }
        let Some(cycle) = cycle else {
            continue;
        };
        if stale {
            out.stale_cycles.push(cycle);
        }
        if changed {
            out.last_changed_cycle = Some(cycle);
        }
        out.state_hashes.push(CycleStateHash {
            cycle,
            state_hash_before: before.unwrap_or_default(),
            state_hash_after: after.unwrap_or_default(),
            changed,
            stale,
        });
    }
    out
}

// Exact STG violations newly reached by the current execution. The semantic
// objective fills it; RuntimeFindingFeedback, which sees the input, persists it.
type PendingSemanticFindings = std::rc::Rc<std::cell::RefCell<Vec<RuntimeTargetEntry>>>;

// Crash signatures already reported and sanitizer logs already claimed.
// Between executions they live in the fuzzer state, so an in-process client
// restarted after a crash neither reports a signature twice nor re-reads logs.
#[derive(Clone, Debug, Default, Deserialize, SerdeAny, Serialize)]
struct RuntimeFindingRecords {
    signatures: HashMap<String, u64>,
    seen_sanitizer_logs: HashSet<String>,
}

#[derive(Debug)]
struct RuntimeFindingFeedback {
    name: Cow<'static, str>,
    findings_dir: PathBuf,
    target_corpus_dir: PathBuf,
    bootstrap_path: Option<PathBuf>,
    events_file: PathBuf,
    sanitizer_log_dir: PathBuf,
    started: std::time::Instant,
    records: RuntimeFindingRecords,
    records_loaded: bool,
    // Directory mtime at the last scan, and whether that scan left new logs
    // unclaimed; an unchanged, settled directory needs no scan.
    scanned_log_dir: Option<SystemTime>,
    unclaimed_logs: bool,
    // Every sanitizer report ends its execution, so only a crash or timeout
    // can leave a new log.
    logs_only_after_failures: bool,
    pending_semantic: PendingSemanticFindings,
}

// Directory mtimes come from the coarse kernel clock: two log files created a
// few milliseconds apart may leave one mtime, so a recent mtime is rescanned.
const SANITIZER_LOG_SETTLE: Duration = Duration::from_millis(50);

#[derive(Clone, Debug)]
struct SanitizerEvidence {
    kind: String,
    path: PathBuf,
    fingerprint: String,
    signature: String,
}

impl RuntimeFindingFeedback {
    fn new(
        findings_dir: PathBuf,
        target_corpus_dir: PathBuf,
        bootstrap_path: Option<PathBuf>,
        events_file: PathBuf,
        sanitizer_log_dir: PathBuf,
    ) -> Self {
        let seen_sanitizer_logs = existing_sanitizer_log_fingerprints(&sanitizer_log_dir);
        Self {
            name: Cow::Borrowed("RuntimeFindingFeedback"),
            findings_dir,
            target_corpus_dir,
            bootstrap_path,
            events_file,
            sanitizer_log_dir,
            started: std::time::Instant::now(),
            records: RuntimeFindingRecords {
                signatures: HashMap::new(),
                seen_sanitizer_logs,
            },
            records_loaded: false,
            scanned_log_dir: None,
            unclaimed_logs: true,
            logs_only_after_failures: false,
            pending_semantic: PendingSemanticFindings::default(),
        }
    }

    fn with_start(mut self, started: std::time::Instant) -> Self {
        self.started = started;
        self
    }

    fn with_logs_only_after_failures(mut self, logs_only_after_failures: bool) -> Self {
        self.logs_only_after_failures = logs_only_after_failures;
        self
    }

    fn record_findings<S>(
        &mut self,
        state: &mut S,
        input: &BytesInput,
        exit_kind: &ExitKind,
    ) -> Result<bool, Error>
    where
        S: HasCorpus<BytesInput> + HasExecutions + HasMetadata,
    {
        let semantic = std::mem::take(&mut *self.pending_semantic.borrow_mut());
        for entry in &semantic {
            self.persist_semantic_finding(state, input, entry)?;
        }
        let sanitizer_evidence = if self.sanitizer_logs_may_have_changed(exit_kind) {
            self.take_new_sanitizer_evidence()
        } else {
            None
        };
        if !exit_kind_is_finding(exit_kind) && sanitizer_evidence.is_none() {
            return Ok(false);
        }
        self.persist_finding(state, input, exit_kind, sanitizer_evidence.as_ref())?;
        Ok(true)
    }

    fn pending_semantic_findings(&self) -> PendingSemanticFindings {
        self.pending_semantic.clone()
    }

    // A sanitizer can report without ending the process (recoverable UBSan),
    // so the log directory is checked after every execution, by its mtime.
    fn sanitizer_logs_may_have_changed(&mut self, exit_kind: &ExitKind) -> bool {
        if self.logs_only_after_failures && !self.unclaimed_logs && !exit_kind_is_finding(exit_kind)
        {
            return false;
        }
        let modified = fs::metadata(&self.sanitizer_log_dir)
            .and_then(|metadata| metadata.modified())
            .ok();
        let settled = modified.is_some_and(|modified| {
            SystemTime::now()
                .duration_since(modified)
                .is_ok_and(|age| age >= SANITIZER_LOG_SETTLE)
        });
        let changed = self.unclaimed_logs
            || exit_kind_is_finding(exit_kind)
            || modified.is_none()
            || modified != self.scanned_log_dir
            || !settled;
        if changed {
            self.scanned_log_dir = modified;
        }
        changed
    }

    fn take_new_sanitizer_evidence(&mut self) -> Option<SanitizerEvidence> {
        self.unclaimed_logs = false;
        let entries = fs::read_dir(&self.sanitizer_log_dir).ok()?;
        let mut candidates = Vec::new();
        for entry in entries.flatten() {
            let path = entry.path();
            let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            if !sanitizer_log_name_is_candidate(file_name) {
                continue;
            };
            let Ok(metadata) = entry.metadata() else {
                continue;
            };
            if !metadata.is_file() || metadata.len() == 0 {
                continue;
            }
            let fingerprint = sanitizer_log_fingerprint(&path, &metadata);
            if self.records.seen_sanitizer_logs.contains(&fingerprint) {
                continue;
            }
            let Ok(contents) = fs::read(&path) else {
                continue;
            };
            let Some(kind) = sanitizer_log_kind(file_name, &contents) else {
                continue;
            };
            let modified = metadata
                .modified()
                .ok()
                .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
                .map(|duration| duration.as_nanos())
                .unwrap_or_default();
            let signature = sanitizer_signature(&kind, &String::from_utf8_lossy(&contents));
            candidates.push((
                modified,
                SanitizerEvidence {
                    kind,
                    fingerprint,
                    path,
                    signature,
                },
            ));
        }
        candidates.sort_by_key(|(modified, _)| *modified);
        let mut candidates = candidates.into_iter();
        for (_, evidence) in candidates.by_ref() {
            if self
                .records
                .seen_sanitizer_logs
                .insert(evidence.fingerprint.clone())
            {
                // One log per execution; the rest are claimed by later ones.
                self.unclaimed_logs = candidates.len() > 0;
                return Some(evidence);
            }
        }
        None
    }

    fn write_reproducer(
        &self,
        source_label: &str,
        input: &BytesInput,
    ) -> Result<(String, PathBuf, PathBuf), Error> {
        let finding_dir = self.findings_dir.join(source_label);
        fs::create_dir_all(&finding_dir)?;
        fs::create_dir_all(&self.target_corpus_dir)?;
        let next = fs::read_dir(&finding_dir)?
            .filter_map(Result::ok)
            .filter_map(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .strip_prefix("seed-")
                    .and_then(|rest| rest.strip_suffix(".seed"))
                    .and_then(|number| number.parse::<u64>().ok())
            })
            .max()
            .unwrap_or(0)
            + 1;
        let seed_id = format!("seed-{next:08}");
        let seed_path = finding_dir.join(format!("{seed_id}.seed"));
        fs::write(&seed_path, input.as_ref())?;
        Ok((seed_id, finding_dir, seed_path))
    }

    fn persist_semantic_finding<S>(
        &mut self,
        state: &S,
        input: &BytesInput,
        entry: &RuntimeTargetEntry,
    ) -> Result<(), Error>
    where
        S: HasCorpus<BytesInput> + HasExecutions + HasMetadata,
    {
        if !is_valid_structured_seed(input.as_ref()) {
            return Ok(());
        }
        let source_label = seed_source_label(&source_context(state)).to_string();
        let (_seed_id, finding_dir, seed_path) = self.write_reproducer(&source_label, input)?;
        let record = SemanticFindingRecord {
            schema_version: "1.0",
            kind: "semantic_violation",
            source: source_label,
            content_hash: content_hash(input.as_ref()),
            reproducer: seed_path.display().to_string(),
            target_id: entry.stable_id.clone(),
            target_kind: entry.kind.clone(),
            hazard: entry.hazard.clone(),
            modeling: entry.modeling.clone(),
            target_source: entry.source.clone(),
            executions: *state.executions(),
            elapsed_seconds: self.started.elapsed().as_secs_f64(),
            unix_time_seconds: unix_time_seconds(),
        };
        let file_stem = entry
            .stable_id
            .replace(|c: char| !c.is_ascii_alphanumeric(), "_");
        let serialized = serde_json::to_string_pretty(&record).map_err(|error| {
            Error::serialize(format!("unable to serialize semantic finding: {error}"))
        })?;
        fs::write(
            finding_dir.join(format!("semantic-{file_stem}.json")),
            format!("{serialized}\n"),
        )?;
        append_json_value(
            &self.events_file,
            &serde_json::json!({
                "event": "semantic_finding",
                "target_id": entry.stable_id,
                "hazard": entry.hazard,
                "reproducer": record.reproducer,
                "executions": record.executions,
                "elapsed_seconds": record.elapsed_seconds,
                "unix_time_seconds": record.unix_time_seconds,
            }),
        )
    }

    fn persist_finding<S>(
        &mut self,
        state: &S,
        input: &BytesInput,
        exit_kind: &ExitKind,
        sanitizer_evidence: Option<&SanitizerEvidence>,
    ) -> Result<(), Error>
    where
        S: HasCorpus<BytesInput> + HasExecutions + HasMetadata,
    {
        if !is_valid_structured_seed(input.as_ref()) {
            return Ok(());
        }
        let signature = sanitizer_evidence
            .map(|evidence| evidence.signature.clone())
            .unwrap_or_else(|| exit_kind_label(exit_kind).to_string());
        let occurrences = self
            .records
            .signatures
            .entry(signature.clone())
            .or_insert(0);
        *occurrences += 1;
        if *occurrences > 1 {
            return Ok(());
        }
        let source = source_context(state);
        let source_label = seed_source_label(&source).to_string();
        let kind = sanitizer_evidence
            .map(|evidence| evidence.kind.clone())
            .unwrap_or_else(|| exit_kind_label(exit_kind).to_string());
        let sanitizer_log = sanitizer_evidence.map(|evidence| evidence.path.display().to_string());
        let parent_id = parent_seed_id(state);
        let (seed_id, finding_dir, seed_path) = self.write_reproducer(&source_label, input)?;
        let digest = content_hash(input.as_ref());

        let detail = if let Some(path) = sanitizer_log.as_deref() {
            format!(
                "{} discovered {} while executing structured seed {}; sanitizer log: {}",
                source_label, kind, seed_id, path
            )
        } else {
            format!(
                "{} discovered {} while executing structured seed {}",
                source_label, kind, seed_id
            )
        };
        let mut metadata = StSeedMetadata::new(source.clone(), *state.corpus().current());
        metadata.content_hash = digest.clone();
        metadata.seed_id = seed_id.clone();
        metadata.parent_id = parent_id.clone();
        metadata.is_finding = true;
        metadata.finding_source = Some(source_label.clone());
        metadata.finding_kind = Some(kind.clone());
        metadata.finding_severity = Some(finding_severity(&kind).to_string());
        metadata.finding_confidence = Some(if sanitizer_evidence.is_some() {
            "confirmed_by_sanitizer_log".to_string()
        } else {
            "confirmed_by_forkserver_exit".to_string()
        });
        metadata.finding_detail = Some(detail.clone());
        metadata.finding_artifact = Some(seed_path.display().to_string());
        persist_bootstrap_metadata(self.bootstrap_path.as_deref(), metadata)?;

        let now = unix_time_seconds();
        let elapsed_seconds = self.started.elapsed().as_secs_f64();
        let cycle_metadata = infer_seed_cycle_metadata(input.as_ref());
        let trace_metadata = read_fb_state_trace_metadata();
        let record = RuntimeFindingRecord {
            schema_version: "1.1",
            executions: *state.executions(),
            content_hash: digest.clone(),
            source: source_label.clone(),
            kind: kind.clone(),
            severity: finding_severity(&kind).to_string(),
            confidence: if sanitizer_evidence.is_some() {
                "confirmed_by_sanitizer_log".to_string()
            } else {
                "confirmed_by_forkserver_exit".to_string()
            },
            detail,
            trigger: if let Some(evidence) = sanitizer_evidence {
                format!("{exit_kind:?}; sanitizer_log={}", evidence.path.display())
            } else {
                format!("{exit_kind:?}")
            },
            sanitizer_log: sanitizer_log.clone(),
            parent_id,
            reproducer: seed_path.display().to_string(),
            target_corpus_hint: self.target_corpus_dir.display().to_string(),
            stack_status: if sanitizer_log.is_some() {
                "sanitizer_log_captured; replay report to symbolize stack if needed".to_string()
            } else {
                "not_captured_during_forkserver; replay report to collect stderr/stack".to_string()
            },
            stack: None,
            target_kind: cycle_metadata.target_kind,
            cycle_count: cycle_metadata.cycle_count,
            cycle_ids: cycle_metadata.cycle_ids,
            stale_cycles: trace_metadata.stale_cycles,
            last_changed_cycle: trace_metadata.last_changed_cycle,
            state_hashes: trace_metadata.state_hashes,
            signature: signature.clone(),
            elapsed_seconds,
            unix_time_seconds: now,
        };
        let json_path = finding_dir.join(format!("{seed_id}.json"));
        let serialized = serde_json::to_string_pretty(&record).map_err(|error| {
            Error::serialize(format!("unable to serialize runtime finding: {error}"))
        })?;
        fs::write(&json_path, format!("{serialized}\n"))?;

        append_runtime_finding_event(
            &self.events_file,
            &RuntimeFindingEvent {
                event: "runtime_finding",
                source: source_label,
                kind,
                content_hash: digest,
                reproducer: seed_path.display().to_string(),
                sanitizer_log,
                signature,
                executions: *state.executions(),
                elapsed_seconds,
                unix_time_seconds: now,
            },
        )?;
        Ok(())
    }
}

impl Named for RuntimeFindingFeedback {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<S> StateInitializer<S> for RuntimeFindingFeedback {}

impl<EM, OT, S> Feedback<EM, BytesInput, OT, S> for RuntimeFindingFeedback
where
    S: HasCorpus<BytesInput> + HasExecutions + HasMetadata,
{
    fn is_interesting(
        &mut self,
        state: &mut S,
        _manager: &mut EM,
        input: &BytesInput,
        _observers: &OT,
        exit_kind: &ExitKind,
    ) -> Result<bool, Error> {
        if !self.records_loaded {
            self.records_loaded = true;
            if let Some(saved) = state.metadata_map().get::<RuntimeFindingRecords>() {
                self.records = saved.clone();
            }
        }
        let known = (
            self.records.signatures.len(),
            self.records.seen_sanitizer_logs.len(),
        );
        let result = self.record_findings(state, input, exit_kind);
        if known
            != (
                self.records.signatures.len(),
                self.records.seen_sanitizer_logs.len(),
            )
        {
            state.add_metadata(self.records.clone());
        }
        result
    }
}

// Deduplication key of a sanitizer report: the error summary plus the top
// three stack frames as module+offset, skipping shared-library frames (libc
// abort/raise are identical for unrelated bugs). Offsets are stable for one
// binary because ASan runs with symbolize=0.
fn sanitizer_signature(kind: &str, report: &str) -> String {
    let summary = report
        .lines()
        .find_map(|line| line.split_once("SUMMARY: ").map(|(_, rest)| rest))
        .map(|rest| rest.split(" (").next().unwrap_or(rest).trim().to_string())
        .or_else(|| {
            report.lines().find_map(|line| {
                line.split_once("runtime error: ")
                    .map(|(_, rest)| rest.trim().to_string())
            })
        })
        .unwrap_or_default();
    let frames = report
        .lines()
        .filter_map(stack_frame_key)
        .filter(|frame| !(frame.starts_with("lib") && frame.contains(".so")))
        .take(3)
        .collect::<Vec<_>>();
    format!("{kind}|{summary}|{}", frames.join(","))
}

fn stack_frame_key(line: &str) -> Option<String> {
    let line = line.trim_start();
    let rest = line.strip_prefix('#')?;
    let digits = rest.chars().take_while(char::is_ascii_digit).count();
    if digits == 0 {
        return None;
    }
    if let Some((_, symbolized)) = rest.split_once(" in ") {
        return symbolized.split_whitespace().next().map(str::to_string);
    }
    let open = rest.find('(')?;
    let close = open + rest[open..].find(')')?;
    let (module, offset) = rest[open + 1..close].rsplit_once("+0x")?;
    let module = module.rsplit('/').next().unwrap_or(module);
    Some(format!("{module}+0x{offset}"))
}

fn sanitizer_log_name_is_candidate(file_name: &str) -> bool {
    let lower = file_name.to_ascii_lowercase();
    lower.starts_with("asan")
        || lower.starts_with("ubsan")
        || lower.contains("addresssanitizer")
        || lower.contains("undefinedbehaviorsanitizer")
}

fn existing_sanitizer_log_fingerprints(log_dir: &Path) -> HashSet<String> {
    let mut fingerprints = HashSet::new();
    let Ok(entries) = fs::read_dir(log_dir) else {
        return fingerprints;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        if !sanitizer_log_name_is_candidate(file_name) {
            continue;
        }
        let Ok(metadata) = entry.metadata() else {
            continue;
        };
        if metadata.is_file() && metadata.len() > 0 {
            fingerprints.insert(sanitizer_log_fingerprint(&path, &metadata));
        }
    }
    fingerprints
}

fn sanitizer_log_fingerprint(path: &Path, metadata: &fs::Metadata) -> String {
    let modified = metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    format!("{}:{}:{modified}", path.display(), metadata.len())
}

fn sanitizer_log_kind(file_name: &str, contents: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(contents);
    let lower = text.to_ascii_lowercase();
    if lower.contains("leaksanitizer has encountered a fatal error")
        && lower.contains("does not work under ptrace")
    {
        return None;
    }
    if lower.contains("error: addresssanitizer") || lower.contains("addresssanitizer:") {
        Some("asan".to_string())
    } else if lower.contains("undefinedbehaviorsanitizer") || lower.contains("runtime error:") {
        Some("ubsan".to_string())
    } else if sanitizer_log_name_is_candidate(file_name)
        && (lower.contains("aborting") || lower.contains("summary:"))
    {
        Some("sanitizer".to_string())
    } else {
        None
    }
}

fn parse_integer_value(value: &str) -> Option<i64> {
    let trimmed = value.trim().replace('_', "");
    if trimmed.is_empty() {
        return None;
    }
    let (negative, digits) = if let Some(rest) = trimmed.strip_prefix('-') {
        (true, rest)
    } else if let Some(rest) = trimmed.strip_prefix('+') {
        (false, rest)
    } else {
        (false, trimmed.as_str())
    };
    let parsed = if let Some(hex) = digits
        .strip_prefix("0x")
        .or_else(|| digits.strip_prefix("0X"))
    {
        i64::from_str_radix(hex, 16).ok()?
    } else {
        digits.parse::<i64>().ok()?
    };
    Some(if negative { -parsed } else { parsed })
}

fn parse_number_value(value: &str) -> Option<f64> {
    let trimmed = value.trim().replace('_', "");
    if trimmed.is_empty() {
        return None;
    }
    parse_integer_value(&trimmed)
        .map(|value| value as f64)
        .or_else(|| trimmed.parse::<f64>().ok())
}

// 根据 ST 类型选择更有意义的变异策略，避免纯字节 havoc 破坏输入语法。
