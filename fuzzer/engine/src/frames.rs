// Canonical v3 testcase frames shared with `fuzzer/corpus/frames.py`.
//
// Text is the interchange format, while decoded field bytes stay cached in
// each Frame for the native execution and mutation hot paths. This module is
// the authoritative Rust implementation of the parser, canonical writer and
// the FNV-1a 64 frame/prefix hashes used by the Python search and corpus store.

pub const FNV_OFFSET: u64 = 14_695_981_039_346_656_037;
pub const FNV_PRIME: u64 = 1_099_511_628_211;
pub const FRAME_SEPARATOR: u8 = 0x1e;
pub const RESERVED_INIT: &str = "__INIT";
pub const RESERVED_DT: &str = "__DT";

pub fn fnv1a64(data: &[u8], seed: u64) -> u64 {
    let mut value = seed;
    for byte in data {
        value ^= u64::from(*byte);
        value = value.wrapping_mul(FNV_PRIME);
    }
    value
}

#[derive(Clone, Debug)]
pub struct Frame {
    pub index: u32,
    pub values: BTreeMap<String, String>,
    pub dt_ns: Option<u64>,
    /// Decoded field bytes keyed by field name. Native execution and raw
    /// mutation use this cache directly; semantic values are only the control
    /// representation used when choosing a typed mutation.
    pub raw_values: BTreeMap<String, Vec<u8>>,
}

impl PartialEq for Frame {
    fn eq(&self, other: &Self) -> bool {
        self.index == other.index && self.values == other.values && self.dt_ns == other.dt_ns
    }
}

impl Eq for Frame {}

impl Frame {
    pub fn new(index: u32, values: BTreeMap<String, String>, dt_ns: Option<u64>) -> Self {
        let mut frame = Self { index, values, dt_ns, raw_values: BTreeMap::new() };
        frame.refresh_raw_values();
        frame
    }

    pub fn refresh_raw_values(&mut self) {
        self.raw_values.clear();
        for (name, value) in &self.values {
            if let Some(bytes) = raw_hex_bytes(value) {
                self.raw_values.insert(name.clone(), bytes);
            }
        }
    }

    /// Populate the byte cache for legacy semantic text as well as explicit
    /// hexadecimal values. The schema is only needed at parse boundaries;
    /// native execution can then reuse the decoded bytes directly.
    pub fn refresh_raw_values_with_schema(&mut self, schema: &FrameSchema) {
        self.raw_values.clear();
        for (name, value) in &self.values {
            let kind = schema.frame_types.get(name).map(String::as_str);
            let bytes = value_bytes_for_schema(
                kind,
                value,
                schema.frame_scalar_width.get(name).copied(),
                schema.frame_string_wide.get(name).copied().unwrap_or(false),
            );
            if let Some(bytes) = bytes {
                self.raw_values.insert(name.clone(), bytes);
            }
        }
    }

    pub fn cached_raw_bytes(&self, name: &str, _current: &str) -> Option<&[u8]> {
        self.raw_values.get(name).map(Vec::as_slice)
    }

    pub fn set_raw_value(&mut self, name: String, value: String, bytes: Vec<u8>) {
        self.values.insert(name.clone(), value.clone());
        self.raw_values.insert(name, bytes);
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TestCase {
    pub init_profile: String,
    pub shared: BTreeMap<String, String>,
    pub frames: Vec<Frame>,
}

#[derive(Clone, Debug, Default)]
pub struct FrameSchema {
    pub frame_defaults: BTreeMap<String, String>,
    pub frame_types: BTreeMap<String, String>,
    pub shared_types: BTreeMap<String, String>,
    pub frame_bounds: BTreeMap<String, (i128, i128)>,
    pub shared_bounds: BTreeMap<String, (i128, i128)>,
    pub frame_string_lengths: BTreeMap<String, usize>,
    pub shared_string_lengths: BTreeMap<String, usize>,
    pub frame_string_wide: BTreeMap<String, bool>,
    pub shared_string_wide: BTreeMap<String, bool>,
    pub frame_byte_capacity: BTreeMap<String, usize>,
    pub shared_byte_capacity: BTreeMap<String, usize>,
    pub frame_scalar_width: BTreeMap<String, usize>,
    pub shared_scalar_width: BTreeMap<String, usize>,
}

fn hex_bytes(value: &str) -> Result<Option<usize>, String> {
    let Some(digits) = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X")) else {
        return Ok(None);
    };
    if digits.len() % 2 != 0 || !digits.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!("invalid hex bytes {value:?}"));
    }
    Ok(Some(digits.len() / 2))
}

