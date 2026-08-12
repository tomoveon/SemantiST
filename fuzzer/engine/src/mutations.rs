#[derive(Debug)]
struct StructuredStMutator {
    name: Cow<'static, str>,
}

const SEMANTIC_MUTATION_PROBABILITY: f64 = 0.70;
const REAL_BOUNDARY_VALUES: &[&str] = &[
    "0.0",
    "-0.0",
    "1.0",
    "-1.0",
    "1.17549435e-38",
    "-1.17549435e-38",
    "3.0e38",
    "-3.0e38",
    "3.4028235e38",
    "-3.4028235e38",
];
const LREAL_BOUNDARY_VALUES: &[&str] = &[
    "0.0",
    "-0.0",
    "1.0",
    "-1.0",
    "2.2250738585072014e-308",
    "-2.2250738585072014e-308",
    "1.0e308",
    "-1.0e308",
    "1.7976931348623157e308",
    "-1.7976931348623157e308",
];
const SECONDS_PER_DAY: i64 = 86_400;
const NANOS_PER_SECOND: i64 = 1_000_000_000;
const NANOS_PER_MILLISECOND: i64 = 1_000_000;
const DATE_BOUNDARY_DAYS: &[i64] = &[
    0, 1, 30, 31, 32, 58, 59, 60, 61, 90, 91, 92, 119, 120, 121, 151, 152, 153, 180, 181, 182,
    243, 244, 245, 272, 273, 274, 304, 305, 306, 333, 334, 335, 364, 365, 730, 789, 31_411,
    47_481,
];
const TOD_BOUNDARY_SECONDS: &[i64] = &[
    0, 1, 59, 60, 3_599, 3_600, 43_199, 43_200, 86_398, 86_399,
];
const TIME_BOUNDARY_VALUES: &[i64] = &[
    0,
    1,
    -1,
    1_000,
    -1_000,
    60_000,
    -60_000,
    3_600_000,
    -3_600_000,
    86_400_000,
    -86_400_000,
];
const TEMPORAL_BOOTSTRAP_VALUES: &[i64] = &[0, 1, -1, 31, 32, 59, 60, 91, 92, 365, 730];

impl StructuredStMutator {
    fn new() -> Self {
        Self {
            name: Cow::Borrowed("StructuredStMutator"),
        }
    }
}

impl Named for StructuredStMutator {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<S> Mutator<BytesInput, S> for StructuredStMutator
where
    S: HasMetadata + HasRand,
{
    fn mutate(&mut self, state: &mut S, input: &mut BytesInput) -> Result<MutationResult, Error> {
        if state
            .metadata_map()
            .get::<SemanticStageControl>()
            .is_some_and(|control| control.skip_mutation_once)
        {
            state.add_metadata(SemanticStageControl {
                skip_mutation_once: false,
            });
            return Ok(MutationResult::Skipped);
        }
        // LibAFL 的 input 仍然是 BytesInput；这里先把 bytes 当 UTF-8 文本解析成记录。
        let text = String::from_utf8_lossy(input.as_ref());
        let context = target_context(state);
        if has_cycle_record(&text) {
            let mut seed = parse_cycle_aware_seed(&text);
            if seed.cycles.is_empty() {
                seed.cycles.insert(
                    0,
                    vec![StRecord {
                        name: "IN".to_string(),
                        ty: "INT".to_string(),
                        value: "0".to_string(),
                    }],
                );
            }
            let use_semantic = context.target_id.is_some()
                && state
                    .rand_mut()
                    .coinflip(SEMANTIC_MUTATION_PROBABILITY);
            if !use_semantic
                || !semantic_mutate_cycle_seed(state.rand_mut(), &mut seed, &context)
            {
                mutate_cycle_aware_seed(state.rand_mut(), &mut seed);
            }
            *input = BytesInput::new(serialize_cycle_aware_seed(&seed).into_bytes());
            return Ok(MutationResult::Mutated);
        }

        let mut records = parse_structured_records(&text);
        if records.is_empty() {
            records.push(StRecord {
                name: "IN".to_string(),
                ty: "INT".to_string(),
                value: "0".to_string(),
            });
        }

        let use_semantic = context.target_id.is_some()
            && state
                .rand_mut()
                .coinflip(SEMANTIC_MUTATION_PROBABILITY);
        if !use_semantic || !semantic_mutate_records(state.rand_mut(), &mut records, &context) {
            mutate_records_fallback(state.rand_mut(), &mut records);
        }

        *input = BytesInput::new(serialize_records(&records).into_bytes());
        Ok(MutationResult::Mutated)
    }

    fn post_exec(&mut self, _state: &mut S, _new_corpus_id: Option<CorpusId>) -> Result<(), Error> {
        Ok(())
    }
}

// 一条结构化 fuzz 输入记录。例如：`POS,INT,0` 或 `STR,STRING,A`。
#[derive(Clone, Debug)]
struct StRecord {
    name: String,
    ty: String,
    value: String,
}

#[derive(Clone, Debug, Default)]
struct CycleAwareSeed {
    shared: Vec<StRecord>,
    cycles: BTreeMap<usize, Vec<StRecord>>,
}

// 从 corpus/queue 中读取的文本输入解析成记录列表。
fn parse_structured_records(text: &str) -> Vec<StRecord> {
    text.lines()
        .filter_map(|line| {
            let mut parts = line.splitn(3, ',');
            let name = parts.next()?.trim();
            let ty = parts.next()?.trim();
            let value = parts.next().unwrap_or("").trim();
            if name.is_empty() || ty.is_empty() {
                return None;
            }
            Some(StRecord {
                name: name.to_string(),
                ty: ty.to_ascii_uppercase(),
                value: value.to_string(),
            })
        })
        .collect()
}

fn serialize_records(records: &[StRecord]) -> String {
    let mut out = String::new();
    for record in records {
        push_record_line(&mut out, None, record);
    }
    out
}

fn bootstrap_seed_variants(seed_dir: &Path) -> Vec<BytesInput> {
    let mut bases = fs::read_dir(seed_dir)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|entry| fs::read(entry.path()).ok())
        .filter(|bytes| is_valid_structured_seed(bytes))
        .collect::<Vec<_>>();
    if bases.is_empty() {
        bases.push(b"IN,INT,0\n".to_vec());
    }
    let mut seen = HashSet::new();
    let mut variants = Vec::new();
    for bytes in bases {
        let text = String::from_utf8_lossy(&bytes);
        let records = parse_structured_records(&text);
        let has_temporal = records.iter().any(|record| is_temporal_type(&record.ty));
        let bootstrap_values = if has_temporal {
            TEMPORAL_BOOTSTRAP_VALUES
        } else {
            &[1_i64, -1, 2]
        };
        for integer in bootstrap_values {
            let mut candidate = records.clone();
            for record in &mut candidate {
                if record.ty == "BOOL" {
                    record.value = if *integer > 0 { "1" } else { "0" }.to_string();
                } else if is_date_type(&record.ty) || is_date_time_type(&record.ty) {
                    record.value = integer
                        .saturating_mul(SECONDS_PER_DAY)
                        .saturating_mul(NANOS_PER_SECOND)
                        .to_string();
                } else if is_time_of_day_type(&record.ty) {
                    record.value = integer
                        .rem_euclid(SECONDS_PER_DAY)
                        .saturating_mul(NANOS_PER_SECOND)
                        .to_string();
                } else if is_duration_type(&record.ty) {
                    record.value = integer.saturating_mul(NANOS_PER_MILLISECOND).to_string();
                } else if is_integer_type(&record.ty) {
                    set_integer_record_value(record, *integer);
                } else if record.ty == "STRING" || record.ty == "WSTRING" {
                    record.value = "A".to_string();
                } else if record.ty == "ARRAY" || record.ty == "POINTER" {
                    record.value = "0x01".to_string();
                }
            }
            let candidate = serialize_records(&candidate).into_bytes();
            if seen.insert(content_hash(&candidate)) {
                variants.push(BytesInput::new(candidate));
            }
        }
    }
    variants
}

