#[derive(Debug, serde::Deserialize)]
struct SeedGenerationProject {
    #[serde(default)]
    pous: Vec<SeedGenerationPou>,
}

#[derive(Debug, serde::Deserialize)]
struct SeedGenerationPou {
    pou: SeedGenerationPouDescriptor,
    environment: SeedGenerationEnvironment,
}

#[derive(Debug, serde::Deserialize)]
struct SeedGenerationPouDescriptor {
    qualified_name: String,
    #[serde(default)]
    stateful: bool,
}

#[derive(Debug, Default, serde::Deserialize)]
struct SeedGenerationEnvironment {
    #[serde(default)]
    symbols: Vec<SeedGenerationSymbol>,
    #[serde(default)]
    types: Vec<SeedGenerationType>,
    #[serde(default)]
    expressions: Vec<serde_json::Value>,
    #[serde(default)]
    persistent_state: Option<SeedGenerationPersistentState>,
}

#[derive(Debug, serde::Deserialize)]
struct SeedGenerationSymbol {
    name: String,
    qualified_name: String,
    type_name: String,
    role: String,
    #[serde(default)]
    initial_value: Option<String>,
}

#[derive(Clone, Debug, Default, serde::Deserialize)]
struct SeedGenerationType {
    name: String,
    kind: String,
    #[serde(default)]
    bit_width: Option<u32>,
    #[serde(default)]
    semantic_bit_width: Option<u32>,
    #[serde(default)]
    signed: Option<bool>,
    #[serde(default)]
    dimensions: Vec<SeedGenerationDimension>,
    #[serde(default)]
    string_capacity: Option<i64>,
}

#[derive(Clone, Debug, Default, serde::Deserialize)]
struct SeedGenerationDimension {
    #[serde(default)]
    lower: Option<i64>,
    #[serde(default)]
    upper: Option<i64>,
}

#[derive(Debug, Default, serde::Deserialize)]
struct SeedGenerationPersistentState {
    #[serde(default)]
    cycle_inputs: Vec<String>,
    #[serde(default)]
    inout_symbols: Vec<String>,
    #[serde(default)]
    configuration_symbols: Vec<String>,
}

#[derive(Clone, Debug)]
struct SeedGenerationRecord {
    cycle: Option<usize>,
    name: String,
    ty: String,
    value: String,
}

#[derive(Clone, Debug)]
struct SeedGenerationField {
    name: String,
    ty: String,
    cycle_scoped: bool,
    kind: String,
    initial: String,
    zero: String,
    one: String,
    negative: String,
    lower: String,
    upper: String,
    constants: Vec<String>,
    must_be_nonzero: bool,
}

#[derive(Clone, Copy, Debug)]
enum SeedGenerationProfile {
    Safe,
    Initial,
    Zero,
    One,
    Negative,
    Lower,
    Upper,
    Constant,
}

#[derive(Debug)]
struct SemanticInitialGenerator {
    inputs: std::collections::VecDeque<BytesInput>,
    stateful: bool,
}