/// Decode an already validated canonical `0x...` value without allocating a
/// second textual representation.  Invalid values return `None`; public
/// parsing performs the strict error reporting before this helper is used.
pub fn raw_hex_bytes(value: &str) -> Option<Vec<u8>> {
    let digits = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X"))?;
    if digits.len() % 2 != 0 || !digits.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    Some(digits.as_bytes().chunks_exact(2).map(|pair| {
        let high = (pair[0] as char).to_digit(16).expect("validated hex") as u8;
        let low = (pair[1] as char).to_digit(16).expect("validated hex") as u8;
        (high << 4) | low
    }).collect())
}

fn value_bytes_for_schema(
    kind: Option<&str>,
    value: &str,
    scalar_width: Option<usize>,
    wide_string: bool,
) -> Option<Vec<u8>> {
    if let Some(bytes) = raw_hex_bytes(value) {
        return Some(bytes);
    }
    let kind = kind?;
    if kind.starts_with("STRING") {
        return Some(value.as_bytes().to_vec());
    }
    if kind.starts_with("WSTRING") {
        return Some(value.encode_utf16().flat_map(u16::to_le_bytes).collect());
    }
    if kind == "REAL" {
        return value.parse::<f32>().ok().map(|number| number.to_le_bytes().to_vec());
    }
    if kind == "LREAL" {
        return value.parse::<f64>().ok().map(|number| number.to_le_bytes().to_vec());
    }
    if wide_string {
        return Some(value.encode_utf16().flat_map(u16::to_le_bytes).collect());
    }
    let width = scalar_width?;
    if width == 0 || width > 16 {
        return None;
    }
    let number = if let Some(hex) = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X")) {
        i128::from_str_radix(hex, 16).ok()?
    } else {
        value.parse::<i128>().ok()?
    };
    Some(number.to_le_bytes()[..width].to_vec())
}

fn check_bytes(field: &str, value: &str, capacity: Option<&usize>, width: Option<&usize>, line: usize) -> Result<bool, String> {
    let Some(digits) = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X")) else {
        if capacity.is_some() { return Err(format!("line {line}: {field} requires hex bytes")); }
        return Ok(false);
    };
    if !digits.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!("line {line}: {field} invalid hex bytes {value:?}"));
    }
    if digits.len() % 2 != 0 {
        return Err(format!("line {line}: {field} invalid hex bytes {value:?}"));
    }
    let count = digits.len() / 2;
    if let Some(&expected) = width {
        if count != expected {
            return Err(format!("line {line}: {field} raw bytes {count} require ABI width {expected}"));
        }
    }
    if let Some(&capacity) = capacity {
        if count > capacity { return Err(format!("line {line}: {field} bytes {count} exceed {capacity}")); }
        return Ok(true);
    }
    Ok(count > 0)
}

fn check_numeric_domain(field: &str, value: &str, bounds: Option<&(i128, i128)>, line: usize) -> Result<(), String> {
    let Some(&(min, max)) = bounds else { return Ok(()); };
    let text = value.trim();
    let (negative, unsigned_text) = if let Some(rest) = text.strip_prefix('-') { (true, rest) }
        else if let Some(rest) = text.strip_prefix('+') { (false, rest) }
        else { (false, text) };
    let number = if let Some(hex) = unsigned_text.strip_prefix("0x").or_else(|| unsigned_text.strip_prefix("0X")) {
        i128::from_str_radix(hex, 16).ok().map(|value| if negative { -value } else { value })
    } else {
        text.parse::<i128>().ok()
    }.ok_or_else(|| format!("line {line}: invalid integer value {value:?} for {field}"))?;
    if number < min || number > max {
        return Err(format!("line {line}: {field} value {number} outside [{min}, {max}]"));
    }
    Ok(())
}