fn mutate_records_fallback<R>(rand: &mut R, records: &mut Vec<StRecord>)
where
    R: Rand,
{
    if records.is_empty() {
        return;
    }
    // Preserve the harness schema, but stack several typed mutations like a
    // small structured-havoc stage. Values are generated by LibAFL's seeded
    // RNG, so runs remain reproducible.
    let mutations = 1 + rand.below_or_zero(records.len().min(4));
    for _ in 0..mutations {
        let idx = rand.below_or_zero(records.len());
        mutate_record(rand, &mut records[idx]);
    }
}

fn normalized_field_name(name: &str) -> String {
    split_cycle_record_name(name)
        .map(|(_cycle, field)| field)
        .unwrap_or_else(|| name.to_string())
        .to_ascii_uppercase()
}

fn expression_operator(expression: &serde_json::Value) -> Option<&str> {
    expression
        .get("kind")
        .and_then(|kind| kind.get("operator"))
        .and_then(|operator| operator.as_str())
}

fn expression_operands(expression: &serde_json::Value) -> &[serde_json::Value] {
    expression
        .get("operands")
        .and_then(|operands| operands.as_array())
        .map(Vec::as_slice)
        .unwrap_or_default()
}

fn expression_variable(expression: &serde_json::Value) -> Option<String> {
    let qualified = expression
        .get("kind")
        .and_then(|kind| kind.get("qualified_name"))
        .and_then(|value| value.as_str())
        .or_else(|| {
            expression
                .get("reads")
                .and_then(|reads| reads.as_array())
                .and_then(|reads| reads.first())
                .and_then(|value| value.as_str())
        })?;
    Some(
        qualified
            .rsplit(['.', ':'])
            .next()
            .unwrap_or(qualified)
            .to_ascii_uppercase(),
    )
}

fn expression_integer_literal(expression: &serde_json::Value) -> Option<i64> {
    let kind = expression.get("kind")?;
    let value = kind.get("value")?;
    let raw = value
        .get("value")
        .and_then(|value| value.as_str())
        .or_else(|| value.as_str())?;
    parse_integer_value(raw)
}

fn expression_record_index(records: &[StRecord], expression: &serde_json::Value) -> Option<usize> {
    let variable = expression_variable(expression)?;
    records
        .iter()
        .position(|record| normalized_field_name(&record.name) == variable)
}

fn expression_integer_value(records: &[StRecord], expression: &serde_json::Value) -> Option<i64> {
    expression_record_index(records, expression)
        .and_then(|index| parse_integer_value(&records[index].value))
        .or_else(|| expression_integer_literal(expression))
}

fn expression_number_literal(expression: &serde_json::Value) -> Option<f64> {
    let kind = expression.get("kind")?;
    let value = kind.get("value")?;
    let raw = value
        .get("value")
        .and_then(|value| value.as_str())
        .or_else(|| value.as_str())?;
    parse_number_value(raw)
}