impl SemanticInitialGenerator {
    fn from_stg(
        model_path: &Path,
        function: &str,
        seed_dir: &Path,
        plan: &semantic::SemanticTaskPlan,
    ) -> Result<Self, Error> {
        let model_bytes = fs::read(model_path).map_err(|error| {
            Error::illegal_argument(format!(
                "unable to read semantic seed model {}: {error}",
                model_path.display()
            ))
        })?;
        let project: SeedGenerationProject = serde_json::from_slice(&model_bytes).map_err(|error| {
            Error::illegal_argument(format!(
                "unable to parse semantic seed model {}: {error}",
                model_path.display()
            ))
        })?;
        let pou = project
            .pous
            .iter()
            .find(|pou| pou.pou.qualified_name.eq_ignore_ascii_case(function))
            .ok_or_else(|| {
                Error::illegal_argument(format!(
                    "semantic seed model contains no POU named {function}"
                ))
            })?;
        let base_records = read_seed_generation_base(seed_dir)?;
        if base_records.is_empty() {
            return Err(Error::illegal_argument(format!(
                "semantic seed generation requires a valid Harness seed in {}",
                seed_dir.display()
            )));
        }
        let constants = seed_generation_constants(&pou.environment.expressions);
        let nonzero_fields = seed_generation_nonzero_fields(&pou.environment.expressions);
        let fields = seed_generation_fields(
            &base_records,
            &pou.environment,
            &constants,
            &nonzero_fields,
        );
        if fields.is_empty() {
            return Err(Error::illegal_argument(
                "semantic seed generation found no fuzzable input fields",
            ));
        }
        let cycle_count = seed_generation_cycle_count(
            pou.pou.stateful,
            &base_records,
            plan,
            &pou.pou.qualified_name,
        );
        let limit = if pou.pou.stateful { 24 } else { 16 };
        let candidates = build_semantic_seed_candidates(&fields, pou.pou.stateful, cycle_count, limit);
        Ok(Self {
            inputs: candidates.into(),
            stateful: pou.pou.stateful,
        })
    }

    fn len(&self) -> usize {
        self.inputs.len()
    }
}

impl<S> libafl::generators::Generator<BytesInput, S> for SemanticInitialGenerator {
    fn generate(&mut self, _state: &mut S) -> Result<BytesInput, Error> {
        self.inputs.pop_front().ok_or_else(|| {
            Error::empty("semantic initial generator exhausted its bounded candidate set")
        })
    }
}

fn parse_seed_generation_records(bytes: &[u8]) -> Vec<SeedGenerationRecord> {
    let Ok(text) = std::str::from_utf8(bytes) else {
        return Vec::new();
    };
    text.lines()
        .filter(|line| !line.trim().is_empty())
        .filter_map(|line| {
            let mut parts = line.splitn(3, ',');
            let raw_name = parts.next()?.trim();
            let ty = parts.next()?.trim();
            let value = parts.next()?.trim();
            if raw_name.is_empty() || ty.is_empty() {
                return None;
            }
            let (cycle, name) = raw_name
                .split_once('.')
                .and_then(|(cycle, name)| {
                    cycle
                        .parse::<usize>()
                        .ok()
                        .filter(|_| !name.trim().is_empty())
                        .map(|cycle| (Some(cycle), name.trim()))
                })
                .unwrap_or((None, raw_name));
            Some(SeedGenerationRecord {
                cycle,
                name: name.to_string(),
                ty: ty.to_ascii_uppercase(),
                value: value.to_string(),
            })
        })
        .collect()
}

fn read_seed_generation_base(seed_dir: &Path) -> Result<Vec<SeedGenerationRecord>, Error> {
    let mut paths = fs::read_dir(seed_dir)
        .map_err(|error| {
            Error::illegal_argument(format!("unable to read {}: {error}", seed_dir.display()))
        })?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_file())
        .collect::<Vec<_>>();
    paths.sort();
    let mut best = Vec::new();
    for path in paths {
        let Ok(bytes) = fs::read(path) else {
            continue;
        };
        let records = parse_seed_generation_records(&bytes);
        if records.len() > best.len() {
            best = records;
        }
    }
    Ok(best)
}

fn seed_generation_constants(
    expressions: &[serde_json::Value],
) -> std::collections::BTreeMap<String, Vec<String>> {
    let mut constants = std::collections::BTreeMap::<String, Vec<String>>::new();
    for expression in expressions {
        let Some(kind) = expression.get("kind") else {
            continue;
        };
        if kind.get("kind").and_then(serde_json::Value::as_str) != Some("literal") {
            continue;
        }
        let Some(literal) = kind.get("value") else {
            continue;
        };
        let value = match literal.get("kind").and_then(serde_json::Value::as_str) {
            Some("integer" | "real" | "string" | "wide_string") => literal
                .get("value")
                .and_then(|value| value.as_str().map(str::to_string)),
            Some("boolean") => literal
                .get("value")
                .and_then(serde_json::Value::as_bool)
                .map(|value| if value { "1" } else { "0" }.to_string()),
            _ => None,
        };
        let Some(value) = value else {
            continue;
        };
        let type_name = expression
            .get("type_name")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .to_ascii_uppercase();
        let values = constants.entry(type_name).or_default();
        if values.len() < 12 && !values.contains(&value) {
            values.push(value);
        }
    }
    constants
}