fn check_string_domain(field: &str, value: &str, max_length: Option<&usize>, wide: bool, line: usize) -> Result<(), String> {
    let Some(&max_length) = max_length else { return Ok(()); };
    if let Some(count) = hex_bytes(value).map_err(|error| format!("line {line}: {field} {error}"))? {
        let unit = if wide { 2 } else { 1 };
        // Text values use at most N payload units. Raw values describe the
        // complete ABI object and may occupy the N+1 terminator slot.
        let capacity = if value.strip_prefix("0x").is_some() || value.strip_prefix("0X").is_some() {
            (max_length + 1) * unit
        } else {
            max_length * unit
        };
        if count % unit != 0 || count > capacity {
            return Err(format!("line {line}: {field} string bytes {count} exceed capacity or split a unit"));
        }
        return Ok(());
    }
    let length = if wide {
        value.encode_utf16().count()
    } else {
        value.as_bytes().len()
    };
    if length > max_length {
        return Err(format!(
            "line {line}: {field} string length {length} exceeds {max_length}",
        ));
    }
    Ok(())
}

fn default_for_type(kind: &str) -> String {
    if kind == "REAL" || kind == "LREAL" {
        "0.0".to_string()
    } else if kind.starts_with("STRING") || kind.starts_with("WSTRING") {
        "A".to_string()
    } else if kind.contains("ARRAY") || kind == "POINTER" || kind.starts_with("REF_TO") {
        "0x00".to_string()
    } else {
        "0".to_string()
    }
}

