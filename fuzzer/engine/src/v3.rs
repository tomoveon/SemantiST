// v3 frame-level execution, prefix cache and goal-task data.
//
// The fuzz loop lives in the Rust engine (see `v3_stage.rs`); this module owns
// the wire formats and the C harness bridge so both the executor and the
// stage can be unit tested without a target.

use serde_json::Value;
use std::ffi::CString;

extern "C" {
    fn semantist_init(shared_records: *const std::ffi::c_char) -> std::ffi::c_int;
    fn semantist_step(frame: *const u8, dt_ns: u64) -> std::ffi::c_int;
    fn semantist_apply_raw_field(
        name: *const std::ffi::c_char,
        bytes: *const u8,
        byte_len: usize,
        out: *mut u8,
    ) -> std::ffi::c_int;
    fn semantist_apply_raw_shared_field(
        name: *const std::ffi::c_char,
        bytes: *const u8,
        byte_len: usize,
    ) -> std::ffi::c_int;
    fn semantist_checkpoint(out: *mut u8);
    fn semantist_restore(input: *const u8);
    fn semantist_state_size() -> usize;
    fn semantist_state_region_count() -> usize;
    fn semantist_state_region_base(index: usize) -> *const u8;
    fn semantist_state_region_size(index: usize) -> u64;
    fn semantist_harness_state_base() -> *const u8;
    #[link_name = "__semantist_semantic_buffer"]
    static SEMANTIST_GOAL_MAP: u8;
    #[link_name = "__semantist_semantic_buffer_size"]
    static SEMANTIST_GOAL_MAP_SIZE: std::sync::atomic::AtomicUsize;
}

fn goal_map_size() -> usize {
    unsafe { SEMANTIST_GOAL_MAP_SIZE.load(std::sync::atomic::Ordering::Relaxed) }
}

fn goal_map_hit(runtime_id: u32) -> bool {
    let index = runtime_id as usize;
    index < goal_map_size() && unsafe { *(&raw const SEMANTIST_GOAL_MAP).cast::<u8>().add(index) > 0 }
}

fn restore_goal_hit(runtime_id: u32) {
    let index = runtime_id as usize;
    if index < goal_map_size() {
        unsafe { *(&raw const SEMANTIST_GOAL_MAP).cast_mut().cast::<u8>().add(index) = 1; }
    }
}

fn clear_goal_map() {
    let size = goal_map_size();
    if size == 0 {
        return;
    }
    unsafe { std::ptr::write_bytes((&raw const SEMANTIST_GOAL_MAP).cast_mut(), 0, size); }
}