fn seed_generation_nonzero_fields(expressions: &[serde_json::Value]) -> HashSet<String> {
    let by_id = expressions
        .iter()
        .filter_map(|expression| {
            expression
                .get("id")
                .and_then(serde_json::Value::as_str)
                .map(|id| (id, expression))
        })
        .collect::<HashMap<_, _>>();
    let mut fields = HashSet::new();
    for expression in expressions {
        let operator = expression
            .get("kind")
            .and_then(|kind| kind.get("operator"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default();
        if !matches!(operator.to_ascii_uppercase().as_str(), "/" | "DIV" | "MOD") {
            continue;
        }
        let Some(denominator_id) = expression
            .get("operands")
            .and_then(serde_json::Value::as_array)
            .and_then(|operands| operands.get(1))
            .and_then(serde_json::Value::as_str)
        else {
            continue;
        };
        let Some(denominator) = by_id.get(denominator_id) else {
            continue;
        };
        for read in denominator
            .get("reads")
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(serde_json::Value::as_str)
        {
            fields.insert(read.rsplit('.').next().unwrap_or(read).to_ascii_uppercase());
        }
    }
    fields
}

fn seed_generation_fields(
    records: &[SeedGenerationRecord],
    environment: &SeedGenerationEnvironment,
    constants: &std::collections::BTreeMap<String, Vec<String>>,
    nonzero_fields: &HashSet<String>,
) -> Vec<SeedGenerationField> {
    let symbols = environment
        .symbols
        .iter()
        .filter(|symbol| matches!(symbol.role.as_str(), "input" | "in_out"))
        .map(|symbol| (symbol.name.to_ascii_uppercase(), symbol))
        .collect::<std::collections::BTreeMap<_, _>>();
    let types = environment
        .types
        .iter()
        .map(|ty| (ty.name.to_ascii_uppercase(), ty))
        .collect::<std::collections::BTreeMap<_, _>>();
    let persistent = environment.persistent_state.as_ref();
    let cycle_inputs = persistent
        .map(|state| {
            state
                .cycle_inputs
                .iter()
                .map(|name| name.to_ascii_uppercase())
                .collect::<HashSet<_>>()
        })
        .unwrap_or_default();
    let configuration = persistent
        .map(|state| {
            state
                .configuration_symbols
                .iter()
                .chain(state.inout_symbols.iter())
                .map(|name| name.to_ascii_uppercase())
                .collect::<HashSet<_>>()
        })
        .unwrap_or_default();

    let mut order = Vec::new();
    let mut records_by_name = std::collections::BTreeMap::<String, Vec<&SeedGenerationRecord>>::new();
    for record in records {
        let key = record.name.to_ascii_uppercase();
        if !records_by_name.contains_key(&key) {
            order.push(key.clone());
        }
        records_by_name.entry(key).or_default().push(record);
    }

    order
        .into_iter()
        .filter_map(|key| {
            let field_records = records_by_name.get(&key)?;
            let base = field_records
                .iter()
                .find(|record| record.cycle.is_none())
                .or_else(|| field_records.iter().min_by_key(|record| record.cycle))?;
            let symbol = symbols.get(&key).copied();
            let type_name = symbol
                .map(|symbol| symbol.type_name.as_str())
                .unwrap_or(base.ty.as_str());
            let type_fact = types.get(&type_name.to_ascii_uppercase()).copied();
            let qualified = symbol
                .map(|symbol| symbol.qualified_name.to_ascii_uppercase())
                .unwrap_or_default();
            let observed_cycle = field_records.iter().any(|record| record.cycle.is_some());
            let cycle_scoped = if configuration.contains(&qualified) {
                false
            } else if cycle_inputs.contains(&qualified) {
                true
            } else {
                observed_cycle
            };
            Some(seed_generation_field(
                base,
                symbol,
                type_fact,
                cycle_scoped,
                constants
                    .get(&type_name.to_ascii_uppercase())
                    .cloned()
                    .unwrap_or_default(),
                nonzero_fields.contains(&key),
            ))
        })
        .collect()
}

fn seed_generation_field(
    base: &SeedGenerationRecord,
    symbol: Option<&SeedGenerationSymbol>,
    type_fact: Option<&SeedGenerationType>,
    cycle_scoped: bool,
    mut constants: Vec<String>,
    must_be_nonzero: bool,
) -> SeedGenerationField {
    let kind = type_fact
        .map(|ty| ty.kind.to_ascii_lowercase())
        .unwrap_or_else(|| seed_generation_kind_from_token(&base.ty));
    let initial = symbol
        .and_then(|symbol| symbol.initial_value.as_deref())
        .and_then(|value| normalize_seed_generation_initial(value, &kind))
        .unwrap_or_else(|| base.value.clone());
    let (zero, one, negative, lower, upper) = seed_generation_values(&kind, type_fact);
    constants.retain(|value| seed_generation_value_is_valid(value, &kind));
    constants.sort();
    constants.dedup();
    constants.truncate(8);
    SeedGenerationField {
        name: base.name.clone(),
        ty: base.ty.clone(),
        cycle_scoped,
        kind,
        initial,
        zero,
        one,
        negative,
        lower,
        upper,
        constants,
        must_be_nonzero,
    }
}

fn seed_generation_kind_from_token(token: &str) -> String {
    match token.to_ascii_uppercase().as_str() {
        "BOOL" => "boolean",
        "REAL" | "LREAL" => "float",
        "STRING" | "WSTRING" => "string",
        "ARRAY" => "array",
        "POINTER" => "pointer",
        _ => "integer",
    }
    .to_string()
}

fn normalize_seed_generation_initial(value: &str, kind: &str) -> Option<String> {
    let value = value.trim();
    match kind {
        "boolean" if value.eq_ignore_ascii_case("true") => Some("1".to_string()),
        "boolean" if value.eq_ignore_ascii_case("false") => Some("0".to_string()),
        "boolean" => value.parse::<i64>().ok().map(|value| i64::from(value != 0).to_string()),
        "integer" | "enum" | "subrange" => value.parse::<i128>().ok().map(|value| value.to_string()),
        "float" => value.parse::<f64>().ok().map(|value| value.to_string()),
        "string" => Some(value.trim_matches(['\'', '"']).to_string()),
        _ => None,
    }
}

fn seed_generation_values(
    kind: &str,
    type_fact: Option<&SeedGenerationType>,
) -> (String, String, String, String, String) {
    match kind {
        "boolean" => (
            "0".to_string(),
            "1".to_string(),
            "0".to_string(),
            "0".to_string(),
            "1".to_string(),
        ),
        "float" => (
            "0.0".to_string(),
            "1.0".to_string(),
            "-1.0".to_string(),
            "-1.0".to_string(),
            "1.0".to_string(),
        ),
        "string" => {
            let capacity = type_fact
                .and_then(|ty| ty.string_capacity)
                .unwrap_or(8)
                .clamp(1, 16) as usize;
            (
                String::new(),
                "A".to_string(),
                "B".to_string(),
                String::new(),
                "A".repeat(capacity),
            )
        }
        "array" | "pointer" => {
            let bytes = type_fact
                .map(|ty| {
                    ty.dimensions
                        .iter()
                        .filter_map(|dimension| Some((dimension.upper? - dimension.lower? + 1).max(1)))
                        .product::<i64>()
                })
                .unwrap_or(1)
                .clamp(1, 8) as usize;
            (
                "0x00".to_string(),
                "0x01".to_string(),
                "0xff".to_string(),
                "0x00".to_string(),
                format!("0x{}", "ff".repeat(bytes)),
            )
        }
        _ => {
            let width = type_fact
                .and_then(|ty| ty.semantic_bit_width.or(ty.bit_width))
                .unwrap_or(32)
                .clamp(1, 64);
            let signed = type_fact.and_then(|ty| ty.signed).unwrap_or(true);
            let (lower, upper) = if signed {
                let magnitude = 1_i128 << (width - 1);
                (-magnitude, magnitude - 1)
            } else {
                (0, ((1_u128 << width) - 1).min(i128::MAX as u128) as i128)
            };
            (
                "0".to_string(),
                "1".to_string(),
                if signed { "-1" } else { "0" }.to_string(),
                lower.to_string(),
                upper.to_string(),
            )
        }
    }
}

fn seed_generation_value_is_valid(value: &str, kind: &str) -> bool {
    match kind {
        "boolean" => value == "0" || value == "1" || value.eq_ignore_ascii_case("true") || value.eq_ignore_ascii_case("false"),
        "integer" | "enum" | "subrange" => value.parse::<i128>().is_ok(),
        "float" => value.parse::<f64>().is_ok(),
        "string" => true,
        _ => false,
    }
}

fn seed_generation_cycle_count(
    stateful: bool,
    records: &[SeedGenerationRecord],
    plan: &semantic::SemanticTaskPlan,
    pou: &str,
) -> usize {
    if !stateful {
        return 1;
    }
    let existing = records
        .iter()
        .filter_map(|record| record.cycle)
        .max()
        .map(|cycle| cycle + 1)
        .unwrap_or(2);
    let semantic = plan
        .tasks
        .iter()
        .filter(|task| task.pou.eq_ignore_ascii_case(pou))
        .flat_map(|task| task.temporal_requirements.iter())
        .map(|requirement| requirement.minimum_cycles)
        .max()
        .unwrap_or(2);
    existing.max(semantic).max(2).min(8)
}

fn field_profile_value(field: &SeedGenerationField, profile: SeedGenerationProfile) -> String {
    match profile {
        SeedGenerationProfile::Safe => {
            if field.must_be_nonzero {
                field.one.clone()
            } else {
                field.initial.clone()
            }
        }
        SeedGenerationProfile::Initial => field.initial.clone(),
        SeedGenerationProfile::Zero => field.zero.clone(),
        SeedGenerationProfile::One => field.one.clone(),
        SeedGenerationProfile::Negative => field.negative.clone(),
        SeedGenerationProfile::Lower => field.lower.clone(),
        SeedGenerationProfile::Upper => field.upper.clone(),
        SeedGenerationProfile::Constant => field
            .constants
            .first()
            .cloned()
            .unwrap_or_else(|| field.initial.clone()),
    }
}

fn serialize_semantic_seed(
    fields: &[SeedGenerationField],
    stateful: bool,
    cycle_count: usize,
    profile: SeedGenerationProfile,
    field_override: Option<(&str, &str)>,
    cycle_pattern: Option<(&str, &[String])>,
) -> Vec<u8> {
    let mut output = String::new();
    for field in fields {
        if stateful && field.cycle_scoped {
            for cycle in 0..cycle_count {
                let value = cycle_pattern
                    .filter(|(name, _)| field.name.eq_ignore_ascii_case(name))
                    .and_then(|(_, values)| values.get(cycle))
                    .cloned()
                    .or_else(|| {
                        field_override
                            .filter(|(name, _)| field.name.eq_ignore_ascii_case(name))
                            .map(|(_, value)| value.to_string())
                    })
                    .unwrap_or_else(|| field_profile_value(field, profile));
                output.push_str(&format!("{cycle}.{},{},{}\n", field.name, field.ty, value));
            }
        } else {
            let value = field_override
                .filter(|(name, _)| field.name.eq_ignore_ascii_case(name))
                .map(|(_, value)| value.to_string())
                .unwrap_or_else(|| field_profile_value(field, profile));
            output.push_str(&format!("{},{},{}\n", field.name, field.ty, value));
        }
    }
    output.into_bytes()
}

fn build_semantic_seed_candidates(
    fields: &[SeedGenerationField],
    stateful: bool,
    cycle_count: usize,
    limit: usize,
) -> Vec<BytesInput> {
    let mut bytes = Vec::new();
    for profile in [
        SeedGenerationProfile::Safe,
        SeedGenerationProfile::Initial,
        SeedGenerationProfile::Zero,
        SeedGenerationProfile::One,
        SeedGenerationProfile::Negative,
        SeedGenerationProfile::Lower,
        SeedGenerationProfile::Upper,
        SeedGenerationProfile::Constant,
    ] {
        bytes.push(serialize_semantic_seed(
            fields,
            stateful,
            cycle_count,
            profile,
            None,
            None,
        ));
    }

    for field in fields {
        if bytes.len() >= limit {
            break;
        }
        bytes.push(serialize_semantic_seed(
            fields,
            stateful,
            cycle_count,
            SeedGenerationProfile::Initial,
            Some((&field.name, &field.upper)),
            None,
        ));
        if bytes.len() >= limit {
            break;
        }
        if let Some(constant) = field.constants.first() {
            bytes.push(serialize_semantic_seed(
                fields,
                stateful,
                cycle_count,
                SeedGenerationProfile::Initial,
                Some((&field.name, constant)),
                None,
            ));
        }
    }

    if stateful {
        for field in fields.iter().filter(|field| field.cycle_scoped) {
            if bytes.len() >= limit {
                break;
            }
            let pattern = if field.kind == "boolean" {
                (0..cycle_count)
                    .map(|cycle| if cycle % 2 == 0 { "0" } else { "1" }.to_string())
                    .collect::<Vec<_>>()
            } else {
                let values = [field.initial.clone(), field.one.clone(), field.upper.clone()];
                (0..cycle_count)
                    .map(|cycle| values[cycle % values.len()].clone())
                    .collect::<Vec<_>>()
            };
            bytes.push(serialize_semantic_seed(
                fields,
                stateful,
                cycle_count,
                SeedGenerationProfile::Initial,
                None,
                Some((&field.name, &pattern)),
            ));
        }
    }

    let mut seen = HashSet::new();
    bytes
        .into_iter()
        .filter(|candidate| seen.insert(content_hash(candidate)))
        .take(limit)
        .map(BytesInput::new)
        .collect()
}

#[cfg(test)]
mod semantic_generation_tests {
    use super::*;

    fn test_model(stateful: bool) -> serde_json::Value {
        serde_json::json!({
            "pous": [{
                "pou": {"qualified_name": "P", "stateful": stateful},
                "environment": {
                    "symbols": [
                        {"name": "X", "qualified_name": "P.X", "type_name": "INT", "role": "input", "initial_value": "5"},
                        {"name": "ENABLE", "qualified_name": "P.ENABLE", "type_name": "BOOL", "role": "input", "initial_value": null}
                    ],
                    "types": [
                        {"name": "INT", "kind": "integer", "semantic_bit_width": 16, "signed": true},
                        {"name": "BOOL", "kind": "boolean", "semantic_bit_width": 1, "signed": false}
                    ],
                    "expressions": [{"kind": {"kind": "literal", "value": {"kind": "integer", "value": "10"}}, "type_name": "INT"}],
                    "persistent_state": if stateful {
                        serde_json::json!({"cycle_inputs": ["P.X", "P.ENABLE"], "configuration_symbols": [], "inout_symbols": []})
                    } else {
                        serde_json::Value::Null
                    }
                }
            }]
        })
    }

    #[test]
    fn function_generation_is_bounded_and_uses_semantic_values() {
        let root = env::temp_dir().join(format!("stfuzzer-generation-{}", current_nanos()));
        let seeds = root.join("seeds");
        fs::create_dir_all(&seeds).unwrap();
        fs::write(root.join("model.json"), serde_json::to_vec(&test_model(false)).unwrap()).unwrap();
        fs::write(seeds.join("seed1"), b"X,INT,0\nENABLE,BOOL,0\n").unwrap();
        let generator = SemanticInitialGenerator::from_stg(
            &root.join("model.json"),
            "P",
            &seeds,
            &semantic::SemanticTaskPlan::default(),
        )
        .unwrap();
        let rendered = generator
            .inputs
            .iter()
            .map(|input| String::from_utf8_lossy(input.as_ref()).to_string())
            .collect::<Vec<_>>();
        assert!(!rendered.is_empty() && rendered.len() <= 16);
        assert!(rendered.iter().any(|seed| seed.contains("X,INT,5")));
        assert!(rendered.iter().any(|seed| seed.contains("X,INT,10")));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn function_block_generation_emits_bounded_cycle_patterns() {
        let root = env::temp_dir().join(format!("stfuzzer-generation-fb-{}", current_nanos()));
        let seeds = root.join("seeds");
        fs::create_dir_all(&seeds).unwrap();
        fs::write(root.join("model.json"), serde_json::to_vec(&test_model(true)).unwrap()).unwrap();
        fs::write(
            seeds.join("seed1"),
            b"0.X,INT,0\n0.ENABLE,BOOL,0\n1.X,INT,0\n1.ENABLE,BOOL,1\n",
        )
        .unwrap();
        let generator = SemanticInitialGenerator::from_stg(
            &root.join("model.json"),
            "P",
            &seeds,
            &semantic::SemanticTaskPlan::default(),
        )
        .unwrap();
        assert!(generator.stateful);
        assert!(!generator.inputs.is_empty() && generator.inputs.len() <= 24);
        assert!(generator.inputs.iter().any(|input| {
            let seed = String::from_utf8_lossy(input.as_ref());
            seed.contains("0.ENABLE,BOOL,0") && seed.contains("1.ENABLE,BOOL,1")
        }));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn safe_profile_changes_only_semantic_denominators() {
        let expressions = serde_json::json!([
            {
                "id": "expr:numerator",
                "kind": {"kind": "variable", "qualified_name": "P.sx"},
                "reads": ["P.sx"]
            },
            {
                "id": "expr:denominator",
                "kind": {"kind": "variable", "qualified_name": "P.C0"},
                "reads": ["P.C0"]
            },
            {
                "id": "expr:modulo",
                "kind": {"kind": "binary", "operator": "MOD"},
                "operands": ["expr:numerator", "expr:denominator"],
                "reads": ["P.sx", "P.C0"]
            }
        ]);
        let nonzero = seed_generation_nonzero_fields(expressions.as_array().unwrap());
        assert_eq!(nonzero, HashSet::from(["C0".to_string()]));

        let fields = [
            SeedGenerationField {
                name: "C0".to_string(),
                ty: "UINT".to_string(),
                cycle_scoped: false,
                kind: "integer".to_string(),
                initial: "0".to_string(),
                zero: "0".to_string(),
                one: "1".to_string(),
                negative: "0".to_string(),
                lower: "0".to_string(),
                upper: "65535".to_string(),
                constants: Vec::new(),
                must_be_nonzero: true,
            },
            SeedGenerationField {
                name: "O0".to_string(),
                ty: "UINT".to_string(),
                cycle_scoped: false,
                kind: "integer".to_string(),
                initial: "0".to_string(),
                zero: "0".to_string(),
                one: "1".to_string(),
                negative: "0".to_string(),
                lower: "0".to_string(),
                upper: "65535".to_string(),
                constants: Vec::new(),
                must_be_nonzero: false,
            },
        ];
        let seed = serialize_semantic_seed(
            &fields,
            true,
            2,
            SeedGenerationProfile::Safe,
            None,
            None,
        );
        assert_eq!(String::from_utf8(seed).unwrap(), "C0,UINT,1\nO0,UINT,0\n");
    }
}