/// Parse and expand a canonical or handwritten testcase.
pub fn parse_testcase(text: &str, schema: &FrameSchema) -> Result<TestCase, String> {
    let mut case = TestCase {
        init_profile: "cold".to_string(),
        shared: BTreeMap::new(),
        frames: Vec::new(),
    };
    let mut previous: BTreeMap<String, String> = schema.frame_defaults.clone();
    // Keep explicit assignments separate from the expanded values.  Shared
    // VAR_IN_OUT records may appear before or after frame records, so their
    // initial values are applied in a second pass below.
    let mut explicit_fields: Vec<std::collections::HashSet<String>> = Vec::new();
    let mut expected_index: u32 = 0;
    for (line_number, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((name_part, rest)) = line.split_once(',') else {
            return Err(format!("line {}: missing comma", line_number + 1));
        };
        let (kind, value) = rest.split_once(',').unwrap_or((rest, ""));
        let name_part = name_part.trim();
        let kind = kind.trim().to_ascii_uppercase();
        let value = value.trim();
        if name_part.eq_ignore_ascii_case(RESERVED_INIT) {
            if !kind.is_empty() && kind != "ENUM" {
                return Err(format!(
                    "line {}: {RESERVED_INIT} has type {kind}, expected ENUM",
                    line_number + 1
                ));
            }
            if value != "cold" {
                return Err(format!(
                    "line {}: unsupported init profile {value:?}",
                    line_number + 1
                ));
            }
            case.init_profile = value.to_string();
            continue;
        }
        let (cycle, field) = match name_part.split_once('.') {
            Some((index, field)) => {
                let cycle = index
                    .parse::<u32>()
                    .map_err(|_| format!("line {}: bad frame index {index:?}", line_number + 1))?;
                (Some(cycle), field)
            }
            None => (None, name_part),
        };
        let Some(cycle) = cycle else {
            let canonical = schema.shared_types.keys()
                .find(|name| name.eq_ignore_ascii_case(field))
                .cloned()
                .unwrap_or_else(|| field.to_string());
            if !schema.shared_types.is_empty() && !schema.shared_types.contains_key(&canonical) {
                return Err(format!("line {}: unknown shared field {field:?}", line_number + 1));
            }
            if !kind.is_empty() {
                if let Some(expected) = schema.shared_types.get(&canonical) {
                    if expected != &kind {
                        return Err(format!(
                            "line {}: {field} has type {kind}, expected {expected}",
                            line_number + 1
                        ));
                    }
                }
            }
            let raw = check_bytes(&canonical, value, schema.shared_byte_capacity.get(&canonical), schema.shared_scalar_width.get(&canonical), line_number + 1)?;
            if !raw { check_numeric_domain(&canonical, value, schema.shared_bounds.get(&canonical), line_number + 1)?; }
            check_string_domain(&canonical, value, schema.shared_string_lengths.get(&canonical),
                                schema.shared_string_wide.get(&canonical).copied().unwrap_or(false),
                                line_number + 1)?;
            case.shared.insert(canonical, if value.starts_with("0x") || value.starts_with("0X") { value.to_ascii_lowercase() } else { value.to_string() });
            continue;
        };
        let same_frame = case.frames.last().map(|frame| frame.index) == Some(cycle);
        if !same_frame && cycle != expected_index {
            return Err(format!(
                "line {}: expected frame {expected_index}, got {cycle}",
                line_number + 1
            ));
        }
        if case.frames.len() <= cycle as usize {
            let mut values = previous.clone();
            for key in schema.frame_defaults.keys() {
                values.entry(key.clone()).or_insert_with(||
                    default_for_type(schema.frame_types.get(key).map(String::as_str).unwrap_or("")));
            }
            case.frames.push(Frame::new(cycle, values, None));
            explicit_fields.push(std::collections::HashSet::new());
            expected_index = cycle + 1;
        }
        if field.eq_ignore_ascii_case(RESERVED_DT) {
            if !kind.is_empty() && kind != "TIME" {
                return Err(format!(
                    "line {}: {RESERVED_DT} has type {kind}, expected TIME",
                    line_number + 1
                ));
            }
            let dt = value
                .parse::<u64>()
                .map_err(|_| format!("line {}: bad __DT {value:?}", line_number + 1))?;
            case.frames.last_mut().expect("frame").dt_ns = Some(dt);
            continue;
        }
        let canonical = schema.frame_types.keys()
            .find(|name| name.eq_ignore_ascii_case(field))
            .cloned()
            .unwrap_or_else(|| field.to_string());
        if !schema.frame_types.is_empty() && !schema.frame_types.contains_key(&canonical) {
            return Err(format!("line {}: unknown frame field {field:?}", line_number + 1));
        }
        if !kind.is_empty() {
            if let Some(expected) = schema.frame_types.get(&canonical) {
                if expected != &kind {
                    return Err(format!(
                        "line {}: {field} has type {kind}, expected {expected}",
                        line_number + 1
                    ));
                }
            }
        }
        let raw = check_bytes(&canonical, value, schema.frame_byte_capacity.get(&canonical), schema.frame_scalar_width.get(&canonical), line_number + 1)?;
        if !raw { check_numeric_domain(&canonical, value, schema.frame_bounds.get(&canonical), line_number + 1)?; }
        check_string_domain(&canonical, value, schema.frame_string_lengths.get(&canonical),
                            schema.frame_string_wide.get(&canonical).copied().unwrap_or(false),
                            line_number + 1)?;
        let frame = case.frames.last_mut().expect("frame");
        frame.values.insert(canonical.clone(), if value.starts_with("0x") || value.starts_with("0X") { value.to_ascii_lowercase() } else { value.to_string() });
        frame.raw_values.remove(&canonical);
        if let Some(bytes) = raw_hex_bytes(value) {
            frame.raw_values.insert(canonical.clone(), bytes);
        }
        explicit_fields[cycle as usize].insert(canonical);
        if field.eq_ignore_ascii_case(RESERVED_DT) {
            frame.dt_ns = Some(value.parse::<u64>().unwrap_or(0));
        }
        previous = frame.values.clone();
    }
    // Python's canonical parser seeds action fields backed by shared
    // VAR_IN_OUT storage from the shared record before expanding frame
    // inheritance.  Rebuild the expanded values here so Rust and Python use
    // the same state even when the shared record is written after a frame.
    let mut inherited = schema.frame_defaults.clone();
    for (name, value) in &case.shared {
        if schema.frame_defaults.contains_key(name) {
            inherited.insert(name.clone(), value.clone());
        }
    }
    for (index, frame) in case.frames.iter_mut().enumerate() {
        let mut values = inherited.clone();
        for name in &explicit_fields[index] {
            if let Some(value) = frame.values.get(name) {
                values.insert(name.clone(), value.clone());
            }
        }
        for key in schema.frame_defaults.keys() {
            values.entry(key.clone()).or_insert_with(||
                default_for_type(schema.frame_types.get(key).map(String::as_str).unwrap_or("")));
        }
        frame.values = values;
        frame.refresh_raw_values_with_schema(schema);
        inherited = frame.values.clone();
    }
    for frame in &case.frames {
        for (name, default) in &schema.frame_defaults {
            if !frame.values.contains_key(name) {
                return Err(format!("frame {}: missing {name}", frame.index));
            }
            let _ = default;
        }
    }
    Ok(case)
}