fn restore_goal_map(observations: &[FrameObservation]) {
    clear_goal_map();
    for observation in observations {
        for &runtime_id in &observation.goal_hits {
            restore_goal_hit(runtime_id);
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Spec {
    pub kind: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub c_type: Option<String>,
    #[serde(default)]
    pub size: Option<u32>,
    #[serde(default)]
    pub count: Option<u32>,
    #[serde(default)]
    pub byte_size: Option<u32>,
    #[serde(default)]
    pub length: Option<u32>,
    #[serde(default)]
    pub element: Option<Box<V3Spec>>,
    #[serde(default)]
    pub members: Vec<V3StructMember>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3StructMember {
    pub name: String,
    #[serde(default)]
    pub offset: u32,
    #[serde(default)]
    pub size: u32,
    pub spec: V3Spec,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Field {
    pub name: String,
    #[serde(default)]
    pub offset: Option<i64>,
    #[serde(default)]
    pub size: Option<u32>,
    #[serde(default)]
    pub c_name: String,
    #[serde(default)]
    pub block_kind: String,
    pub spec: V3Spec,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3FrameLayout {
    pub size: u32,
    #[serde(default = "one")]
    pub align: u32,
    #[serde(default)]
    pub fields: Vec<V3Field>,
}

fn one() -> u32 { 1 }

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Target {
    pub name: String,
    pub kind: String,
    #[serde(default)]
    pub instance_size: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Abi {
    pub schema_version: String,
    pub target: V3Target,
    pub frame: V3FrameLayout,
    #[serde(default)]
    pub frame_fields: Vec<V3Field>,
    #[serde(default)]
    pub shared_fields: Vec<V3Field>,
    #[serde(default)]
    pub state_fields: Vec<V3Field>,
}

impl V3Abi {
    pub fn action_fields(&self) -> Vec<V3Field> {
        self.frame_fields.iter().chain(self.shared_fields.iter()
            .filter(|field| field.block_kind == "VAR_IN_OUT")).cloned().collect()
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StateSlice {
    pub name: String,
    pub region: String,
    pub offset: Option<i64>,
    pub ctype: Option<String>,
    #[serde(default)]
    pub block_kind: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AbstractionEntry {
    pub field: String,
    #[serde(default)]
    pub thresholds: Vec<i64>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub timer: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TimerSpec {
    pub name: String,
    #[serde(default)]
    pub pt_const_ns: Option<u64>,
    #[serde(default)]
    pub region: Option<String>,
    #[serde(default)]
    pub offset: Option<i64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Condition {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub lhs: Value,
    #[serde(default)]
    pub op: String,
    #[serde(default)]
    pub rhs: Value,
    #[serde(default)]
    pub depends_on: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Path {
    pub path_id: String,
    #[serde(default)]
    pub conditions: Vec<V3Condition>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Goal {
    pub runtime_id: Option<u32>,
    pub stable_id: Option<String>,
    pub kind: Option<String>,
    pub hazard: Option<String>,
    #[serde(default)]
    pub modeling: Option<String>,
    #[serde(default)]
    pub source: Option<Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Task {
    pub task_id: String,
    pub goal: V3Goal,
    #[serde(default)]
    pub goal_class: String,
    #[serde(default)]
    pub reach_runtime_ids: Vec<u32>,
    #[serde(default)]
    pub polarity: Option<String>,
    #[serde(default)]
    pub requires_history: bool,
    #[serde(default)]
    pub controllable_fields: Vec<String>,
    #[serde(default)]
    pub state_slice: Vec<StateSlice>,
    #[serde(default)]
    pub candidate_paths: Vec<V3Path>,
    #[serde(default)]
    pub abstraction: Vec<AbstractionEntry>,
    #[serde(default)]
    pub edge_inputs: Vec<String>,
    #[serde(default)]
    pub timers: Vec<TimerSpec>,
    #[serde(default)]
    pub symbolic_suitable: bool,
    #[serde(default)]
    pub budget: Option<Value>,
    #[serde(default)]
    pub control_obligations: Vec<Value>,
    #[serde(default)]
    pub seed_prefixes: Vec<Vec<std::collections::BTreeMap<String, String>>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Tasks {
    pub schema_version: String,
    #[serde(default)]
    pub contract_id: Option<String>,
    pub target: Option<Value>,
    #[serde(default)]
    pub single_cycle: bool,
    #[serde(default)]
    pub unknown_cycle_effects: bool,
    pub tasks: Vec<V3Task>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Region {
    pub index: u32,
    pub name: String,
    pub size: u64,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub fields: Vec<Value>,
    #[serde(default)]
    pub pointer_offsets: Vec<u64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct V3Layout {
    pub total_size: u64,
    #[serde(default)]
    pub snapshot_schema_version: u32,
    #[serde(default)]
    pub harness_region_index: Option<u32>,
    pub regions: Vec<V3Region>,
}

impl V3Layout {
    pub fn region_base(&self, name: &str) -> Option<usize> {
        let mut offset = 0usize;
        for region in &self.regions {
            if region.name == name || (name == "instance" && region.source == "harness") {
                let extra = if name == "instance" {
                    region
                        .fields
                        .iter()
                        .find(|field| field.get("kind").and_then(Value::as_str) == Some("fb_instance"))
                        .and_then(|field| field.get("offset").and_then(Value::as_u64))
                        .unwrap_or(0) as usize
                } else {
                    0
                };
                return Some(offset + extra);
            }
            offset += region.size as usize;
        }
        None
    }
}

#[derive(Clone, Debug)]
pub struct V3Bundle {
    pub dir: PathBuf,
    pub abi: V3Abi,
    pub layout: V3Layout,
    pub tasks: V3Tasks,
    pub contract_id: String,
    pub scan_period_ns: u64,
}

impl V3Bundle {
    pub fn load(dir: &Path, _target: &Path) -> Result<Self, String> {
        let read = |name: &str| -> Result<Vec<u8>, String> {
            fs::read(dir.join(name)).map_err(|e| format!("read {name}: {e}"))
        };
        let abi_document: Value = serde_json::from_slice(&read("abi.json")?)
            .map_err(|e| format!("abi.json: {e}"))?;
        require_fields(
            &abi_document,
            "abi.json",
            &["schema_version", "target", "frame", "frame_fields", "shared_fields", "state_fields"],
        )?;
        require_fields(
            abi_document.get("target").unwrap_or(&Value::Null),
            "abi.json.target",
            &["name", "kind", "instance_size"],
        )?;
        require_fields(
            abi_document.get("frame").unwrap_or(&Value::Null),
            "abi.json.frame",
            &["size", "align", "fields"],
        )?;
        let abi: V3Abi = serde_json::from_value(abi_document)
            .map_err(|e| format!("abi.json: {e}"))?;

        let layout_document: Value = serde_json::from_slice(&read("state-layout.json")?)
            .map_err(|e| format!("state-layout.json: {e}"))?;
        require_fields(
            &layout_document,
            "state-layout.json",
            &["schema_version", "snapshot_schema_version", "total_size", "harness_region_index", "regions"],
        )?;
        let layout: V3Layout = serde_json::from_value(layout_document)
            .map_err(|e| format!("state-layout.json: {e}"))?;

        let tasks = if std::env::var("SEMANTIST_EXPERIMENT_NO_GOAL_TASKS").as_deref() == Ok("1") {
            V3Tasks {
                schema_version: "semantist.goal-tasks/1.0.0".to_string(),
                contract_id: None,
                target: None,
                single_cycle: false,
                unknown_cycle_effects: false,
                tasks: Vec::new(),
            }
        } else {
            let tasks_document: Value = serde_json::from_slice(&read("goal-tasks.json")?)
                .map_err(|e| format!("goal-tasks.json: {e}"))?;
            require_fields(&tasks_document, "goal-tasks.json", &["schema_version", "target", "tasks"])?;
            serde_json::from_value(tasks_document)
                .map_err(|e| format!("goal-tasks.json: {e}"))?
        };

        let contract: Value = serde_json::from_slice(&read("execution-contract.json")?)
            .map_err(|e| format!("execution-contract.json: {e}"))?;
        require_fields(&contract, "execution-contract.json", &["schema_version", "contract_id", "time"])?;
        let contract_id = contract
            .get("contract_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| "execution-contract.json.contract_id must be a non-empty string".to_string())?
            .to_string();
        let scan_period_ns = contract
            .get("time")
            .and_then(|time| time.get("scan_period_ns"))
            .and_then(Value::as_u64)
            .filter(|value| *value > 0)
            .ok_or_else(|| "execution-contract.json.time.scan_period_ns must be a positive integer".to_string())?;
        Ok(Self {
            dir: dir.to_path_buf(),
            abi,
            layout,
            tasks,
            contract_id,
            scan_period_ns,
        })
    }

    pub fn schema(&self) -> FrameSchema {
        let mut schema = FrameSchema::default();
        for entry in self.abi.action_fields() {
            schema.frame_defaults.insert(entry.name.clone(), default_for_named(&entry.name, &entry.spec));
            schema.frame_types.insert(entry.name.clone(), type_token(&entry.spec));
            if entry.spec.kind == "scalar" {
                schema.frame_scalar_width.insert(entry.name.clone(), entry.spec.size.unwrap_or(1) as usize);
            } else if entry.spec.kind != "string" {
                let capacity = if entry.spec.kind == "pointer" { 4096 } else { entry.spec.byte_size.or(entry.spec.size).unwrap_or(0) as usize };
                schema.frame_byte_capacity.insert(entry.name.clone(), capacity);
            }
            if let Some(bounds) = integer_bounds(&entry.spec) {
                schema.frame_bounds.insert(entry.name.clone(), bounds);
            }
            if entry.spec.kind == "string" {
                if let Some(length) = entry.spec.length {
                    schema.frame_string_lengths.insert(entry.name.clone(), length as usize);
                }
                schema.frame_string_wide.insert(
                    entry.name.clone(),
                    entry.spec.name.eq_ignore_ascii_case("WSTRING"),
                );
            }
        }
        for entry in &self.abi.shared_fields {
            schema.shared_types.insert(entry.name.clone(), type_token(&entry.spec));
            if entry.spec.kind == "scalar" {
                schema.shared_scalar_width.insert(entry.name.clone(), entry.spec.size.unwrap_or(1) as usize);
            } else if entry.spec.kind != "string" {
                let capacity = if entry.spec.kind == "pointer" { 4096 } else { entry.spec.byte_size.or(entry.spec.size).unwrap_or(0) as usize };
                schema.shared_byte_capacity.insert(entry.name.clone(), capacity);
            }
            if let Some(bounds) = integer_bounds(&entry.spec) {
                schema.shared_bounds.insert(entry.name.clone(), bounds);
            }
            if entry.spec.kind == "string" {
                if let Some(length) = entry.spec.length {
                    schema.shared_string_lengths.insert(entry.name.clone(), length as usize);
                }
                schema.shared_string_wide.insert(
                    entry.name.clone(),
                    entry.spec.name.eq_ignore_ascii_case("WSTRING"),
                );
            }
        }
        schema
    }

    pub fn defect_tasks(&self) -> Vec<&V3Task> {
        self.tasks
            .tasks
            .iter()
            .filter(|task| task.goal_class == "defect")
            .collect()
    }
}

fn require_fields(document: &Value, label: &str, fields: &[&str]) -> Result<(), String> {
    let object = document
        .as_object()
        .ok_or_else(|| format!("{label} must be a JSON object"))?;
    for field in fields {
        if !object.contains_key(*field) {
            return Err(format!("{label} missing required field {field}"));
        }
    }
    Ok(())
}

pub fn type_token(spec: &V3Spec) -> String {
    if spec.kind == "scalar" || spec.kind == "string" {
        spec.name.clone()
    } else if spec.kind == "pointer" {
        "POINTER".to_string()
    } else {
        "ARRAY".to_string()
    }
}

pub fn default_for_named(name: &str, spec: &V3Spec) -> String {
    // XML_READER consumes a large byte buffer until STOP_POS.  A minimal
    // well-formed element keeps a cold native execution finite; raw mutation
    // still owns every byte after this structural seed.
    if spec.kind == "array"
        && name.eq_ignore_ascii_case("BUF")
        && spec.byte_size.or(spec.size).unwrap_or(0) >= 1024
    {
        let size = spec.byte_size.or(spec.size).unwrap_or(0) as usize;
        let mut bytes = vec![0u8; size];
        bytes[..4].copy_from_slice(b"<a/>");
        return encode_hex_bytes(&bytes);
    }
    if (spec.kind == "opaque" || spec.kind == "array") && !spec.members.is_empty() {
        let size = spec.byte_size.or(spec.size).unwrap_or(0) as usize;
        if size > 0 {
            let mut bytes = vec![0u8; size];
            prime_structured_bytes(name, spec, &mut bytes);
            return encode_hex_bytes(&bytes);
        }
    }
    default_for(spec)
}

pub fn default_for(spec: &V3Spec) -> String {
    if spec.kind == "scalar" {
        if spec.name == "REAL" || spec.name == "LREAL" {
            return "0.0".to_string();
        }
        return "0".to_string();
    }
    if spec.kind == "string" {
        return "A".to_string();
    }
    "0x00".to_string()
}

/// Seed protocol-like aggregate controls without constraining later raw
/// mutation. The rules use ABI member names and sizes only, so they apply to
/// any compatible structure and do not encode a target-specific testcase.
pub fn prime_structured_bytes(parent: &str, spec: &V3Spec, bytes: &mut [u8]) {
    fn put(bytes: &mut [u8], offset: usize, size: usize, value: i128) {
        if size == 0 || offset >= bytes.len() || offset + size > bytes.len() {
            return;
        }
        let raw = value.to_le_bytes();
        let count = size.min(raw.len());
        bytes[offset..offset + count].copy_from_slice(&raw[..count]);
    }
    let root_type = spec.name.to_ascii_lowercase();
    fn visit(
        parent: &str,
        members: &[V3StructMember],
        base: usize,
        bytes: &mut [u8],
        root_type: &str,
    ) {
        for member in members {
            let offset = base.saturating_add(member.offset as usize);
            let size = member.size.max(member.spec.byte_size.or(member.spec.size).unwrap_or(0)) as usize;
            let path = if parent.is_empty() {
                member.name.clone()
            } else {
                format!("{parent}.{}", member.name)
            };
            let lower = member.name.to_ascii_lowercase();
            if !member.spec.members.is_empty() {
                visit(&path, &member.spec.members, offset, bytes, root_type);
                continue;
            }
            if member.spec.kind == "array" && lower == "mailbox" {
                // ST arrays with a lower bound of 1 are lowered to a
                // zero-based ABI array.  The generated LLVM accesses
                // MAILBOX[1] at the first physical byte, so seed the ABI
                // element at the member offset rather than source index 1.
                if size > 0 && offset < bytes.len() {
                    bytes[offset] = 1;
                }
                continue;
            }
            if member.spec.kind != "scalar" {
                if lower == "buffer" && offset < bytes.len() {
                    let payload: &[u8] = if root_type.contains("network_buffer_short") {
                        b"A,A\r\n"
                    } else if path.to_ascii_lowercase().contains(".r_buf.") {
                        // NET_VAR_BUFFER consumes this length into BUF_OUT[1..64].
                        // Keep the protocol header coherent while exercising the
                        // first out-of-bounds length at 65 bytes.
                        &[0x01, 0x20, 0x41]
                    } else if parent.to_ascii_lowercase().ends_with("r_buf") {
                        b"<a/>"
                    } else {
                        &[0]
                    };
                    let count = payload.len().min(bytes.len() - offset);
                    bytes[offset..offset + count].copy_from_slice(&payload[..count]);
                }
                continue;
            }
            let value = match lower.as_str() {
                "command" => Some(0x8001),
                "state" => Some(2),
                "error_id" => Some(0),
                "buf_size" => Some(128),
                "size" => Some(if root_type.contains("network_buffer_short") {
                    5
                } else if path.to_ascii_lowercase().contains("r_buf") {
                    65
                } else {
                    128
                }),
                "index" | "start_pos" => Some(0),
                "stop_pos" => Some(3),
                _ => None,
            };
            if let Some(value) = value {
                put(bytes, offset, size, value);
            }
        }
    }
    visit(parent, &spec.members, 0, bytes, &root_type);
}

pub fn integer_bounds(spec: &V3Spec) -> Option<(i128, i128)> {
    if spec.kind != "scalar" || matches!(spec.name.as_str(), "REAL" | "LREAL") {
        return None;
    }
    let bytes = spec.size.or(spec.byte_size)?;
    if bytes == 0 || bytes > 8 {
        return None;
    }
    let bits = bytes * 8;
    if spec.name == "BOOL" {
        return Some((0, 1));
    }
    let unsigned = spec.c_type.as_deref().is_some_and(|ctype| ctype.starts_with("uint"))
        || matches!(spec.name.as_str(), "USINT" | "BYTE" | "CHAR" | "UINT" | "WORD" | "WCHAR"
                    | "UDINT" | "DWORD" | "ULINT" | "LWORD");
    if unsigned {
        Some((0, (1i128 << bits) - 1))
    } else {
        Some((-(1i128 << (bits - 1)), (1i128 << (bits - 1)) - 1))
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ExecOutcome {
    pub frames: usize,
    pub suffix: usize,
    pub prefix_depth: usize,
    pub restored: bool,
    pub scans: usize,
    pub provenance: String,
    pub observations: Vec<FrameObservation>,
    pub exit_kind: String,
    pub parse_ns: u64,
    pub abi_ns: u64,
    pub native_step_ns: u64,
    pub semantic_ns: u64,
    pub snapshot_ns: u64,
    pub worker_roundtrip_ns: u64,
    pub worker_launch_ns: u64,
    pub final_state: Vec<u8>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct FrameObservation {
    pub frame_index: usize,
    pub logical_time_ns: u64,
    pub values: std::collections::BTreeMap<String, i128>,
    pub goal_hits: Vec<u32>,
}

#[derive(Clone, Debug)]
struct Snapshot {
    depth: usize,
    state: Vec<u8>,
    observations: Vec<FrameObservation>,
    bytes: usize,
    saves: u64,
}

fn observation_bytes(observations: &[FrameObservation]) -> usize {
    observations.iter().map(|entry| {
        std::mem::size_of::<FrameObservation>()
            + entry.values.iter().map(|(name, _)| name.len() + std::mem::size_of::<i128>()).sum::<usize>()
            + entry.goal_hits.len() * std::mem::size_of::<u32>()
    }).sum()
}

#[derive(Clone, Debug)]
struct PrefixRoot {
    id: u64,
    init_profile: String,
    shared: std::collections::BTreeMap<String, String>,
}

#[derive(Clone, Debug)]
struct PrefixNode {
    id: u64,
    parent_id: u64,
    frame: Frame,
}

#[derive(Clone, Debug)]
pub struct PrefixCache {
    snapshots: HashMap<u64, Snapshot>,
    roots: Vec<PrefixRoot>,
    nodes: Vec<PrefixNode>,
    next_id: u64,
    max_bytes: usize,
    max_entries: usize,
    used_bytes: usize,
    pub hits: u64,
    pub saved_scans: u64,
    pub restore_ns: u64,
    pub checkpoint_ns: u64,
    pub restore_attempts: u64,
    pub validations: u64,
    pub invalidations: u64,
    pub enabled: bool,
}

impl Default for PrefixCache {
    fn default() -> Self {
        Self {
            snapshots: HashMap::new(),
            roots: Vec::new(),
            nodes: Vec::new(),
            next_id: 1,
            max_bytes: 256 * 1024 * 1024,
            max_entries: 4096,
            used_bytes: 0,
            hits: 0,
            saved_scans: 0,
            restore_ns: 0,
            checkpoint_ns: 0,
            restore_attempts: 0,
            validations: 0,
            invalidations: 0,
            enabled: true,
        }
    }
}

impl PrefixCache {
    pub fn new(enabled: bool) -> Self {
        let mut cache = Self::default();
        cache.enabled = enabled;
        cache
    }

    pub fn len(&self) -> usize {
        self.snapshots.len()
    }

    fn allocate_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        id
    }

    fn prefix_ids(&mut self, case: &TestCase) -> Vec<u64> {
        if self.nodes.len() > self.max_entries * 16 {
            self.clear();
        }
        let root_id = match self.roots.iter().find(|root|
            root.init_profile == case.init_profile && root.shared == case.shared
        ) {
            Some(root) => root.id,
            None => {
                let id = self.allocate_id();
                self.roots.push(PrefixRoot { id, init_profile: case.init_profile.clone(), shared: case.shared.clone() });
                id
            }
        };
        let mut parent_id = root_id;
        let mut ids = Vec::with_capacity(case.frames.len());
        for frame in &case.frames {
            let id = match self.nodes.iter().find(|node| node.parent_id == parent_id && node.frame == *frame) {
                Some(node) => node.id,
                None => {
                    let id = self.allocate_id();
                    self.nodes.push(PrefixNode { id, parent_id, frame: frame.clone() });
                    id
                }
            };
            ids.push(id);
            parent_id = id;
        }
        ids
    }

    fn evict(&mut self) {
        while self.snapshots.len() >= self.max_entries || self.used_bytes > self.max_bytes {
            let victim = self
                .snapshots
                .iter()
                .min_by_key(|(_, snapshot)| snapshot.saves)
                .map(|(id, _)| *id);
            match victim {
                Some(id) => {
                    if let Some(snapshot) = self.snapshots.remove(&id) {
                        self.used_bytes = self.used_bytes.saturating_sub(snapshot.bytes);
                    }
                }
                None => break,
            }
        }
    }

    fn store(&mut self, id: u64, depth: usize, observations: &[FrameObservation]) {
        if !self.enabled || depth == 0 {
            return;
        }
        if let Some(existing) = self.snapshots.get_mut(&id) {
            existing.saves += 1;
            return;
        }
        let size = unsafe { semantist_state_size() };
        if size == 0 {
            return;
        }
        let mut state = vec![0u8; size];
        let start = std::time::Instant::now();
        unsafe { semantist_checkpoint(state.as_mut_ptr()) };
        self.checkpoint_ns += start.elapsed().as_nanos() as u64;
        let bytes = state.len() + observation_bytes(observations);
        if bytes > self.max_bytes { return; }
        self.evict();
        while self.used_bytes + bytes > self.max_bytes && !self.snapshots.is_empty() {
            let victim = self.snapshots.iter().min_by_key(|(_, snapshot)| snapshot.saves).map(|(id, _)| *id);
            if let Some(victim) = victim {
                if let Some(old) = self.snapshots.remove(&victim) {
                    self.used_bytes = self.used_bytes.saturating_sub(old.bytes);
                }
            }
        }
        self.used_bytes += bytes;
        self.snapshots.insert(id, Snapshot { depth, state, observations: observations.to_vec(), bytes, saves: 0 });
    }

    /// Export a snapshot for a later KLEE process.  The node number is an
    /// in-memory prefix identity; the canonical prefix bytes are stored only
    /// as a lookup record and are never used as a hash or deduplication key.
    pub fn export_snapshot(&self, id: u64, depth: usize, prefix: &[u8], shared: &str, layout: &V3Layout) {
        let Some(snapshot) = self.snapshots.get(&id) else { return; };
        let Ok(run_dir) = std::env::var("SEMANTIST_V3_RUN_DIR") else { return; };
        let root = std::path::Path::new(&run_dir).join("prefixes").join("snapshots");
        if std::fs::create_dir_all(&root).is_err() { return; }
        let state_path = root.join(format!("node-{id:016x}.bin"));
        if std::fs::write(&state_path, &snapshot.state).is_err() { return; }
        let (relocations, complete, reason) = snapshot_relocations(layout, &snapshot.state);
        let relocation_path = root.join(format!("node-{id:016x}.reloc.json"));
        let relocation_document = serde_json::json!({
            "complete": complete,
            "reason": reason,
            "relocations": relocations,
        });
        if std::fs::write(&relocation_path, serde_json::to_vec_pretty(&relocation_document).unwrap_or_default()).is_err() {
            return;
        }
        let record = serde_json::json!({
            "node_id": id,
            "depth": depth,
            "prefix": String::from_utf8_lossy(prefix),
            "shared_records": shared,
            "state_file": state_path.strip_prefix(std::path::Path::new(&run_dir))
                .unwrap_or(&state_path).to_string_lossy(),
            "relocation_file": relocation_path.strip_prefix(std::path::Path::new(&run_dir))
                .unwrap_or(&relocation_path).to_string_lossy(),
            "relocation_complete": complete,
        });
        let index = std::path::Path::new(&run_dir).join("prefixes").join("snapshots.jsonl");
        append_event(&index, record).expect("failed to append snapshot record");
    }

    fn restore(&mut self, id: u64) -> Option<usize> {
        let expected_size = unsafe { semantist_state_size() };
        if self.snapshots.get(&id)?.state.len() != expected_size {
            if let Some(snapshot) = self.snapshots.remove(&id) {
                self.used_bytes = self.used_bytes.saturating_sub(snapshot.bytes);
            }
            self.invalidations += 1;
            return None;
        }
        let snapshot = self.snapshots.get_mut(&id)?;
        snapshot.saves += 1;
        let start = std::time::Instant::now();
        unsafe { semantist_restore(snapshot.state.as_ptr()) };
        self.restore_ns += start.elapsed().as_nanos() as u64;
        self.hits += 1;
        Some(snapshot.depth)
    }

    pub fn clear(&mut self) {
        self.snapshots.clear();
        self.roots.clear();
        self.nodes.clear();
        self.used_bytes = 0;
    }

    /// A target crash/timeout invalidates exported snapshots as well as the
    /// in-process cache.  Keep an audit marker, but remove the active index so
    /// later solver phases cannot import state from before the failed input.
    pub fn invalidate_exports(reason: &str) {
        let Ok(run_dir) = std::env::var("SEMANTIST_V3_RUN_DIR") else { return; };
        let root = std::path::Path::new(&run_dir).join("prefixes");
        let snapshots = root.join("snapshots");
        let index = root.join("snapshots.jsonl");
        let had_artifacts = snapshots.exists() || index.exists();
        if snapshots.exists() {
            let _ = std::fs::remove_dir_all(&snapshots);
        }
        let _ = std::fs::remove_file(&index);
        if had_artifacts {
            let marker = root.join("snapshots.invalidated.json");
            let document = serde_json::json!({
                "status": "invalidated",
                "reason": reason,
            });
            let _ = std::fs::create_dir_all(&root);
            let _ = std::fs::write(
                marker,
                serde_json::to_vec_pretty(&document).unwrap_or_default(),
            );
        }
    }
}

/// Convert raw pointers in a native checkpoint into region-relative pointers.
/// A sidecar is kept next to the state blob so the KLEE driver can apply the
/// existing C relocation API in a fresh process.  An unmapped pointer makes
/// the import unsupported; the caller can then retry the same prefix cold.
fn snapshot_relocations(layout: &V3Layout, state: &[u8]) -> (Vec<Value>, bool, Option<String>) {
    if std::mem::size_of::<usize>() != 8 {
        return (Vec::new(), false, Some("pointer snapshots require 64-bit addresses".into()));
    }
    let target_count = unsafe { semantist_state_region_count() };
    let harness_index = layout.harness_region_index.map(|value| value as usize).unwrap_or(target_count);
    let mut region_offsets = std::collections::BTreeMap::new();
    let mut cursor = 0usize;
    for region in &layout.regions {
        region_offsets.insert(region.index as usize, cursor);
        cursor = cursor.saturating_add(region.size as usize);
    }
    if cursor > state.len() || target_count > harness_index {
        return (Vec::new(), false, Some("snapshot layout does not match native region count".into()));
    }
    let mut targets: Vec<(usize, usize, usize)> = Vec::new();
    for index in 0..target_count {
        let base = unsafe { semantist_state_region_base(index) } as usize;
        let size = unsafe { semantist_state_region_size(index) as usize };
        if base == 0 || size == 0 {
            return (Vec::new(), false, Some(format!("native target region {index} has no address")));
        }
        targets.push((index, base, size));
    }
    if let Some(region) = layout.regions.iter().find(|entry| entry.index as usize == harness_index) {
        let base = unsafe { semantist_harness_state_base() } as usize;
        if base == 0 || region.size == 0 {
            return (Vec::new(), false, Some("native harness region has no address".into()));
        }
        targets.push((harness_index, base, region.size as usize));
    }
    let mut records = Vec::new();
    let mut missing = Vec::new();
    for region in &layout.regions {
        if region.pointer_offsets.is_empty() {
            continue;
        }
        let source_base = *region_offsets.get(&(region.index as usize)).unwrap_or(&usize::MAX);
        for &offset in &region.pointer_offsets {
            let source_offset = source_base.saturating_add(offset as usize);
            if source_base == usize::MAX || source_offset.checked_add(8).map_or(true, |end| end > state.len()) {
                missing.push(format!("source region {} offset {} is outside checkpoint", region.index, offset));
                continue;
            }
            let mut raw = [0u8; 8];
            raw.copy_from_slice(&state[source_offset..source_offset + 8]);
            let address = usize::from_ne_bytes(raw);
            if address == 0 {
                continue;
            }
            let target = targets.iter().find_map(|(index, base, size)| {
                let end = base.checked_add(*size)?;
                (address >= *base && address < end).then(|| (*index, address - *base))
            });
            if let Some((target_region, target_offset)) = target {
                records.push(serde_json::json!({
                    "region": region.index,
                    "offset": offset,
                    "target_region": target_region,
                    "target_offset": target_offset,
                }));
            } else {
                missing.push(format!("pointer region {} offset {} points outside known regions", region.index, offset));
            }
        }
    }
    if missing.is_empty() {
        (records, true, None)
    } else {
        (records, false, Some(missing.join("; ")))
    }
}

/// Execute a canonical testcase from the longest cached prefix.
pub fn execute_with_cache(bytes: &[u8], bundle: &V3Bundle, cache: &mut PrefixCache) -> Result<ExecOutcome, String> {
    let parse_started = std::time::Instant::now();
    let text = std::str::from_utf8(bytes)
        .map_err(|e| format!("parser reject: input is not UTF-8: {e}"))?;
    let case = parse_testcase(text, &bundle.schema())
        .map_err(|e| format!("parser reject: {e}"))?;
    let parse_ns = parse_started.elapsed().as_nanos() as u64;
    let prefix_ids = cache.prefix_ids(&case);
    let frame_count = case.frames.len();
    let mut observations = Vec::with_capacity(frame_count);
    let mut start = 0usize;
    if cache.enabled && frame_count > 1 {
        for depth in (1..frame_count).rev() {
            if cache.snapshots.contains_key(&prefix_ids[depth - 1]) {
                start = depth;
                break;
            }
        }
    }
    let raw_shared = raw_shared_fields(bundle, &case);
    let shared = shared_records(text, raw_shared.iter().map(|(name, _)| name.as_str()).collect());
    let shared_c = CString::new(shared.clone()).map_err(|e| format!("shared records: {e}"))?;
    let init = unsafe { semantist_init(shared_c.as_ptr()) };
    if init != 0 {
        return Err(format!("semantist_init returned {init}"));
    }
    for (name, raw) in &raw_shared {
        let field_name = CString::new(name.as_bytes())
            .map_err(|error| format!("shared field {name}: {error}"))?;
        let applied = unsafe {
            semantist_apply_raw_shared_field(field_name.as_ptr(), raw.as_ptr(), raw.len())
        };
        if applied != 0 {
            return Err(format!("raw shared field {name} rejected"));
        }
    }
    let mut provenance = if start > 0 { "snapshot" } else { "cold" };
    if start > 0 {
        let id = prefix_ids[start - 1];
        cache.restore_attempts += 1;
        if cache.restore(id).is_none() {
            start = 0;
            provenance = "cold";
        } else {
            observations = cache.snapshots.get(&id).map(|snapshot| snapshot.observations.clone()).unwrap_or_default();
            cache.saved_scans += start as u64;
            for observation in &observations {
                for &runtime_id in &observation.goal_hits {
                    restore_goal_hit(runtime_id);
                }
            }
        }
    }
    let frame_size = bundle.abi.frame.size.max(1) as usize;
    let mut frame = vec![0u8; frame_size];
    let mut logical_time = observations.last().map_or(0, |entry| entry.logical_time_ns);
    let mut abi_ns = 0;
    let mut native_step_ns = 0;
    let mut semantic_ns = 0;
    let mut snapshot_ns = 0;
    for (index, value) in case.frames.iter().enumerate().skip(start) {
        // The semantic map is an execution aggregate. Isolate each step so
        // FrameObservation records the cycle that actually hit a target;
        // restore the aggregate after the testcase for the LibAFL observer.
        clear_goal_map();
        let (dt, frame_abi_ns, frame_step_ns) = execute_frame(bundle, index, value, &mut frame)?;
        abi_ns += frame_abi_ns;
        native_step_ns += frame_step_ns;
        logical_time = logical_time.saturating_add(dt);
        let semantic_started = std::time::Instant::now();
        observations.push(capture_frame_observation(bundle, index, logical_time));
        semantic_ns += semantic_started.elapsed().as_nanos() as u64;
        let depth = index + 1;
        if cache.enabled && frame_count > 1 && (depth % 16 == 0 || depth == frame_count) {
            let snapshot_started = std::time::Instant::now();
            cache.store(prefix_ids[depth - 1], depth, &observations);
            let prefix = serialize(bundle, &case.frames[..depth], &case.shared);
            cache.export_snapshot(prefix_ids[depth - 1], depth, &prefix, &shared, &bundle.layout);
            snapshot_ns += snapshot_started.elapsed().as_nanos() as u64;
        }
    }
    let semantic_started = std::time::Instant::now();
    restore_goal_map(&observations);
    semantic_ns += semantic_started.elapsed().as_nanos() as u64;
    Ok(ExecOutcome {
        frames: frame_count,
        suffix: frame_count.saturating_sub(start),
        prefix_depth: start,
        restored: start > 0,
        scans: frame_count.saturating_sub(start),
        provenance: provenance.to_string(),
        observations,
        exit_kind: "Ok".to_string(),
        parse_ns,
        abi_ns,
        native_step_ns,
        semantic_ns,
        snapshot_ns,
        worker_roundtrip_ns: 0,
        worker_launch_ns: 0,
        final_state: Vec::new(),
    })
}

fn execute_frame(bundle: &V3Bundle, index: usize, value: &Frame, frame: &mut [u8]) -> Result<(u64, u64, u64), String> {
    let abi_started = std::time::Instant::now();
    let dt = value.dt_ns.unwrap_or(bundle.scan_period_ns);
    for field in bundle.abi.action_fields() {
        let current = value.values.get(&field.name)
            .cloned()
            .unwrap_or_else(|| default_for_named(&field.name, &field.spec));
        // Raw seeds and byte mutations are already decoded when the Frame is
        // built. Keep that representation through the native hot path for
        // every field kind; only legacy textual values need type encoding.
        let bytes = value
            .cached_raw_bytes(&field.name, &current)
            .map(|raw| raw.to_vec())
            .unwrap_or_else(|| field_bytes(&field.spec, &current));
        let name = CString::new(field.name.as_bytes())
            .map_err(|error| format!("frame {index} field {}: {error}", field.name))?;
        let applied = unsafe {
            semantist_apply_raw_field(name.as_ptr(), bytes.as_ptr(), bytes.len(), frame.as_mut_ptr())
        };
        if applied != 0 {
            return Err(format!("semantist_apply_raw_field failed at frame {index} field {}", field.name));
        }
    }
    let abi_ns = abi_started.elapsed().as_nanos() as u64;
    let step_started = std::time::Instant::now();
    let stepped = unsafe { semantist_step(frame.as_ptr(), dt) };
    if stepped != 0 { return Err(format!("semantist_step returned {stepped} at frame {index}")); }
    Ok((dt, abi_ns, step_started.elapsed().as_nanos() as u64))
}

fn raw_shared_fields(bundle: &V3Bundle, case: &TestCase) -> Vec<(String, Vec<u8>)> {
    let mut result = Vec::new();
    for field in &bundle.abi.shared_fields {
        let Some(value) = case.shared.get(&field.name) else { continue };
        let raw_hex = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X"));
        let exact_scalar = field.spec.kind == "scalar"
            && raw_hex.is_some()
            && raw_hex.is_some_and(|digits| digits.len() % 2 == 0
                && digits.len() / 2 == field.spec.size.unwrap_or(1) as usize);
        if field.spec.kind != "scalar" || exact_scalar {
            result.push((field.name.clone(), field_bytes(&field.spec, value)));
        }
    }
    result
}

fn shared_records(text: &str, raw_names: std::collections::HashSet<&str>) -> String {
    let mut lines = Vec::new();
    for line in text.lines() {
        let name = line.split(',').next().unwrap_or_default().trim();
        if name.is_empty() || name == "__INIT" || name == "__DT" {
            continue;
        }
        if !name.contains('.') {
            if !raw_names.contains(name) {
                lines.push(line.to_string());
            }
        }
    }
    lines.join("\n")
}

fn frame_records(bundle: &V3Bundle, index: usize, frame: &Frame) -> String {
    let mut out = String::new();
    for field in bundle.abi.action_fields() {
        let value = frame
            .values
            .get(&field.name)
            .cloned()
            .unwrap_or_else(|| default_for_named(&field.name, &field.spec));
        out.push_str(&format!("{}.{},{},{}\n", index, field.name, type_token(&field.spec), value));
    }
    if let Some(dt) = frame.dt_ns {
        out.push_str(&format!("{}.__DT,TIME,{}\n", index, dt));
    }
    out
}

#[derive(Clone, Debug, Deserialize)]
pub struct V3Candidate {
    pub file: PathBuf,
    pub bytes: Vec<u8>,
    pub kind: String,
    pub task_id: Option<String>,
    pub runtime_id: Option<u32>,
}

pub fn write_candidate(
    run_dir: &Path,
    bytes: &[u8],
    kind: &str,
    task_id: Option<&str>,
    runtime_id: Option<u32>,
) -> Result<PathBuf, String> {
    let dir = run_dir.join("findings").join("candidates");
    fs::create_dir_all(&dir).map_err(|e| format!("create candidates: {e}"))?;
    let next = fs::read_dir(&dir).map_err(|e| format!("list candidates: {e}"))?
        .filter_map(Result::ok)
        .filter_map(|entry| entry.file_name().to_string_lossy().strip_prefix("candidate-")
            .and_then(|rest| rest.strip_suffix(".txt"))
            .and_then(|number| number.parse::<u64>().ok()))
        .max().unwrap_or(0) + 1;
    let stem = format!("candidate-{next:06}");
    let path = dir.join(format!("{stem}.txt"));
    fs::write(&path, bytes).map_err(|e| format!("write candidate: {e}"))?;
    let meta = serde_json::json!({
        "kind": kind,
        "task_id": task_id,
        "runtime_id": runtime_id,
        "candidate_id": stem,
        "file": path,
        "created_epoch_ns": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0),
    });
    fs::write(dir.join(format!("{stem}.json")), serde_json::to_vec_pretty(&meta).unwrap())
        .map_err(|e| format!("write candidate metadata: {e}"))?;
    Ok(path)
}

// Keep BTreeMap in the public surface for deterministic prefix records.
pub fn frame_values(frame: &Frame) -> BTreeMap<String, String> {
    frame.values.clone()
}

#[cfg(test)]
mod v3_tests {
    use super::*;

    #[test]
    fn prefix_ids_follow_exact_shared_state_and_frame_parentage() {
        let schema = test_bundle().schema();
        let mut cache = PrefixCache::default();
        let first = parse_testcase("0.Run,BOOL,1\n1.Speed,INT,7\n", &schema).unwrap();
        let repeat = parse_testcase("0.Run,BOOL,1\n1.Speed,INT,7\n", &schema).unwrap();
        let mut shared = first.clone();
        shared.shared.insert("Mode".into(), "2".into());
        let mut changed_frame = first.clone();
        changed_frame.frames[1].values.insert("Speed".into(), "8".into());
        let ids = cache.prefix_ids(&first);
        assert_eq!(cache.prefix_ids(&repeat), ids);
        assert_ne!(cache.prefix_ids(&shared)[0], ids[0]);
        let changed_ids = cache.prefix_ids(&changed_frame);
        assert_eq!(changed_ids[0], ids[0]);
        assert_ne!(changed_ids[1], ids[1]);
    }

    #[test]
    fn identical_candidate_inputs_keep_distinct_sequential_artifacts() {
        let run_dir = std::env::temp_dir().join(format!("v3-candidates-{}-{}", std::process::id(), current_nanos()));
        let first = write_candidate(&run_dir, b"0.X,INT,1\n", "goal", Some("g"), Some(1)).unwrap();
        let second = write_candidate(&run_dir, b"0.X,INT,1\n", "goal", Some("g"), Some(1)).unwrap();
        assert_ne!(first, second);
        assert_eq!(fs::read(first).unwrap(), fs::read(second).unwrap());
        fs::remove_dir_all(run_dir).unwrap();
    }

    #[test]
    fn frame_records_use_abi_types_and_dt() {
        let bundle = test_bundle();
        let mut values = BTreeMap::new();
        values.insert("Run".to_string(), "1".to_string());
        values.insert("Speed".to_string(), "7".to_string());
        let frame = Frame::new(3, values, Some(5));
        let text = frame_records(&bundle, 3, &frame);
        assert!(text.contains("3.Run,BOOL,1"));
        assert!(text.contains("3.Speed,INT,7"));
        assert!(text.contains("3.__DT,TIME,5"));
    }

    #[test]
    fn structured_seed_uses_zero_based_physical_mailbox_element() {
        let byte = || V3Spec {
            kind: "scalar".into(), name: "BYTE".into(), c_type: Some("uint8_t".into()),
            size: Some(1), count: None, byte_size: Some(1), length: None,
            element: None, members: Vec::new(),
        };
        let mailbox = V3StructMember {
            name: "MAILBOX".into(), offset: 4, size: 16,
            spec: V3Spec {
                kind: "array".into(), name: "ARRAY OF BYTE".into(), c_type: None,
                size: Some(0), count: Some(16), byte_size: Some(16), length: None,
                element: Some(Box::new(byte())), members: Vec::new(),
            },
        };
        let spec = V3Spec {
            kind: "opaque".into(), name: "IP_C".into(), c_type: None,
            size: Some(20), count: None, byte_size: Some(20), length: None,
            element: None, members: vec![mailbox],
        };
        let mut bytes = vec![0u8; 20];
        prime_structured_bytes("IP_C", &spec, &mut bytes);
        assert_eq!(bytes[4], 1);
        assert_eq!(bytes[5], 0);
    }

    #[test]
    fn large_buf_cold_default_is_a_finite_xml_record() {
        let spec = V3Spec {
            kind: "array".into(), name: "ARRAY OF BYTE".into(), c_type: None,
            size: Some(4096), count: Some(4096), byte_size: Some(4096), length: None,
            element: None, members: Vec::new(),
        };
        let value = default_for_named("BUF", &spec);
        let bytes = raw_hex_bytes(&value).unwrap();
        assert_eq!(bytes.len(), 4096);
        assert_eq!(&bytes[..4], b"<a/>");
    }

    #[test]
    fn shared_records_keep_unprefixed_lines_only() {
        let text = "Enable,BOOL,1\n0.Enable,BOOL,1\n__INIT,ENUM,cold\n";
        assert_eq!(shared_records(text, std::collections::HashSet::new()), "Enable,BOOL,1");
    }

    #[test]
    fn inout_only_target_has_frame_actions_and_preserves_dt() {
        let mut bundle = test_bundle();
        bundle.abi.frame_fields.clear();
        bundle.abi.shared_fields.push(V3Field {
            name: "Counter".into(), offset: Some(8), size: Some(2), c_name: "f0".into(),
            block_kind: "VAR_IN_OUT".into(),
            spec: V3Spec { kind: "scalar".into(), name: "UINT".into(), c_type: Some("uint16_t".into()),
                           size: Some(2), count: None, byte_size: Some(2), length: None, element: None, members: Vec::new() },
        });
        let case = parse_testcase("Counter,UINT,7\n0.Counter,UINT,65535\n0.__DT,TIME,10\n1.Counter,UINT,0\n1.__DT,TIME,25\n",
                                  &bundle.schema()).unwrap();
        assert_eq!(case.shared["Counter"], "7");
        assert_eq!(case.frames[0].values["Counter"], "65535");
        assert_eq!(case.frames[1].dt_ns, Some(25));
        assert!(frame_records(&bundle, 1, &case.frames[1]).contains("1.Counter,UINT,0"));
        assert!(parse_testcase("0.Counter,UINT,65536\n", &bundle.schema()).is_err());
    }

    #[test]
    fn bundle_loader_rejects_missing_exchange_fields_instead_of_defaulting() {
        let root = std::env::temp_dir().join(format!("v3-bundle-load-{}-{}", std::process::id(), current_nanos()));
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("abi.json"), serde_json::json!({
            "schema_version": "semantist.abi/1.0.0",
            "target": {"name": "F", "kind": "FUNCTION", "instance_size": 0},
            "frame": {"size": 1, "align": 1, "fields": []},
            "frame_fields": [], "shared_fields": [], "state_fields": [],
        }).to_string()).unwrap();
        fs::write(root.join("state-layout.json"), serde_json::json!({
            "schema_version": "semantist.state-layout/1.0.0",
            "snapshot_schema_version": 1, "total_size": 0,
            "harness_region_index": 0, "regions": [],
        }).to_string()).unwrap();
        fs::write(root.join("goal-tasks.json"), serde_json::json!({
            "schema_version": "semantist.goal-tasks/1.0.0", "target": null, "tasks": [],
        }).to_string()).unwrap();
        fs::write(root.join("execution-contract.json"), serde_json::json!({
            "schema_version": "semantist.execution-contract/1.0.0",
            "contract_id": "contract-1", "time": {"scan_period_ns": 10},
        }).to_string()).unwrap();

        let valid = V3Bundle::load(&root, Path::new("target")).unwrap();
        assert_eq!(valid.scan_period_ns, 10);

        let mut abi: Value = serde_json::from_str(&fs::read_to_string(root.join("abi.json")).unwrap()).unwrap();
        abi.as_object_mut().unwrap().remove("frame_fields");
        fs::write(root.join("abi.json"), abi.to_string()).unwrap();
        let error = V3Bundle::load(&root, Path::new("target")).unwrap_err().to_string();
        assert!(error.contains("abi.json missing required field frame_fields"), "{error}");
        fs::remove_dir_all(root).unwrap();
    }

    fn test_bundle() -> V3Bundle {
        V3Bundle {
            dir: PathBuf::new(),
            abi: V3Abi {
                schema_version: "x".into(),
                target: V3Target { name: "F".into(), kind: "FUNCTION_BLOCK".into(), instance_size: 0 },
                frame: V3FrameLayout { size: 4, align: 2, fields: vec![] },
                frame_fields: vec![
                    V3Field { name: "Run".into(), offset: Some(0), size: Some(1), c_name: "f0".into(), block_kind: "VAR_INPUT".into(), spec: V3Spec { kind: "scalar".into(), name: "BOOL".into(), c_type: Some("uint8_t".into()), size: Some(1), count: None, byte_size: Some(1), length: None, element: None, members: Vec::new() } },
                    V3Field { name: "Speed".into(), offset: Some(2), size: Some(2), c_name: "f1".into(), block_kind: "VAR_INPUT".into(), spec: V3Spec { kind: "scalar".into(), name: "INT".into(), c_type: Some("int16_t".into()), size: Some(2), count: None, byte_size: Some(2), length: None, element: None, members: Vec::new() } },
                ],
                shared_fields: vec![], state_fields: vec![],
            },
            layout: V3Layout { total_size: 0, snapshot_schema_version: 1, harness_region_index: None, regions: vec![] },
            tasks: V3Tasks { schema_version: "x".into(), contract_id: None, target: None, single_cycle: false, unknown_cycle_effects: false, tasks: vec![] },
            contract_id: "c".into(),
            scan_period_ns: 10,
        }
    }
}