fn expression_number_value(
    records: &[StRecord],
    expression: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> Option<f64> {
    if depth > 16 {
        return None;
    }
    if let Some(index) = expression_record_index(records, expression) {
        return parse_number_value(&records[index].value);
    }
    if let Some(value) = expression_number_literal(expression) {
        return Some(value);
    }
    if let Some(variable) = expression_variable(expression) {
        if let Some(definition) = definitions.get(&variable) {
            return expression_number_value(records, definition, definitions, depth + 1);
        }
    }
    let operands = expression_operands(expression);
    let Some(operator) = expression_operator(expression) else {
        return (operands.len() == 1)
            .then(|| expression_number_value(records, &operands[0], definitions, depth + 1))
            .flatten();
    };
    if operands.len() != 2 {
        return None;
    }
    let left = expression_number_value(records, &operands[0], definitions, depth + 1)?;
    let right = expression_number_value(records, &operands[1], definitions, depth + 1)?;
    match operator.to_ascii_uppercase().as_str() {
        "+" => Some(left + right),
        "-" => Some(left - right),
        "*" => Some(left * right),
        "/" | "DIV" => (right != 0.0).then_some(left / right),
        "MOD" => (right != 0.0).then_some(left % right),
        _ => None,
    }
}

fn evaluate_integer_expression(
    records: &[StRecord],
    expression: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> Option<i64> {
    if depth > 16 {
        return None;
    }
    if let Some(value) = expression_integer_value(records, expression) {
        return Some(value);
    }
    if let Some(variable) = expression_variable(expression) {
        if let Some(definition) = definitions.get(&variable) {
            return evaluate_integer_expression(records, definition, definitions, depth + 1);
        }
    }
    let operands = expression_operands(expression);
    let Some(operator) = expression_operator(expression) else {
        return (operands.len() == 1)
            .then(|| evaluate_integer_expression(records, &operands[0], definitions, depth + 1))
            .flatten();
    };
    if operands.len() != 2 {
        return None;
    }
    let left = evaluate_integer_expression(records, &operands[0], definitions, depth + 1)?;
    let right = evaluate_integer_expression(records, &operands[1], definitions, depth + 1)?;
    match operator.to_ascii_uppercase().as_str() {
        "+" => left.checked_add(right),
        "-" => left.checked_sub(right),
        "*" => left.checked_mul(right),
        "DIV" => (right != 0).then(|| left.checked_div(right)).flatten(),
        "MOD" => (right != 0).then(|| left.checked_rem(right)).flatten(),
        _ => None,
    }
}

fn mutate_expression_to_value(
    records: &mut [StRecord],
    expression: &serde_json::Value,
    desired: i64,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> bool {
    if depth > 16 {
        return false;
    }
    if let Some(index) = expression_record_index(records, expression) {
        if is_integer_type(&records[index].ty) {
            set_integer_record_value(&mut records[index], desired);
            return true;
        }
    }
    if let Some(variable) = expression_variable(expression) {
        if let Some(definition) = definitions.get(&variable) {
            return mutate_expression_to_value(
                records,
                definition,
                desired,
                definitions,
                depth + 1,
            );
        }
    }
    let operands = expression_operands(expression);
    let Some(operator) = expression_operator(expression) else {
        return operands.len() == 1
            && mutate_expression_to_value(
                records,
                &operands[0],
                desired,
                definitions,
                depth + 1,
            );
    };
    if operands.len() != 2 {
        return false;
    }
    let left = evaluate_integer_expression(records, &operands[0], definitions, depth + 1);
    let right = evaluate_integer_expression(records, &operands[1], definitions, depth + 1);
    match operator.to_ascii_uppercase().as_str() {
        "+" => {
            right.is_some_and(|fixed| {
                mutate_expression_to_value(
                    records,
                    &operands[0],
                    desired.saturating_sub(fixed),
                    definitions,
                    depth + 1,
                )
            }) || left.is_some_and(|fixed| {
                mutate_expression_to_value(
                    records,
                    &operands[1],
                    desired.saturating_sub(fixed),
                    definitions,
                    depth + 1,
                )
            })
        }
        "-" => {
            right.is_some_and(|fixed| {
                mutate_expression_to_value(
                    records,
                    &operands[0],
                    desired.saturating_add(fixed),
                    definitions,
                    depth + 1,
                )
            }) || left.is_some_and(|fixed| {
                mutate_expression_to_value(
                    records,
                    &operands[1],
                    fixed.saturating_sub(desired),
                    definitions,
                    depth + 1,
                )
            })
        }
        "*" => {
            right.is_some_and(|fixed| {
                fixed != 0
                    && desired.checked_rem(fixed) == Some(0)
                    && mutate_expression_to_value(
                        records,
                        &operands[0],
                        desired.checked_div(fixed).unwrap_or(desired),
                        definitions,
                        depth + 1,
                    )
            }) || left.is_some_and(|fixed| {
                fixed != 0
                    && desired.checked_rem(fixed) == Some(0)
                    && mutate_expression_to_value(
                        records,
                        &operands[1],
                        desired.checked_div(fixed).unwrap_or(desired),
                        definitions,
                        depth + 1,
                    )
            })
        }
        _ => false,
    }
}

fn comparison_target_number(
    operator: &str,
    fixed: f64,
    variable_on_left: bool,
    desired: bool,
) -> f64 {
    let step = if fixed.abs() >= 1.0 {
        fixed.abs() * 0.01
    } else {
        1.0
    };
    match (operator, variable_on_left, desired) {
        ("<", true, true) | (">", false, true) => fixed - step,
        ("<", true, false) | (">", false, false) => fixed,
        ("<=", true, true) | (">=", false, true) => fixed,
        ("<=", true, false) | (">=", false, false) => fixed + step,
        (">", true, true) | ("<", false, true) => fixed + step,
        (">", true, false) | ("<", false, false) => fixed,
        (">=", true, true) | ("<=", false, true) => fixed,
        (">=", true, false) | ("<=", false, false) => fixed - step,
        ("=", _, true) | ("<>", _, false) => fixed,
        ("=", _, false) | ("<>", _, true) => fixed + step,
        _ => fixed,
    }
}

fn set_number_record_value(record: &mut StRecord, value: f64) {
    if is_float_type(&record.ty) {
        let value = if value.is_finite() { value } else { 0.0 };
        record.value = if value != 0.0 && !(1.0e-4..1.0e9).contains(&value.abs()) {
            format!("{value:.9e}")
        } else {
            format!("{value:.6}")
        };
    } else if is_integer_type(&record.ty) {
        set_integer_record_value(record, value.round() as i64);
    }
}

#[derive(Clone, Copy, Debug)]
struct RangeConstraint {
    value_index: usize,
    bound_index: Option<usize>,
    bound_value: f64,
    is_lower: bool,
    inclusive: bool,
}

fn extract_range_constraint(
    records: &[StRecord],
    expression: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> Option<RangeConstraint> {
    let operator = expression_operator(expression)?;
    let operands = expression_operands(expression);
    if !matches!(operator, "<" | "<=" | ">" | ">=") || operands.len() != 2 {
        return None;
    }
    let left_index = expression_record_index(records, &operands[0]);
    let right_index = expression_record_index(records, &operands[1]);
    let left_value = expression_number_value(records, &operands[0], definitions, depth + 1);
    let right_value = expression_number_value(records, &operands[1], definitions, depth + 1);
    match (left_index, right_index, operator) {
        (Some(value_index), bound_index, "<" | "<=") => Some(RangeConstraint {
            value_index,
            bound_index,
            bound_value: right_value?,
            is_lower: false,
            inclusive: operator == "<=",
        }),
        (Some(value_index), bound_index, ">" | ">=") => Some(RangeConstraint {
            value_index,
            bound_index,
            bound_value: right_value?,
            is_lower: true,
            inclusive: operator == ">=",
        }),
        (bound_index, Some(value_index), "<" | "<=") => Some(RangeConstraint {
            value_index,
            bound_index,
            bound_value: left_value?,
            is_lower: true,
            inclusive: operator == "<=",
        }),
        (bound_index, Some(value_index), ">" | ">=") => Some(RangeConstraint {
            value_index,
            bound_index,
            bound_value: left_value?,
            is_lower: false,
            inclusive: operator == ">=",
        }),
        _ => None,
    }
}

fn mutate_range_conjunction(
    records: &mut [StRecord],
    left: &serde_json::Value,
    right: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> bool {
    let Some(first) = extract_range_constraint(records, left, definitions, depth) else {
        return false;
    };
    let Some(second) = extract_range_constraint(records, right, definitions, depth) else {
        return false;
    };
    if first.value_index != second.value_index || first.is_lower == second.is_lower {
        return false;
    }
    let (lower, upper) = if first.is_lower {
        (first, second)
    } else {
        (second, first)
    };
    let integer = is_integer_type(&records[lower.value_index].ty);
    let mut low = lower.bound_value;
    let mut high = upper.bound_value;
    let needs_room = if integer {
        if lower.inclusive && upper.inclusive {
            low > high
        } else if lower.inclusive || upper.inclusive {
            low >= high
        } else {
            low + 1.0 >= high
        }
    } else {
        low >= high
    };
    if needs_room {
        low = 0.0;
        high = if integer { 10.0 } else { 1.0 };
        if let Some(index) = lower.bound_index {
            set_number_record_value(&mut records[index], low);
        }
        if let Some(index) = upper.bound_index {
            set_number_record_value(&mut records[index], high);
        }
    }
    let value = if integer {
        if lower.inclusive {
            low.ceil()
        } else {
            low.floor() + 1.0
        }
    } else {
        (low + high) / 2.0
    };
    set_number_record_value(&mut records[lower.value_index], value);
    true
}

fn mutate_expression_goal(
    records: &mut [StRecord],
    expression: &serde_json::Value,
    desired: bool,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> bool {
    if depth > 16 {
        return false;
    }
    if let Some(variable) = expression_variable(expression) {
        if expression_record_index(records, expression).is_none() {
            if let Some(definition) = definitions.get(&variable) {
                return mutate_expression_goal(records, definition, desired, definitions, depth + 1);
            }
        }
    }
    let operands = expression_operands(expression);
    let Some(operator) = expression_operator(expression) else {
        if operands.len() == 1 {
            return mutate_expression_goal(records, &operands[0], desired, definitions, depth + 1);
        }
        return false;
    };
    if operator.eq_ignore_ascii_case("NOT") && operands.len() == 1 {
        return mutate_expression_goal(records, &operands[0], !desired, definitions, depth + 1);
    }
    if operator.eq_ignore_ascii_case("AND") && operands.len() == 2 {
        if desired {
            if mutate_range_conjunction(records, &operands[0], &operands[1], definitions, depth + 1)
            {
                return true;
            }
            let left = mutate_expression_goal(records, &operands[0], true, definitions, depth + 1);
            let right =
                mutate_expression_goal(records, &operands[1], true, definitions, depth + 1);
            return left || right;
        }
        return mutate_expression_goal(records, &operands[0], false, definitions, depth + 1)
            || mutate_expression_goal(records, &operands[1], false, definitions, depth + 1);
    }
    if operator.eq_ignore_ascii_case("OR") && operands.len() == 2 {
        if desired {
            return mutate_expression_goal(records, &operands[0], true, definitions, depth + 1)
                || mutate_expression_goal(records, &operands[1], true, definitions, depth + 1);
        }
        let left = mutate_expression_goal(records, &operands[0], false, definitions, depth + 1);
        let right = mutate_expression_goal(records, &operands[1], false, definitions, depth + 1);
        return left || right;
    }
    if !matches!(operator, "<" | "<=" | ">" | ">=" | "=" | "<>") || operands.len() != 2 {
        return false;
    }
    if let Some(left_index) = expression_record_index(records, &operands[0]) {
        if !is_integer_type(&records[left_index].ty) && !is_float_type(&records[left_index].ty) {
            return false;
        }
        if let Some(right) = expression_number_value(records, &operands[1], definitions, depth + 1)
        {
            set_number_record_value(
                &mut records[left_index],
                comparison_target_number(operator, right, true, desired),
            );
            return true;
        }
    }
    if let Some(right_index) = expression_record_index(records, &operands[1]) {
        if !is_integer_type(&records[right_index].ty) && !is_float_type(&records[right_index].ty) {
            return false;
        }
        if let Some(left) = expression_number_value(records, &operands[0], definitions, depth + 1) {
            set_number_record_value(
                &mut records[right_index],
                comparison_target_number(operator, left, false, desired),
            );
            return true;
        }
    }
    false
}

fn semantic_type_bounds(field: &ControllableSeedField) -> Option<(i64, i64)> {
    let fact = field.type_fact.as_ref()?;
    let width = fact
        .get("semantic_bit_width")
        .or_else(|| fact.get("bit_width"))
        .and_then(|value| value.as_u64())?;
    if width == 0 || width > 63 {
        return None;
    }
    let signed = fact
        .get("signed")
        .and_then(|value| value.as_bool())
        .unwrap_or(false);
    if signed {
        let magnitude = 1_i64.checked_shl(width as u32 - 1)?;
        Some((-magnitude, magnitude - 1))
    } else {
        if width >= 63 {
            return Some((0, i64::MAX));
        }
        Some((0, (1_i64.checked_shl(width as u32)?).saturating_sub(1)))
    }
}

fn semantic_float_type_name(
    record: &StRecord,
    field: Option<&ControllableSeedField>,
) -> Option<&'static str> {
    if record.ty == "LREAL" {
        return Some("LREAL");
    }
    if record.ty == "REAL" {
        return Some("REAL");
    }
    let field = field?;
    if field
        .type_name
        .as_deref()
        .is_some_and(|name| name.eq_ignore_ascii_case("LREAL"))
        || field
            .type_fact
            .as_ref()
            .and_then(|fact| {
                fact.get("semantic_bit_width")
                    .or_else(|| fact.get("bit_width"))
                    .and_then(|value| value.as_u64())
            })
            == Some(64)
    {
        return Some("LREAL");
    }
    let type_kind = field
        .type_fact
        .as_ref()
        .and_then(|fact| fact.get("kind"))
        .and_then(|kind| kind.as_str())
        .unwrap_or_default();
    if field
        .type_name
        .as_deref()
        .is_some_and(|name| name.eq_ignore_ascii_case("REAL"))
        || type_kind == "float"
    {
        return Some("REAL");
    }
    None
}

fn float_boundary_values(type_name: &str) -> &'static [&'static str] {
    if type_name.eq_ignore_ascii_case("LREAL") {
        LREAL_BOUNDARY_VALUES
    } else {
        REAL_BOUNDARY_VALUES
    }
}

fn set_float_boundary_record<R>(
    rand: &mut R,
    record: &mut StRecord,
    field: Option<&ControllableSeedField>,
) -> bool
where
    R: Rand,
{
    let Some(type_name) = semantic_float_type_name(record, field) else {
        return false;
    };
    let values = float_boundary_values(type_name);
    record.value = values[rand.below_or_zero(values.len())].to_string();
    true
}

fn is_date_type(ty: &str) -> bool {
    ty.eq_ignore_ascii_case("DATE") || ty.eq_ignore_ascii_case("LDATE")
}

fn is_date_time_type(ty: &str) -> bool {
    ty.eq_ignore_ascii_case("DT")
        || ty.eq_ignore_ascii_case("DATE_AND_TIME")
        || ty.eq_ignore_ascii_case("LDT")
        || ty.eq_ignore_ascii_case("LDATE_AND_TIME")
}

fn is_time_of_day_type(ty: &str) -> bool {
    ty.eq_ignore_ascii_case("TOD")
        || ty.eq_ignore_ascii_case("TIME_OF_DAY")
        || ty.eq_ignore_ascii_case("LTOD")
        || ty.eq_ignore_ascii_case("LTIME_OF_DAY")
}

fn is_duration_type(ty: &str) -> bool {
    ty.eq_ignore_ascii_case("TIME") || ty.eq_ignore_ascii_case("LTIME")
}

fn is_temporal_type(ty: &str) -> bool {
    is_date_type(ty) || is_date_time_type(ty) || is_time_of_day_type(ty) || is_duration_type(ty)
}

fn temporal_boundary_value<R>(rand: &mut R, ty: &str) -> i64
where
    R: Rand,
{
    if is_time_of_day_type(ty) {
        return TOD_BOUNDARY_SECONDS[rand.below_or_zero(TOD_BOUNDARY_SECONDS.len())]
            .saturating_mul(NANOS_PER_SECOND);
    }
    if is_duration_type(ty) {
        return TIME_BOUNDARY_VALUES[rand.below_or_zero(TIME_BOUNDARY_VALUES.len())]
            .saturating_mul(NANOS_PER_MILLISECOND);
    }
    let day = DATE_BOUNDARY_DAYS[rand.below_or_zero(DATE_BOUNDARY_DAYS.len())];
    let seconds = if is_date_time_type(ty) {
        TOD_BOUNDARY_SECONDS[rand.below_or_zero(TOD_BOUNDARY_SECONDS.len())]
    } else {
        0
    };
    day.saturating_mul(SECONDS_PER_DAY)
        .saturating_add(seconds)
        .saturating_mul(NANOS_PER_SECOND)
}

fn set_temporal_record_value<R>(rand: &mut R, record: &mut StRecord) -> bool
where
    R: Rand,
{
    if !is_temporal_type(&record.ty) {
        return false;
    }
    if rand.coinflip(0.75) {
        record.value = temporal_boundary_value(rand, &record.ty).to_string();
    } else {
        let day = rand.between(0, 47_481) as i64;
        let seconds = if is_date_type(&record.ty) {
            0
        } else {
            rand.between(0, 86_399) as i64
        };
        let value = if is_duration_type(&record.ty) || is_time_of_day_type(&record.ty) {
            seconds.saturating_mul(NANOS_PER_SECOND)
        } else {
            day.saturating_mul(SECONDS_PER_DAY)
                .saturating_add(seconds)
                .saturating_mul(NANOS_PER_SECOND)
        };
        record.value = value.to_string();
    }
    true
}

fn float_stress_profile(type_name: &str, positive: bool) -> (&'static str, &'static str, &'static str) {
    match (type_name.eq_ignore_ascii_case("LREAL"), positive) {
        (true, true) => ("-1.0e308", "1.0e308", "1.7976931348623157e308"),
        (true, false) => ("-1.7976931348623157e308", "-1.0e308", "1.0e308"),
        (false, true) => ("-3.0e38", "3.0e38", "3.4028235e38"),
        (false, false) => ("-3.4028235e38", "-3.0e38", "3.0e38"),
    }
}

fn looks_like_lower_bound(name: &str) -> bool {
    let name = normalized_field_name(name);
    name.contains("MIN") || name.contains("LOW") || name.contains("LOWER")
}

fn looks_like_upper_bound(name: &str) -> bool {
    let name = normalized_field_name(name);
    name.contains("MAX") || name.contains("HIGH") || name.contains("UPPER")
}

fn looks_like_default_value(name: &str) -> bool {
    normalized_field_name(name).contains("DEFAULT")
}

fn mutate_float_boundary_group<R>(
    rand: &mut R,
    records: &mut [StRecord],
    fields: &[ControllableSeedField],
) -> bool
where
    R: Rand,
{
    let mut float_indices = fields
        .iter()
        .filter_map(|field| {
            let name = field.seed_field.as_deref()?.to_ascii_uppercase();
            let index = records
                .iter()
                .position(|record| normalized_field_name(&record.name) == name)?;
            semantic_float_type_name(&records[index], Some(field)).map(|type_name| (index, type_name))
        })
        .collect::<Vec<_>>();
    if float_indices.is_empty() {
        float_indices = records
            .iter()
            .enumerate()
            .filter_map(|(index, record)| semantic_float_type_name(record, None).map(|type_name| (index, type_name)))
            .collect();
    }
    let Some((_, type_name)) = float_indices.first().copied() else {
        return false;
    };
    let (lower, stress, upper) = float_stress_profile(type_name, rand.coinflip(0.5));
    let mut lower_index = None;
    let mut upper_index = None;
    let mut value_indices = Vec::new();
    for (index, _) in float_indices {
        if looks_like_lower_bound(&records[index].name) {
            lower_index = Some(index);
        } else if looks_like_upper_bound(&records[index].name) {
            upper_index = Some(index);
        } else if !looks_like_default_value(&records[index].name) {
            value_indices.push(index);
        }
    }
    if value_indices.is_empty() {
        return false;
    }
    if let Some(index) = lower_index {
        records[index].value = lower.to_string();
    }
    if let Some(index) = upper_index {
        records[index].value = upper.to_string();
    }
    for index in value_indices {
        records[index].value = stress.to_string();
    }
    true
}

fn mutate_controllable_seed_field<R>(
    rand: &mut R,
    records: &mut [StRecord],
    fields: &[ControllableSeedField],
) -> bool
where
    R: Rand,
{
    let candidates = fields
        .iter()
        .filter_map(|field| {
            let name = field.seed_field.as_deref()?.to_ascii_uppercase();
            records
                .iter()
                .position(|record| normalized_field_name(&record.name) == name)
                .map(|index| (field, index))
        })
        .collect::<Vec<_>>();
    let Some((field, index)) = candidates.get(rand.below_or_zero(candidates.len())) else {
        return false;
    };
    let record = &mut records[*index];
    let type_kind = field
        .type_fact
        .as_ref()
        .and_then(|fact| fact.get("kind"))
        .and_then(|kind| kind.as_str())
        .unwrap_or_default();
    if record.ty == "BOOL" || type_kind == "boolean" {
        record.value = if record.value == "0" { "1" } else { "0" }.to_string();
        return true;
    }
    if set_temporal_record_value(rand, record) {
        return true;
    }
    if is_integer_type(&record.ty) || matches!(type_kind, "integer" | "enum" | "subrange") {
        let (minimum, maximum) =
            semantic_type_bounds(field).unwrap_or((i32::MIN as i64, i32::MAX as i64));
        let values = [
            minimum,
            minimum.saturating_add(1),
            0,
            1,
            maximum.saturating_sub(1),
            maximum,
        ];
        set_integer_record_value(record, values[rand.below_or_zero(values.len())]);
        return true;
    }
    if semantic_float_type_name(record, Some(field)).is_some() {
        return set_float_boundary_record(rand, record, Some(field));
    }
    if record.ty == "STRING" || record.ty == "WSTRING" || type_kind == "string" {
        let capacity = field
            .type_fact
            .as_ref()
            .and_then(|fact| fact.get("string_capacity"))
            .and_then(|value| value.as_i64())
            .unwrap_or(16)
            .clamp(0, 4096) as usize;
        let lengths = [0, 1, capacity, capacity.saturating_add(1)];
        record.value = "A".repeat(lengths[rand.below_or_zero(lengths.len())]);
        return true;
    }
    if record.ty == "ARRAY" || type_kind == "array" {
        let choices = ["0x00", "0xff", "0x00000000", "0xffffffffffffffff"];
        record.value = choices[rand.below_or_zero(choices.len())].to_string();
        return true;
    }
    if record.ty == "POINTER" || type_kind == "pointer" {
        let choices = ["0x", "0x00", "0x01", "0xffffffffffffffff"];
        record.value = choices[rand.below_or_zero(choices.len())].to_string();
        return true;
    }
    false
}

fn mutate_hazard_goal<R>(
    rand: &mut R,
    records: &mut [StRecord],
    context: &TargetContext,
) -> bool
where
    R: Rand,
{
    let Some(expression) = context.goal_expression.as_ref() else {
        return false;
    };
    let violation = context.target_type.as_deref() == Some("hazard_violation");
    let operands = expression_operands(expression);
    match context.hazard_kind.as_deref() {
        Some("division" | "modulo") if operands.len() == 2 => {
            mutate_expression_to_value(
                records,
                &operands[1],
                if violation { 0 } else { 1 },
                &context.value_definitions,
                0,
            )
        }
        Some("arithmetic_boundary" | "dangerous_conversion") => {
            if violation
                && context.hazard_kind.as_deref() == Some("arithmetic_boundary")
                && mutate_float_boundary_group(rand, records, &context.controllable_seed_fields)
            {
                return true;
            }
            let candidates = context
                .controllable_seed_fields
                .iter()
                .filter_map(|field| {
                    let name = field.seed_field.as_deref()?;
                    records
                        .iter()
                        .position(|record| {
                            normalized_field_name(&record.name) == name.to_ascii_uppercase()
                        })
                        .map(|index| (field, index))
                })
                .collect::<Vec<_>>();
            let Some((field, index)) = candidates.get(rand.below_or_zero(candidates.len())) else {
                return false;
            };
            if set_float_boundary_record(rand, &mut records[*index], Some(field)) {
                return true;
            }
            let (minimum, maximum) =
                semantic_type_bounds(field).unwrap_or((i32::MIN as i64, i32::MAX as i64));
            let boundary_values = if violation {
                [minimum, minimum.saturating_add(1), maximum.saturating_sub(1), maximum]
            } else {
                [0, 1, minimum.saturating_add(1), maximum.saturating_sub(1)]
            };
            set_integer_record_value(
                &mut records[*index],
                boundary_values[rand.below_or_zero(boundary_values.len())],
            );
            true
        }
        Some("array_access") => {
            let index_expression = operands.last().unwrap_or(expression);
            let Some(index) = expression_record_index(records, index_expression) else {
                return false;
            };
            set_integer_record_value(&mut records[index], if violation { -1 } else { 0 });
            true
        }
        Some("pointer_access") => {
            let Some(index) = records.iter().position(|record| record.ty == "POINTER") else {
                return false;
            };
            records[index].value = if violation { "0x" } else { "0x00" }.to_string();
            true
        }
        _ => false,
    }
}

fn semantic_mutate_records<R>(
    rand: &mut R,
    records: &mut Vec<StRecord>,
    context: &TargetContext,
) -> bool
where
    R: Rand,
{
    if context.target_id.is_none() || records.is_empty() {
        return false;
    }
    if mutate_hazard_goal(rand, records, context) {
        return true;
    }
    if let Some(expression) = context.goal_expression.as_ref() {
        if mutate_expression_goal(records, expression, true, &context.value_definitions, 0) {
            return true;
        }
    }
    context.semantic_task_id.is_some()
        && mutate_controllable_seed_field(rand, records, &context.controllable_seed_fields)
}

fn is_integer_type(ty: &str) -> bool {
    matches!(
        ty,
        "BOOL"
            | "SINT"
            | "INT"
            | "DINT"
            | "LINT"
            | "USINT"
            | "UINT"
            | "UDINT"
            | "ULINT"
            | "BYTE"
            | "WORD"
            | "DWORD"
            | "LWORD"
            | "CHAR"
            | "WCHAR"
            | "DATE"
            | "LDATE"
            | "DT"
            | "DATE_AND_TIME"
            | "LDT"
            | "LDATE_AND_TIME"
            | "TOD"
            | "TIME_OF_DAY"
            | "LTOD"
            | "LTIME_OF_DAY"
            | "TIME"
            | "LTIME"
    )
}

fn is_float_type(ty: &str) -> bool {
    matches!(ty, "REAL" | "LREAL")
}

fn set_integer_record_value(record: &mut StRecord, value: i64) {
    let value = if is_unsigned_type(&record.ty) && value < 0 {
        0
    } else {
        value
    };
    record.value = value.to_string();
}

fn is_unsigned_type(ty: &str) -> bool {
    matches!(
        ty,
        "USINT" | "UINT" | "UDINT" | "ULINT" | "BYTE" | "WORD" | "DWORD" | "LWORD" | "CHAR"
    )
}

fn split_cycle_record_name(name: &str) -> Option<(usize, String)> {
    let (prefix, rest) = name.split_once('.')?;
    if prefix.is_empty() || rest.is_empty() || !prefix.chars().all(|ch| ch.is_ascii_digit()) {
        return None;
    }
    Some((prefix.parse().ok()?, rest.to_string()))
}

fn has_cycle_record(text: &str) -> bool {
    parse_structured_records(text)
        .iter()
        .any(|record| split_cycle_record_name(&record.name).is_some())
}

fn parse_cycle_aware_seed(text: &str) -> CycleAwareSeed {
    let mut seed = CycleAwareSeed::default();
    for mut record in parse_structured_records(text) {
        if let Some((cycle, field_name)) = split_cycle_record_name(&record.name) {
            record.name = field_name;
            seed.cycles.entry(cycle).or_default().push(record);
        } else {
            seed.shared.push(record);
        }
    }
    seed
}

fn infer_seed_cycle_metadata(bytes: &[u8]) -> SeedCycleMetadata {
    let text = String::from_utf8_lossy(bytes);
    let mut cycle_ids = parse_structured_records(&text)
        .iter()
        .filter_map(|record| split_cycle_record_name(&record.name).map(|(cycle, _name)| cycle))
        .collect::<Vec<_>>();
    cycle_ids.sort_unstable();
    cycle_ids.dedup();
    let target_kind = if cycle_ids.is_empty() {
        "FUNCTION"
    } else {
        "FUNCTION_BLOCK"
    }
    .to_string();
    SeedCycleMetadata {
        target_kind,
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
        let mut changed = None;
        let mut stale = None;
        for item in line.split_whitespace() {
            let Some((key, value)) = item.split_once('=') else {
                continue;
            };
            match key {
                "cycle" => cycle = value.parse::<i32>().ok(),
                "state_hash_before" => before = Some(value.to_string()),
                "state_hash_after" => after = Some(value.to_string()),
                "changed" => changed = Some(value != "0"),
                "stale" => stale = Some(value != "0"),
                _ => {}
            }
        }
        let Some(cycle) = cycle else {
            continue;
        };
        let changed = changed.unwrap_or(false);
        let stale = stale.unwrap_or(false);
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

fn serialize_cycle_aware_seed(seed: &CycleAwareSeed) -> String {
    let mut out = String::new();
    for record in &seed.shared {
        push_record_line(&mut out, None, record);
    }
    for (cycle, records) in &seed.cycles {
        for record in records {
            push_record_line(&mut out, Some(*cycle), record);
        }
    }
    out
}

fn push_record_line(out: &mut String, cycle: Option<usize>, record: &StRecord) {
    if let Some(cycle) = cycle {
        out.push_str(&cycle.to_string());
        out.push('.');
    }
    out.push_str(&record.name);
    out.push(',');
    out.push_str(&record.ty);
    out.push(',');
    out.push_str(&record.value);
    out.push('\n');
}

fn normalize_cycle_ids(seed: &mut CycleAwareSeed) {
    let old_cycles = std::mem::take(&mut seed.cycles);
    seed.cycles = old_cycles
        .into_values()
        .enumerate()
        .collect::<BTreeMap<usize, Vec<StRecord>>>();
}

fn semantic_mutate_cycle_seed<R>(
    rand: &mut R,
    seed: &mut CycleAwareSeed,
    context: &TargetContext,
) -> bool
where
    R: Rand,
{
    if context.target_id.is_none() {
        return false;
    }
    ensure_cycle_hint(seed, context.cycle_hint.unwrap_or(3).max(1) as usize);
    if let Some((_cycle, records)) = seed.cycles.iter_mut().next_back() {
        if mutate_hazard_goal(rand, records, context)
            || context
                .goal_expression
                .as_ref()
                .is_some_and(|expression| {
                    mutate_expression_goal(records, expression, true, &context.value_definitions, 0)
                })
        {
            normalize_cycle_ids(seed);
            return true;
        }
    }
    if context.semantic_task_id.is_none() {
        return false;
    }
    let mut cycles = seed.cycles.values().cloned().collect::<Vec<_>>();
    if cycles.is_empty() {
        return false;
    }
    let minimum_cycles = context.cycle_hint.unwrap_or(1).max(1) as usize;
    let operation = rand.below_or_zero(4);
    let selected = rand.below_or_zero(cycles.len());
    let changed = match operation {
        0 => mutate_controllable_seed_field(
            rand,
            &mut cycles[selected],
            &context.controllable_seed_fields,
        ),
        1 if cycles.len() < 64 => {
            let duplicate = cycles[selected].clone();
            cycles.insert(selected.saturating_add(1), duplicate);
            true
        }
        2 if cycles.len() < 64 => {
            cycles.push(cycles[selected].clone());
            true
        }
        3 if cycles.len() > minimum_cycles => {
            cycles.remove(selected);
            true
        }
        _ => mutate_controllable_seed_field(
            rand,
            &mut cycles[selected],
            &context.controllable_seed_fields,
        ),
    };
    if changed {
        seed.cycles = cycles.into_iter().enumerate().collect();
    }
    changed
}

fn ensure_cycle_hint(seed: &mut CycleAwareSeed, desired_cycles: usize) {
    let desired_cycles = desired_cycles.max(1).min(64);
    if seed.cycles.is_empty() {
        let base = if seed.shared.is_empty() {
            vec![StRecord {
                name: "IN".to_string(),
                ty: "INT".to_string(),
                value: "0".to_string(),
            }]
        } else {
            seed.shared.clone()
        };
        seed.cycles.insert(0, base);
    }
    while seed.cycles.len() < desired_cycles {
        let source = seed
            .cycles
            .values()
            .next_back()
            .cloned()
            .unwrap_or_default();
        let next_id = seed.cycles.keys().next_back().copied().unwrap_or(0) + 1;
        seed.cycles.insert(next_id, source);
    }
}

fn mutate_cycle_aware_seed<R>(rand: &mut R, seed: &mut CycleAwareSeed)
where
    R: Rand,
{
    match rand.below_or_zero(8) {
        0 => cycle_mutate_value(rand, seed),
        1 => cycle_clone_cycle(rand, seed),
        2 => cycle_delete_cycle(rand, seed),
        3 => cycle_insert_cycle(rand, seed),
        4 => cycle_reset_like_cycle(rand, seed),
        5 => cycle_enable_toggle(seed),
        6 => cycle_read_write_pattern(seed),
        _ => normalize_cycle_ids(seed),
    }
    normalize_cycle_ids(seed);
}

fn cycle_mutate_value<R>(rand: &mut R, seed: &mut CycleAwareSeed)
where
    R: Rand,
{
    let shared_count = seed.shared.len();
    let cycle_count: usize = seed.cycles.values().map(Vec::len).sum();
    let total = shared_count + cycle_count;
    if total == 0 {
        return;
    }
    let mut idx = rand.below_or_zero(total);
    if idx < shared_count {
        mutate_record(rand, &mut seed.shared[idx]);
        return;
    }
    idx -= shared_count;
    for records in seed.cycles.values_mut() {
        if idx < records.len() {
            mutate_record(rand, &mut records[idx]);
            return;
        }
        idx -= records.len();
    }
}

fn cycle_clone_cycle<R>(rand: &mut R, seed: &mut CycleAwareSeed)
where
    R: Rand,
{
    let ids = seed.cycles.keys().copied().collect::<Vec<_>>();
    if ids.is_empty() {
        return;
    }
    let source_id = ids[rand.below_or_zero(ids.len())];
    let mut cloned = seed.cycles.get(&source_id).cloned().unwrap_or_default();
    if !cloned.is_empty() {
        let idx = rand.below_or_zero(cloned.len());
        mutate_record(rand, &mut cloned[idx]);
    }
    let next_id = seed.cycles.keys().next_back().copied().unwrap_or(0) + 1;
    seed.cycles.insert(next_id, cloned);
}

fn cycle_delete_cycle<R>(rand: &mut R, seed: &mut CycleAwareSeed)
where
    R: Rand,
{
    if seed.cycles.len() <= 1 {
        return;
    }
    let ids = seed.cycles.keys().copied().collect::<Vec<_>>();
    let remove_id = ids[rand.below_or_zero(ids.len())];
    seed.cycles.remove(&remove_id);
}

fn cycle_insert_cycle<R>(rand: &mut R, seed: &mut CycleAwareSeed)
where
    R: Rand,
{
    let ids = seed.cycles.keys().copied().collect::<Vec<_>>();
    let mut records = if ids.is_empty() {
        seed.shared.clone()
    } else {
        let source_id = ids[rand.below_or_zero(ids.len())];
        seed.cycles.get(&source_id).cloned().unwrap_or_default()
    };
    if records.is_empty() {
        records.push(StRecord {
            name: "IN".to_string(),
            ty: "INT".to_string(),
            value: "0".to_string(),
        });
    }
    for record in &mut records {
        if is_reset_like(&record.name) && record.ty == "BOOL" {
            record.value = "0".to_string();
        }
    }
    let next_id = seed.cycles.keys().next_back().copied().unwrap_or(0) + 1;
    seed.cycles.insert(next_id, records);
}

fn cycle_reset_like_cycle<R>(rand: &mut R, seed: &mut CycleAwareSeed)
where
    R: Rand,
{
    let ids = seed.cycles.keys().copied().collect::<Vec<_>>();
    if ids.is_empty() {
        return;
    }
    let target_id = ids[rand.below_or_zero(ids.len())];
    if let Some(records) = seed.cycles.get_mut(&target_id) {
        for record in records {
            if record.ty == "BOOL" {
                if is_reset_like(&record.name) {
                    record.value = "1".to_string();
                } else if is_enable_like(&record.name)
                    || is_read_like(&record.name)
                    || is_write_like(&record.name)
                {
                    record.value = "0".to_string();
                }
            }
        }
    }
}

fn cycle_enable_toggle(seed: &mut CycleAwareSeed) {
    let mut enabled = false;
    for records in seed.cycles.values_mut() {
        enabled = !enabled;
        for record in records {
            if record.ty == "BOOL" && is_enable_like(&record.name) {
                record.value = if enabled { "1" } else { "0" }.to_string();
            }
        }
    }
}

fn cycle_read_write_pattern(seed: &mut CycleAwareSeed) {
    for (idx, records) in seed.cycles.values_mut().enumerate() {
        for record in records {
            if record.ty != "BOOL" {
                continue;
            }
            if is_write_like(&record.name) {
                record.value = if idx % 4 == 1 || idx % 4 == 2 {
                    "1"
                } else {
                    "0"
                }
                .to_string();
            } else if is_read_like(&record.name) {
                record.value = if idx % 4 == 3 { "1" } else { "0" }.to_string();
            } else if is_reset_like(&record.name) {
                record.value = if idx == 0 { "1" } else { "0" }.to_string();
            } else if is_enable_like(&record.name) {
                record.value = "1".to_string();
            }
        }
    }
}

fn is_reset_like(name: &str) -> bool {
    let upper = name.to_ascii_uppercase();
    matches!(upper.as_str(), "RST" | "RESET" | "INIT" | "CLR" | "CLEAR") || upper.contains("RESET")
}

fn is_enable_like(name: &str) -> bool {
    let upper = name.to_ascii_uppercase();
    matches!(upper.as_str(), "E" | "EN" | "ENA" | "ENABLE" | "RUN")
}

fn is_read_like(name: &str) -> bool {
    let upper = name.to_ascii_uppercase();
    matches!(upper.as_str(), "RD" | "READ" | "R")
}

fn is_write_like(name: &str) -> bool {
    let upper = name.to_ascii_uppercase();
    matches!(upper.as_str(), "WD" | "WR" | "WRITE" | "W")
}

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

#[derive(Debug)]
struct RuntimeFindingFeedback {
    name: Cow<'static, str>,
    findings_dir: PathBuf,
    target_corpus_dir: PathBuf,
    bootstrap_path: Option<PathBuf>,
    events_file: PathBuf,
    sanitizer_log_dir: PathBuf,
    seen: HashSet<String>,
    seen_sanitizer_logs: HashSet<String>,
}

#[derive(Clone, Debug)]
struct SanitizerEvidence {
    kind: String,
    path: PathBuf,
    fingerprint: String,
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
            seen: HashSet::new(),
            seen_sanitizer_logs,
        }
    }

    fn take_new_sanitizer_evidence(&mut self) -> Option<SanitizerEvidence> {
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
            let fingerprint = sanitizer_log_fingerprint(&path, &metadata);
            candidates.push((
                modified,
                SanitizerEvidence {
                    kind,
                    fingerprint,
                    path,
                },
            ));
        }
        candidates.sort_by_key(|(modified, _)| *modified);
        for (_, evidence) in candidates {
            if self.seen_sanitizer_logs.insert(evidence.fingerprint.clone()) {
                return Some(evidence);
            }
        }
        None
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
        let digest = content_hash(input.as_ref());
        if !self.seen.insert(digest.clone()) {
            return Ok(());
        }

        let source = source_context(state);
        let source_label = seed_source_label(&source).to_string();
        let kind = sanitizer_evidence
            .map(|evidence| evidence.kind.clone())
            .unwrap_or_else(|| exit_kind_label(exit_kind).to_string());
        let sanitizer_log = sanitizer_evidence
            .map(|evidence| evidence.path.display().to_string());
        let parent_id = parent_hash(state);
        let finding_dir = self.findings_dir.join(&source_label);
        fs::create_dir_all(&finding_dir)?;
        fs::create_dir_all(&self.target_corpus_dir)?;

        let seed_path = finding_dir.join(format!("{digest}.seed"));
        fs::write(&seed_path, input.as_ref())?;

        let detail = if let Some(path) = sanitizer_log.as_deref() {
            format!(
                "{} discovered {} while executing structured seed {}; sanitizer log: {}",
                source_label, kind, digest, path
            )
        } else {
            format!(
                "{} discovered {} while executing structured seed {}",
                source_label, kind, digest
            )
        };
        let mut metadata = StSeedMetadata::new(source.clone(), parent_id.clone(), input.as_ref());
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
        let cycle_metadata = infer_seed_cycle_metadata(input.as_ref());
        let trace_metadata = read_fb_state_trace_metadata();
        let record = RuntimeFindingRecord {
            schema_version: "1.0",
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
            unix_time_seconds: now,
        };
        let json_path = finding_dir.join(format!("{digest}.json"));
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
                executions: *state.executions(),
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
        let sanitizer_evidence = self.take_new_sanitizer_evidence();
        if !exit_kind_is_finding(exit_kind) && sanitizer_evidence.is_none() {
            return Ok(false);
        }
        self.persist_finding(state, input, exit_kind, sanitizer_evidence.as_ref())?;
        Ok(true)
    }
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
fn mutate_record<R>(rand: &mut R, record: &mut StRecord)
where
    R: Rand,
{
    match record.ty.as_str() {
        "BOOL" => record.value = if rand.coinflip(0.5) { "1" } else { "0" }.to_string(),
        "REAL" | "LREAL" => {
            if rand.coinflip(0.35) {
                let values = float_boundary_values(&record.ty);
                record.value = values[rand.below_or_zero(values.len())].to_string();
            } else {
                let sign = if rand.coinflip(0.5) { "-" } else { "" };
                let whole = rand.between(0, 10_000);
                let frac = rand.between(0, 999);
                record.value = format!("{sign}{whole}.{frac:03}");
            }
        }
        "STRING" | "WSTRING" => {
            let len = rand.between(0, 64);
            record.value = random_ascii(rand, len);
        }
        "DATE" | "LDATE" | "DT" | "DATE_AND_TIME" | "LDT" | "LDATE_AND_TIME" | "TOD"
        | "TIME_OF_DAY" | "LTOD" | "LTIME_OF_DAY" | "TIME" | "LTIME" => {
            set_temporal_record_value(rand, record);
        }
        "ARRAY" | "POINTER" => {
            let len = rand.between(1, 64);
            record.value = random_hex(rand, len);
        }
        "SINT" | "INT" | "DINT" | "LINT" => {
            let choices = ["0", "1", "-1", "32767", "-32768", "65535"];
            if rand.coinflip(0.35) {
                record.value = choices[rand.below_or_zero(choices.len())].to_string();
            } else {
                let value = rand.between(0, 200_000) as i64 - 100_000;
                record.value = value.to_string();
            }
        }
        "USINT" | "UINT" | "UDINT" | "ULINT" | "BYTE" | "WORD" | "DWORD" | "LWORD" | "CHAR" => {
            let choices = ["0", "1", "255", "256", "65535", "4294967295"];
            if rand.coinflip(0.35) {
                record.value = choices[rand.below_or_zero(choices.len())].to_string();
            } else {
                record.value = rand.between(0, 200_000).to_string();
            }
        }
        _ => {
            let len = rand.between(0, 32);
            record.value = random_ascii(rand, len);
        }
    }
}

// 生成适合 STRING/WSTRING 的可打印文本片段。
fn random_ascii<R>(rand: &mut R, len: usize) -> String
where
    R: Rand,
{
    let alphabet = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/.:";
    let mut out = String::with_capacity(len);
    for _ in 0..len {
        out.push(alphabet[rand.below_or_zero(alphabet.len())] as char);
    }
    out
}

// 生成 ARRAY/POINTER backing buffer 使用的十六进制字节串，例如 `0x414243`。
fn random_hex<R>(rand: &mut R, len: usize) -> String
where
    R: Rand,
{
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::from("0x");
    for _ in 0..len {
        let byte = rand.between(0, 255);
        out.push(HEX[(byte >> 4) & 0xf] as char);
        out.push(HEX[byte & 0xf] as char);
    }
    out
}

// 运行配置：命令行参数和环境变量最终都会汇总到这里。