impl TestCase {
    pub fn canonical(&self, include_dt: bool) -> String {
        let mut lines = Vec::new();
        if self.init_profile != "cold" {
            lines.push(format!("{RESERVED_INIT},ENUM,{}", self.init_profile));
        }
        for (name, value) in &self.shared {
            lines.push(format!("{name},,{value}"));
        }
        for frame in &self.frames {
            for (name, value) in &frame.values {
                lines.push(format!("{}.{name},,{}", frame.index, value));
            }
            if include_dt {
                if let Some(dt) = frame.dt_ns {
                    lines.push(format!("{}.{RESERVED_DT},TIME,{dt}", frame.index));
                }
            }
        }
        if lines.is_empty() {
            String::new()
        } else {
            lines.join("\n") + "\n"
        }
    }

    pub fn frame_hashes(&self) -> Vec<u64> {
        self.frames
            .iter()
            .map(|frame| {
                let mut text = String::new();
                for (name, value) in &frame.values {
                    text.push_str(name);
                    text.push('=');
                    text.push_str(value);
                    text.push(';');
                }
                if let Some(dt) = frame.dt_ns {
                    text.push_str("__DT=");
                    text.push_str(&dt.to_string());
                    text.push(';');
                }
                fnv1a64(text.as_bytes(), FNV_OFFSET)
            })
            .collect()
    }

    pub fn prefix_hashes(&self) -> Vec<u64> {
        let mut rolling = FNV_OFFSET;
        let mut result = Vec::new();
        for hash in self.frame_hashes() {
            let mut bytes = hash.to_le_bytes().to_vec();
            bytes.push(FRAME_SEPARATOR);
            rolling = fnv1a64(&bytes, rolling);
            result.push(rolling);
        }
        result
    }
}

fn sample_schema() -> FrameSchema {
    let mut schema = FrameSchema::default();
    schema.frame_defaults.insert("A".into(), "0".into());
    schema.frame_defaults.insert("B".into(), "0".into());
    schema.frame_types.insert("A".into(), "BOOL".into());
    schema.frame_types.insert("B".into(), "INT".into());
    schema
}

#[cfg(test)]
mod frames_tests {
    use super::*;

    #[test]
    fn expands_and_hashes_like_the_python_parser() {
        let case = parse_testcase("0.A,BOOL,1\n1.B,INT,7\n", &sample_schema()).unwrap();
        assert_eq!(case.canonical(false), "0.A,,1\n0.B,,0\n1.A,,1\n1.B,,7\n");
        assert_eq!(
            case.frame_hashes(),
            vec![0xe66d_db1f_1e0c_f68b, 0xe65c_db1f_1dfe_8358]
        );
        assert_eq!(
            case.prefix_hashes(),
            vec![0xe1d6_e667_81a0_443b, 0x0389_1823_f001_f0df]
        );
    }

    #[test]
    fn rejects_non_consecutive_frames_and_unknown_fields() {
        assert!(parse_testcase("0.A,BOOL,1\n2.B,INT,7\n", &sample_schema()).is_err());
        assert!(parse_testcase("0.NOPE,BOOL,1\n", &sample_schema()).is_err());
    }

    #[test]
    fn omitted_fields_inherit_the_previous_frame() {
        let case = parse_testcase("0.A,BOOL,1\n1.B,INT,7\n", &sample_schema()).unwrap();
        assert_eq!(case.frames[1].values.get("A").map(String::as_str), Some("1"));
        assert_eq!(case.frames[1].values.get("B").map(String::as_str), Some("7"));
    }

    #[test]
    fn shared_var_in_out_value_seeds_frames_even_when_frame_is_omitted() {
        let mut schema = sample_schema();
        schema.frame_defaults.insert("Counter".into(), "0".into());
        schema.frame_types.insert("Counter".into(), "UINT".into());
        schema.shared_types.insert("Counter".into(), "UINT".into());
        let case = parse_testcase(
            "Counter,UINT,7\n0.A,BOOL,1\n1.B,INT,7\n",
            &schema,
        )
        .unwrap();
        assert_eq!(case.frames[0].values.get("Counter").map(String::as_str), Some("7"));
        assert_eq!(case.frames[1].values.get("Counter").map(String::as_str), Some("7"));
    }

    #[test]
    fn explicit_var_in_out_value_overrides_shared_seed_and_is_inherited() {
        let mut schema = sample_schema();
        schema.frame_defaults.insert("Counter".into(), "0".into());
        schema.frame_types.insert("Counter".into(), "UINT".into());
        schema.shared_types.insert("Counter".into(), "UINT".into());
        let case = parse_testcase(
            "0.Counter,UINT,2\nCounter,UINT,7\n1.A,BOOL,1\n",
            &schema,
        )
        .unwrap();
        assert_eq!(case.frames[0].values.get("Counter").map(String::as_str), Some("2"));
        assert_eq!(case.frames[1].values.get("Counter").map(String::as_str), Some("2"));
    }

    #[test]
    fn dt_only_frame_is_retained() {
        let case = parse_testcase("0.__DT,TIME,10\n1.__DT,TIME,20\n", &FrameSchema::default()).unwrap();
        assert_eq!(case.frames.len(), 2);
        assert_eq!(case.frames[0].dt_ns, Some(10));
        assert_eq!(case.frames[1].dt_ns, Some(20));
    }

    #[test]
    fn init_profile_matches_python_cold_grammar() {
        assert!(parse_testcase("__INIT,ENUM,warm\n0.A,BOOL,1\n", &sample_schema()).is_err());
        assert!(parse_testcase("__INIT,ENUM,\n0.A,BOOL,1\n", &sample_schema()).is_err());
        assert!(parse_testcase("__INIT,INT,cold\n0.A,BOOL,1\n", &sample_schema()).is_err());
        assert!(parse_testcase("__INIT,ENUM,cold\n0.A,BOOL,1\n", &sample_schema()).is_ok());
        assert!(parse_testcase("__INIT,enum,cold\n0.A,bool,1\n", &sample_schema()).is_ok());
        let lower = parse_testcase("0.a,bool,1\n", &sample_schema()).unwrap();
        assert_eq!(lower.frames[0].values.get("A"), Some(&"1".to_string()));
        assert!(parse_testcase("0.__DT,INT,10\n", &sample_schema()).is_err());
        assert!(parse_testcase("0.__DT,TIME,10\n", &sample_schema()).is_ok());
        assert!(parse_testcase("0.__DT,time,10\n", &sample_schema()).is_ok());
    }

    #[test]
    fn unsigned_domain_rejects_negative_and_overflow() {
        let mut schema = FrameSchema::default();
        schema.frame_defaults.insert("U".into(), "0".into());
        schema.frame_types.insert("U".into(), "UINT".into());
        schema.frame_bounds.insert("U".into(), (0, 65535));
        assert!(parse_testcase("0.U,UINT,65535\n", &schema).is_ok());
        assert!(parse_testcase("0.U,UINT,-1\n", &schema).is_err());
        assert!(parse_testcase("0.U,UINT,65536\n", &schema).is_err());
    }

    #[test]
    fn string_domain_rejects_values_longer_than_abi_capacity() {
        let mut schema = FrameSchema::default();
        schema.frame_defaults.insert("Text".into(), "A".into());
        schema.frame_types.insert("Text".into(), "STRING".into());
        schema.frame_string_lengths.insert("Text".into(), 3);
        assert!(parse_testcase("0.Text,STRING,ABC\n", &schema).is_ok());
        assert!(parse_testcase("0.Text,STRING,ABCD\n", &schema).is_err());
    }

    #[test]
    fn raw_field_bytes_preserve_storage_and_validate_decoded_length() {
        let mut schema = FrameSchema::default();
        schema.frame_defaults.insert("B".into(), "0".into());
        schema.frame_types.insert("B".into(), "BOOL".into());
        schema.frame_bounds.insert("B".into(), (0, 1));
        schema.frame_scalar_width.insert("B".into(), 1);
        schema.frame_defaults.insert("S".into(), "A".into());
        schema.frame_types.insert("S".into(), "STRING".into());
        schema.frame_string_lengths.insert("S".into(), 2);
        let case = parse_testcase("0.B,BOOL,0X03\n0.S,STRING,0x80ff\n", &schema).unwrap();
        assert_eq!(case.frames[0].values["B"], "0x03");
        assert_eq!(case.frames[0].values["S"], "0x80ff");
        assert_eq!(case.frames[0].cached_raw_bytes("B", "0x03"), Some([0x03].as_slice()));
        assert_eq!(case.frames[0].cached_raw_bytes("S", "0x80ff"), Some([0x80, 0xff].as_slice()));
        assert_eq!(case.frames[0].cached_raw_bytes("S", "0x00"), Some([0x80, 0xff].as_slice()));
        assert!(parse_testcase("0.B,BOOL,0x3\n", &schema).is_err());
        assert!(parse_testcase("0.S,STRING,0x80ff01\n", &schema).is_ok());
        assert!(parse_testcase("0.S,STRING,0x8\n", &schema).is_err());
    }

    #[test]
    fn semantic_text_is_decoded_once_into_the_frame_byte_cache() {
        let mut schema = FrameSchema::default();
        schema.frame_defaults.insert("Text".into(), "A".into());
        schema.frame_types.insert("Text".into(), "STRING".into());
        schema.frame_string_lengths.insert("Text".into(), 4);
        schema.frame_defaults.insert("Wide".into(), "A".into());
        schema.frame_types.insert("Wide".into(), "WSTRING".into());
        schema.frame_string_lengths.insert("Wide".into(), 4);
        schema.frame_string_wide.insert("Wide".into(), true);
        schema.frame_defaults.insert("Count".into(), "0".into());
        schema.frame_types.insert("Count".into(), "UINT".into());
        schema.frame_scalar_width.insert("Count".into(), 2);
        let case = parse_testcase(
            "0.Text,STRING,Hi\n0.Wide,WSTRING,\u{1f600}\n0.Count,UINT,258\n",
            &schema,
        ).unwrap();
        assert_eq!(case.frames[0].cached_raw_bytes("Text", "Hi"), Some(b"Hi".as_slice()));
        assert_eq!(case.frames[0].cached_raw_bytes("Wide", "\u{1f600}"), Some([0x3d, 0xd8, 0x00, 0xde].as_slice()));
        assert_eq!(case.frames[0].cached_raw_bytes("Count", "258"), Some([0x02, 0x01].as_slice()));
    }

    #[test]
    fn wstring_domain_counts_utf16_code_units() {
        let mut schema = FrameSchema::default();
        schema.frame_defaults.insert("Wide".into(), "A".into());
        schema.frame_types.insert("Wide".into(), "WSTRING".into());
        schema.frame_string_lengths.insert("Wide".into(), 2);
        schema.frame_string_wide.insert("Wide".into(), true);
        assert!(parse_testcase("0.Wide,WSTRING,AB\n", &schema).is_ok());
        // U+1F600 occupies two UTF-16 code units, so adding A exceeds 2.
        assert!(parse_testcase("0.Wide,WSTRING,\u{1f600}A\n", &schema).is_err());
    }

    #[test]
    fn fnv_matches_the_empty_reference() {
        assert_eq!(fnv1a64(b"", FNV_OFFSET), 0xcbf2_9ce4_8422_2325);
    }
}
