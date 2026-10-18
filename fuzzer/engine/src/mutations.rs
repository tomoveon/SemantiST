#[derive(Debug)]
struct StructuredStMutator {
    name: Cow<'static, str>,
    ablation: SemantiSTAblation,
    // A mutational stage mutates fresh copies of one corpus input many times
    // in a row; the records parsed from the last input are reused for it.
    parsed: Option<(Vec<u8>, ParsedSeed)>,
}

#[derive(Clone, Debug)]
enum ParsedSeed {
    Cycles(CycleAwareSeed),
    Records(Vec<StRecord>),
}

const SEMANTIC_MUTATION_PROBABILITY: f64 = 0.70;
const DEFAULT_FB_MAX_CYCLES: usize = 256;
const HARD_FB_MAX_CYCLES: usize = 10_000;

fn fb_max_cycles() -> usize {
    static LIMIT: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    *LIMIT.get_or_init(|| parse_fb_max_cycles(env::var("SEMANTIST_FB_MAX_CYCLES").ok().as_deref()))
}

fn parse_fb_max_cycles(value: Option<&str>) -> usize {
    value.and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_FB_MAX_CYCLES)
        .min(HARD_FB_MAX_CYCLES)
}
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
    fn new(ablation: SemantiSTAblation) -> Self {
        Self {
            name: Cow::Borrowed("StructuredStMutator"),
            ablation,
            parsed: None,
        }
    }

    fn parse(&mut self, input: &[u8]) -> &ParsedSeed {
        if self.parsed.as_ref().is_none_or(|(bytes, _)| bytes.as_slice() != input) {
            // LibAFL 的 input 仍然是 BytesInput；这里先把 bytes 当 UTF-8 文本解析成记录。
            let text = String::from_utf8_lossy(input);
            let parsed = if has_cycle_record(&text) {
                ParsedSeed::Cycles(parse_cycle_aware_seed(&text))
            } else {
                ParsedSeed::Records(parse_structured_records(&text))
            };
            self.parsed = Some((input.to_vec(), parsed));
        }
        &self.parsed.as_ref().expect("parsed above").1
    }

    fn mutate_with_context<S>(
        &mut self,
        state: &mut S,
        input: &mut BytesInput,
        context: &TargetContext,
    ) -> Result<MutationResult, Error>
    where
        S: HasRand,
    {
        let mut records = match self.parse(input.as_ref()).clone() {
            ParsedSeed::Records(records) => records,
            ParsedSeed::Cycles(mut seed) => {
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
                let use_semantic = self.ablation.targeted_mutation()
                    && context.target_id.is_some()
                    && state
                        .rand_mut()
                        .coinflip(SEMANTIC_MUTATION_PROBABILITY);
                if !use_semantic
                    || !semantic_mutate_cycle_seed(state.rand_mut(), &mut seed, context, self.ablation)
                {
                    mutate_cycle_aware_seed(state.rand_mut(), &mut seed);
                }
                *input = BytesInput::new(serialize_cycle_aware_seed(&seed).into_bytes());
                return Ok(MutationResult::Mutated);
            }
        };
        if records.is_empty() {
            records.push(StRecord {
                name: "IN".to_string(),
                ty: "INT".to_string(),
                value: "0".to_string(),
            });
        }

        let use_semantic = self.ablation.targeted_mutation()
            && context.target_id.is_some()
            && state
                .rand_mut()
                .coinflip(SEMANTIC_MUTATION_PROBABILITY);
        if !use_semantic || !semantic_mutate_records(state.rand_mut(), &mut records, context) {
            mutate_records_fallback(state.rand_mut(), &mut records);
        }

        *input = BytesInput::new(serialize_records(&records).into_bytes());
        Ok(MutationResult::Mutated)
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
        let _timer = PhaseTimer::start(Phase::Mutate);
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
        // The semantic guidance is only read here. Moving it out of the
        // metadata map for one mutation avoids cloning its expression trees.
        let context = state.metadata_map_mut().remove::<TargetContext>();
        let result = self.mutate_with_context(state, input, context.as_deref().unwrap_or(&TargetContext::default()));
        if let Some(context) = context {
            state.metadata_map_mut().insert_boxed(context);
        }
        result
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

// 一行 `NAME,TYPE,VALUE` 的三个字段（借用原文本）；格式不完整的行返回 None。
fn structured_record_fields(line: &str) -> Option<(&str, &str, &str)> {
    let mut parts = line.splitn(3, ',');
    let name = parts.next()?.trim();
    let ty = parts.next()?.trim();
    let value = parts.next().unwrap_or("").trim();
    if name.is_empty() || ty.is_empty() {
        return None;
    }
    Some((name, ty, value))
}

// 从 corpus/queue 中读取的文本输入解析成记录列表。
fn parse_structured_records(text: &str) -> Vec<StRecord> {
    text.lines()
        .filter_map(structured_record_fields)
        .map(|(name, ty, value)| StRecord {
            name: name.to_string(),
            ty: ty.to_ascii_uppercase(),
            value: value.to_string(),
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
    let kind = expression.get("kind")?;
    let qualified = match kind.get("kind")?.as_str()? {
        "variable" => kind.get("qualified_name")?.as_str()?,
        "member_access" => {
            // The compiler wraps a direct identifier in MemberAccess. Only
            // unwrap that identity-preserving leaf, never arbitrary reads or
            // a real member selection with a base/compound expression.
            let [variable] = expression_operands(expression) else { return None; };
            let variable_kind = variable.get("kind")?;
            if variable_kind.get("kind")?.as_str()? != "variable"
                || !variable.get("operands")?.as_array()?.is_empty()
            { return None; }
            let qualified = variable_kind.get("qualified_name")?.as_str()?;
            let member = kind.get("member")?.as_str()?;
            let type_name = expression.get("type_name")?.as_str()?;
            if qualified.is_empty() || member.is_empty() || type_name.is_empty()
                || !qualified.rsplit('.').next()?.eq_ignore_ascii_case(member)
                || !type_name.eq_ignore_ascii_case(variable.get("type_name")?.as_str()?)
            { return None; }
            let reads_same_variable = |value: &serde_json::Value| {
                value.get("reads").and_then(|reads| reads.as_array()).is_some_and(|reads| {
                    reads.len() == 1
                        && reads[0].as_str().is_some_and(|read| read.eq_ignore_ascii_case(qualified))
                })
            };
            if !reads_same_variable(expression) || !reads_same_variable(variable) {
                return None;
            }
            qualified
        }
        _ => return None,
    };
    Some(qualified.to_ascii_uppercase())
}

fn expression_definition<'a>(
    variable: &str,
    definitions: &'a BTreeMap<String, serde_json::Value>,
) -> Option<&'a serde_json::Value> {
    definitions.get(variable).or_else(|| {
        // Compatibility with older plans is limited to a direct POU local.
        // Never collapse P.instance.X and P.X into the same short symbol.
        let (_, local) = variable.split_once('.')?;
        if local.contains(['.', ':']) { return None; }
        definitions.get(local)
    })
}

fn seed_variable_was_written(
    expression: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>,
) -> bool {
    expression_variable(expression)
        .is_some_and(|variable| expression_definition(&variable, definitions).is_some())
}

fn integer_record_bounds(ty: &str) -> Option<(i128, i128)> {
    let (bits, signed) = match ty {
        "SINT" => (8, true), "INT" => (16, true), "DINT" => (32, true), "LINT" => (64, true),
        "BOOL" => return Some((0, 1)),
        "USINT" | "BYTE" | "CHAR" => (8, false), "UINT" | "WORD" | "WCHAR" => (16, false),
        "UDINT" | "DWORD" => (32, false), "ULINT" | "LWORD" => (64, false),
        _ => return None,
    };
    Some(if signed { (-(1_i128 << (bits - 1)), (1_i128 << (bits - 1)) - 1) }
        else { (0, (1_i128 << bits) - 1) })
}

fn parse_wide_integer(raw: &str) -> Option<i128> {
    let value = raw.trim().replace('_', "");
    if value.eq_ignore_ascii_case("TRUE") { return Some(1); }
    if value.eq_ignore_ascii_case("FALSE") { return Some(0); }
    let (negative, digits) = value.strip_prefix('-').map(|digits| (true, digits))
        .unwrap_or((false, value.trim_start_matches('+')));
    let result = if let Some(digits) = digits.strip_prefix("0x").or_else(|| digits.strip_prefix("0X")) {
        i128::from_str_radix(digits, 16).ok()?
    } else { digits.parse::<i128>().ok()? };
    if negative { result.checked_neg() } else { Some(result) }
}

fn integer_expression_wide(records: &[StRecord], expression: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>, depth: usize) -> Option<i128>
{
    if depth > 16 { return None; }
    if expression.get("type_name").and_then(|ty| ty.as_str()).is_some_and(is_float_type) {
        return None;
    }
    if let Some(index) = expression_record_index(records, expression) {
        // The seed stores the invocation input, not a later assignment's
        // value. Without variable versions, even a known write is ambiguous.
        if seed_variable_was_written(expression, definitions) { return None; }
        let (low, high) = integer_record_bounds(&records[index].ty)?;
        return parse_wide_integer(&records[index].value).filter(|value| (low..=high).contains(value));
    }
    if let Some(value) = expression.get("kind").and_then(|kind| kind.get("value")) {
        if value.get("kind").and_then(|kind| kind.as_str()) == Some("real") { return None; }
        if let Some(value) = value.get("value").and_then(|value| value.as_bool()) {
            return Some(i128::from(value));
        }
        if let Some(raw) = value.get("value").and_then(|value| value.as_str()).or_else(|| value.as_str()) {
            return parse_wide_integer(raw);
        }
    }
    if let Some(variable) = expression_variable(expression) {
        return integer_expression_wide(records, expression_definition(&variable, definitions)?, definitions, depth + 1);
    }
    let operands = expression_operands(expression);
    if operands.len() == 1 {
        let operand = integer_expression_wide(records, &operands[0], definitions, depth + 1)?;
        let result = match expression_operator(expression)? {
            "+" => Some(operand), "-" => operand.checked_neg(), _ => None,
        }?;
        if let Some((low, high)) = expression.get("type_name").and_then(|ty| ty.as_str()).and_then(integer_record_bounds) {
            if !(low..=high).contains(&result) { return None; }
        }
        return Some(result);
    }
    if operands.len() != 2 { return None; }
    let left = integer_expression_wide(records, &operands[0], definitions, depth + 1)?;
    let right = integer_expression_wide(records, &operands[1], definitions, depth + 1)?;
    let result = match expression_operator(expression)? {
        "+" => left.checked_add(right), "-" => left.checked_sub(right), "*" => left.checked_mul(right),
        "/" | "DIV" => left.checked_div(right), "MOD" => left.checked_rem(right), _ => None,
    }?;
    if let Some((low, high)) = expression.get("type_name").and_then(|ty| ty.as_str()).and_then(integer_record_bounds) {
        if result < low || result > high { return None; }
    }
    Some(result)
}

fn mutate_integer_comparison(record: &mut StRecord, operator: &str, fixed: i128,
    variable_on_left: bool, desired: bool) -> bool
{
    let Some((low, high)) = integer_record_bounds(&record.ty) else { return false; };
    for candidate in [Some(fixed), fixed.checked_sub(1), fixed.checked_add(1), Some(low), Some(high)].into_iter().flatten() {
        if candidate < low || candidate > high { continue; }
        let (left, right) = if variable_on_left { (candidate, fixed) } else { (fixed, candidate) };
        let satisfied = match operator {
            "<" => left < right, "<=" => left <= right, ">" => left > right, ">=" => left >= right,
            "=" => left == right, "<>" => left != right, _ => return false,
        };
        if satisfied == desired && parse_wide_integer(&record.value) != Some(candidate) {
            record.value = candidate.to_string();
            return true;
        }
    }
    false
}

fn expression_record_index(records: &[StRecord], expression: &serde_json::Value) -> Option<usize> {
    let variable = expression_variable(expression)?;
    let local = variable.split_once('.').map(|(_, local)| local);
    let mut indices = records
        .iter()
        .enumerate()
        .filter_map(|(index, record)| {
            let name = normalized_field_name(&record.name);
            (name == variable || local == Some(name.as_str())).then_some(index)
        });
    let index = indices.next()?;
    indices.next().is_none().then_some(index)
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
    if let Some(value) = integer_expression_wide(records, expression, definitions, depth) {
        // Do not silently lose low bits of LINT/ULINT in a floating constraint.
        return (-(1_i128 << 53)..=(1_i128 << 53)).contains(&value).then_some(value as f64);
    }
    if let Some(index) = expression_record_index(records, expression) {
        if seed_variable_was_written(expression, definitions) { return None; }
        if !is_float_type(&records[index].ty) { return None; }
        return typed_float_value(&records[index].ty, parse_number_value(&records[index].value)?);
    }
    if let Some(value) = expression_number_literal(expression) {
        return typed_float_value(expression.get("type_name").and_then(|ty| ty.as_str()).unwrap_or("LREAL"), value);
    }
    if let Some(variable) = expression_variable(expression) {
        if let Some(definition) = expression_definition(&variable, definitions) {
            return expression_number_value(records, definition, definitions, depth + 1);
        }
    }
    let operands = expression_operands(expression);
    let operator = expression_operator(expression)?;
    if operands.len() == 1 {
        let operand = expression_number_value(records, &operands[0], definitions, depth + 1)?;
        return match operator { "+" => Some(operand), "-" => Some(-operand), _ => None };
    }
    if operands.len() != 2 {
        return None;
    }
    let left = expression_number_value(records, &operands[0], definitions, depth + 1)?;
    let right = expression_number_value(records, &operands[1], definitions, depth + 1)?;
    let value = match operator.to_ascii_uppercase().as_str() {
        "+" => Some(left + right),
        "-" => Some(left - right),
        "*" => Some(left * right),
        "/" => (right != 0.0).then_some(left / right),
        _ => None,
    }?;
    typed_float_value(expression.get("type_name").and_then(|ty| ty.as_str()).unwrap_or("LREAL"), value)
}

fn evaluate_integer_expression(
    records: &[StRecord],
    expression: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> Option<i64> {
    integer_expression_wide(records, expression, definitions, depth)?.try_into().ok()
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
    if depth == 0 {
        let original = records.to_vec();
        if mutate_expression_to_value(records, expression, desired, definitions, 1)
            && integer_expression_wide(records, expression, definitions, 0) == Some(desired as i128)
        {
            return true;
        }
        records.clone_from_slice(&original);
        return false;
    }
    if let Some(index) = expression_record_index(records, expression) {
        if seed_variable_was_written(expression, definitions) { return false; }
        if integer_record_bounds(&records[index].ty)
            .is_some_and(|(low, high)| (low..=high).contains(&(desired as i128)))
            && parse_wide_integer(&records[index].value) != Some(desired as i128)
        {
            records[index].value = desired.to_string();
            return true;
        }
        return false;
    }
    if let Some(variable) = expression_variable(expression) {
        if let Some(definition) = expression_definition(&variable, definitions) {
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
    let Some(operator) = expression_operator(expression) else { return false; };
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

fn typed_float_value(ty: &str, value: f64) -> Option<f64> {
    if integer_record_bounds(ty).is_some() { return None; }
    let value = if ty == "REAL" { value as f32 as f64 } else { value };
    value.is_finite().then_some(value)
}

fn comparison_holds<T: PartialOrd>(operator: &str, left: T, right: T) -> Option<bool> {
    match operator {
        "<" => Some(left < right), "<=" => Some(left <= right),
        ">" => Some(left > right), ">=" => Some(left >= right),
        "=" => Some(left == right), "<>" => Some(left != right), _ => None,
    }
}

fn mutate_float_comparison(
    record: &mut StRecord,
    operator: &str,
    fixed: f64,
    variable_on_left: bool,
    desired: bool,
) -> bool {
    if !fixed.is_finite() { return false; }
    let candidates = if record.ty == "REAL" {
        let rounded = fixed as f32;
        [rounded as f64, rounded.next_down() as f64, rounded.next_up() as f64]
    } else {
        [fixed, fixed.next_down(), fixed.next_up()]
    };
    let current = parse_number_value(&record.value).and_then(|value| typed_float_value(&record.ty, value));
    for candidate in candidates {
        if !candidate.is_finite() || current == Some(candidate) { continue; }
        let (left, right) = if variable_on_left { (candidate, fixed) } else { (fixed, candidate) };
        if comparison_holds(operator, left, right) == Some(desired) {
            set_number_record_value(record, candidate);
            return true;
        }
    }
    false
}

fn set_number_record_value(record: &mut StRecord, value: f64) {
    if let Some(value) = typed_float_value(&record.ty, value) {
        // Rust's shortest round-trip representation preserves adjacent IEEE
        // values; fixed decimal precision can round a strict boundary away.
        if record.ty == "REAL" {
            record.value = (value as f32).to_string();
        } else if record.ty == "LREAL" {
            record.value = value.to_string();
        }
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
    if !is_float_type(&records[lower.value_index].ty) { return false; }
    let mut low = lower.bound_value;
    let mut high = upper.bound_value;
    let needs_room = low > high || (low == high && (!lower.inclusive || !upper.inclusive));
    if needs_room {
        let (Some(lower_index), Some(upper_index)) = (lower.bound_index, upper.bound_index) else { return false; };
        if lower_index == upper_index || lower_index == lower.value_index || upper_index == lower.value_index
            || !is_float_type(&records[lower_index].ty) || !is_float_type(&records[upper_index].ty)
        { return false; }
        low = 0.0;
        high = 1.0;
        set_number_record_value(&mut records[lower_index], low);
        set_number_record_value(&mut records[upper_index], high);
    }
    let value = low / 2.0 + high / 2.0;
    set_number_record_value(&mut records[lower.value_index], value);
    true
}

fn evaluate_expression_goal(
    records: &[StRecord],
    expression: &serde_json::Value,
    definitions: &BTreeMap<String, serde_json::Value>,
    depth: usize,
) -> Option<bool> {
    if depth > 16 { return None; }
    if let Some(value) = expression.get("kind").and_then(|kind| kind.get("value"))
        .and_then(|value| value.get("value")).and_then(|value| value.as_bool())
    { return Some(value); }
    if let Some(variable) = expression_variable(expression) {
        if let Some(index) = expression_record_index(records, expression) {
            if expression_definition(&variable, definitions).is_some() { return None; }
            return (records[index].ty == "BOOL")
                .then(|| parse_wide_integer(&records[index].value).map(|value| value != 0)).flatten();
        }
        return evaluate_expression_goal(records, expression_definition(&variable, definitions)?, definitions, depth + 1);
    }
    let operands = expression_operands(expression);
    let operator = expression_operator(expression)?;
    if operator.eq_ignore_ascii_case("NOT") && operands.len() == 1 {
        return evaluate_expression_goal(records, &operands[0], definitions, depth + 1).map(|value| !value);
    }
    if operands.len() != 2 { return None; }
    if operator.eq_ignore_ascii_case("AND") || operator.eq_ignore_ascii_case("OR") {
        let left = evaluate_expression_goal(records, &operands[0], definitions, depth + 1)?;
        let right = evaluate_expression_goal(records, &operands[1], definitions, depth + 1)?;
        return Some(if operator.eq_ignore_ascii_case("AND") { left && right } else { left || right });
    }
    if let (Some(left), Some(right)) = (
        integer_expression_wide(records, &operands[0], definitions, depth + 1),
        integer_expression_wide(records, &operands[1], definitions, depth + 1),
    ) {
        return comparison_holds(operator, left, right);
    }
    comparison_holds(operator,
        expression_number_value(records, &operands[0], definitions, depth + 1)?,
        expression_number_value(records, &operands[1], definitions, depth + 1)?)
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
        if let Some(index) = expression_record_index(records, expression) {
            if expression_definition(&variable, definitions).is_some() { return false; }
            if records[index].ty != "BOOL" { return false; }
            let value = if desired { "1" } else { "0" };
            if records[index].value == value { return false; }
            records[index].value = value.to_string();
            return true;
        }
        if let Some(definition) = expression_definition(&variable, definitions) {
            return mutate_expression_goal(records, definition, desired, definitions, depth + 1);
        }
        return false;
    }
    let operands = expression_operands(expression);
    let Some(operator) = expression_operator(expression) else { return false; };
    if operator.eq_ignore_ascii_case("NOT") && operands.len() == 1 {
        return mutate_expression_goal(records, &operands[0], !desired, definitions, depth + 1);
    }
    if (operator.eq_ignore_ascii_case("AND") || operator.eq_ignore_ascii_case("OR")) && operands.len() == 2 {
        // A bounded local attempt, not a solver: preserve the original input
        // unless the whole Boolean goal is satisfied after the edits.
        let original = records.to_vec();
        if operator.eq_ignore_ascii_case("AND") && desired {
            mutate_range_conjunction(records, &operands[0], &operands[1], definitions, depth + 1);
        }
        let needs_both = operator.eq_ignore_ascii_case("AND") == desired;
        for operand in operands {
            if evaluate_expression_goal(records, operand, definitions, depth + 1) != Some(desired) {
                mutate_expression_goal(records, operand, desired, definitions, depth + 1);
            }
            if !needs_both && evaluate_expression_goal(records, expression, definitions, depth) == Some(desired) {
                break;
            }
        }
        if evaluate_expression_goal(records, expression, definitions, depth) == Some(desired) {
            return records.iter().zip(&original).any(|(new, old)| new.value != old.value);
        }
        records.clone_from_slice(&original);
        return false;
    }
    if !matches!(operator, "<" | "<=" | ">" | ">=" | "=" | "<>") || operands.len() != 2 {
        return false;
    }
    for (variable, fixed, on_left) in [(&operands[0], &operands[1], true), (&operands[1], &operands[0], false)] {
        let Some(index) = expression_record_index(records, variable) else { continue; };
        if seed_variable_was_written(variable, definitions) { continue; }
        let original = records[index].value.clone();
        let changed = if integer_record_bounds(&records[index].ty).is_some() {
            integer_expression_wide(records, fixed, definitions, depth + 1)
                .is_some_and(|value| mutate_integer_comparison(&mut records[index], operator, value, on_left, desired))
        } else if is_float_type(&records[index].ty) {
            expression_number_value(records, fixed, definitions, depth + 1)
                .is_some_and(|value| mutate_float_comparison(&mut records[index], operator, value, on_left, desired))
        } else { false };
        if changed && evaluate_expression_goal(records, expression, definitions, depth) == Some(desired) {
            return true;
        }
        records[index].value = original;
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
    let named = |expected: &str| {
        field
            .type_name
            .as_deref()
            .is_some_and(|name| name.eq_ignore_ascii_case(expected))
    };
    let fact = field.type_fact.as_ref();
    // Width alone does not make a float: TIME, DATE, LINT and LWORD are also
    // 64 bits wide in RuSTy and must keep integer values.
    let is_float = fact
        .and_then(|fact| fact.get("kind"))
        .and_then(|kind| kind.as_str())
        == Some("float");
    let width = fact.and_then(|fact| {
        fact.get("semantic_bit_width")
            .or_else(|| fact.get("bit_width"))
            .and_then(|value| value.as_u64())
    });
    if named("LREAL") || (is_float && width == Some(64)) {
        return Some("LREAL");
    }
    if named("REAL") || is_float {
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
    // A task may end at a hazard while its active guide is still a branch or
    // state prerequisite. Only apply hazard-specific operands to a hazard guide.
    if !matches!(context.target_type.as_deref(), Some("hazard_reach" | "hazard_violation")) {
        return false;
    }
    let Some(expression) = context.goal_expression.as_ref() else {
        return false;
    };
    let violation = context.target_type.as_deref() == Some("hazard_violation");
    let operands = expression_operands(expression);
    match context.hazard_kind.as_deref() {
        Some("division" | "modulo")
            if operands.len() == 2
                && expression_operator(expression).is_some_and(|operator| {
                    matches!(operator, "/" | "%")
                        || operator.eq_ignore_ascii_case("DIV")
                        || operator.eq_ignore_ascii_case("MOD")
                }) => {
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

fn split_cycle_prefix(name: &str) -> Option<(usize, &str)> {
    let (prefix, rest) = name.split_once('.')?;
    if prefix.is_empty() || rest.is_empty() || !prefix.chars().all(|ch| ch.is_ascii_digit()) {
        return None;
    }
    Some((prefix.parse().ok()?, rest))
}

fn split_cycle_record_name(name: &str) -> Option<(usize, String)> {
    split_cycle_prefix(name).map(|(cycle, rest)| (cycle, rest.to_string()))
}

// 每次变异都要判断，所以只扫描字段，不分配记录。
fn has_cycle_record(text: &str) -> bool {
    text.lines()
        .filter_map(structured_record_fields)
        .any(|(name, _, _)| split_cycle_prefix(name).is_some())
}

fn parse_cycle_aware_seed(text: &str) -> CycleAwareSeed {
    let mut seed = CycleAwareSeed::default();
    for (name, ty, value) in text.lines().filter_map(structured_record_fields) {
        let (cycle, field_name) = match split_cycle_prefix(name) {
            Some((cycle, field_name)) => (Some(cycle), field_name),
            None => (None, name),
        };
        let record = StRecord {
            name: field_name.to_string(),
            ty: ty.to_ascii_uppercase(),
            value: value.to_string(),
        };
        match cycle {
            Some(cycle) => seed.cycles.entry(cycle).or_default().push(record),
            None => seed.shared.push(record),
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
        use std::fmt::Write as _;
        let _ = write!(out, "{cycle}.");
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
    ablation: SemantiSTAblation,
) -> bool
where
    R: Rand,
{
    if context.target_id.is_none() {
        return false;
    }
    ensure_cycle_hint(seed, context.cycle_hint.unwrap_or(3).max(1) as usize);
    // Keep exploring deeper state histories even when the terminal predicate
    // is easy to mutate. This is a cheap, bounded counterpart to a dynamic
    // scan horizon and preserves the terminal cycle while growing its prefix.
    if ablation.prefix_growth()
        && (context.cycle_hint.unwrap_or(1) > 1 || !context.transition_seed_fields.is_empty())
        && rand.coinflip(0.15)
        && expand_state_prefix(rand, seed)
    {
        return true;
    }
    // Spend some mutations on the state-building prefix, but retain direct
    // terminal-goal mutations and ordinary cycle havoc for exploration.
    if ablation.transition_guidance()
        && !context.transition_seed_fields.is_empty()
        && rand.coinflip(0.5)
        && mutate_transition_prefix(rand, seed, &context.transition_seed_fields)
    {
        normalize_cycle_ids(seed);
        return true;
    }
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
    if ablation.transition_guidance()
        && mutate_transition_prefix(rand, seed, &context.transition_seed_fields)
    {
        normalize_cycle_ids(seed);
        return true;
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
        1 if cycles.len() < fb_max_cycles() => {
            let duplicate = cycles[selected].clone();
            cycles.insert(selected.saturating_add(1), duplicate);
            true
        }
        2 if cycles.len() < fb_max_cycles() => {
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

fn mutate_transition_prefix<R>(
    rand: &mut R,
    seed: &mut CycleAwareSeed,
    transfer_fields: &[Vec<ControllableSeedField>],
) -> bool
where
    R: Rand,
{
    if seed.cycles.len() < 2 || transfer_fields.is_empty() {
        return false;
    }
    let transfer = &transfer_fields[rand.below_or_zero(transfer_fields.len())];
    let prefix_cycle = rand.below_or_zero(seed.cycles.len() - 1);
    seed.cycles
        .values_mut()
        .nth(prefix_cycle)
        .is_some_and(|records| mutate_controllable_seed_field(rand, records, transfer))
}

fn expand_state_prefix<R>(rand: &mut R, seed: &mut CycleAwareSeed) -> bool
where
    R: Rand,
{
    if seed.cycles.len() < 2 || seed.cycles.len() >= fb_max_cycles() {
        return false;
    }
    let mut cycles = seed.cycles.values().cloned().collect::<Vec<_>>();
    let source = rand.below_or_zero(cycles.len() - 1);
    let duplicate = cycles[source].clone();
    cycles.insert(source + 1, duplicate);
    seed.cycles = cycles.into_iter().enumerate().collect();
    true
}

fn ensure_cycle_hint(seed: &mut CycleAwareSeed, desired_cycles: usize) {
    let desired_cycles = desired_cycles.max(1).min(fb_max_cycles());
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
    if seed.cycles.len() >= fb_max_cycles() {
        return;
    }
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
    if seed.cycles.len() >= fb_max_cycles() {
        return;
    }
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

    fn record_findings<S>(&mut self, state: &mut S, input: &BytesInput, exit_kind: &ExitKind) -> Result<bool, Error>
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
        if self.logs_only_after_failures && !self.unclaimed_logs && !exit_kind_is_finding(exit_kind) {
            return false;
        }
        let modified = fs::metadata(&self.sanitizer_log_dir).and_then(|metadata| metadata.modified()).ok();
        let settled = modified.is_some_and(|modified| {
            SystemTime::now().duration_since(modified).is_ok_and(|age| age >= SANITIZER_LOG_SETTLE)
        });
        let changed = self.unclaimed_logs || exit_kind_is_finding(exit_kind)
            || modified.is_none() || modified != self.scanned_log_dir || !settled;
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
            if self.records.seen_sanitizer_logs.insert(evidence.fingerprint.clone()) {
                // One log per execution; the rest are claimed by later ones.
                self.unclaimed_logs = candidates.len() > 0;
                return Some(evidence);
            }
        }
        None
    }

    fn write_reproducer(&self, source_label: &str, input: &BytesInput) -> Result<(String, PathBuf, PathBuf), Error> {
        let digest = content_hash(input.as_ref());
        let finding_dir = self.findings_dir.join(source_label);
        fs::create_dir_all(&finding_dir)?;
        fs::create_dir_all(&self.target_corpus_dir)?;
        let seed_path = finding_dir.join(format!("{digest}.seed"));
        fs::write(&seed_path, input.as_ref())?;
        Ok((digest, finding_dir, seed_path))
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
        let _timer = PhaseTimer::start(Phase::IoFinding);
        let source_label = seed_source_label(&source_context(state)).to_string();
        let (digest, finding_dir, seed_path) = self.write_reproducer(&source_label, input)?;
        let record = SemanticFindingRecord {
            schema_version: "1.0",
            kind: "semantic_violation",
            source: source_label,
            content_hash: digest,
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
        let file_stem = entry.stable_id.replace(|c: char| !c.is_ascii_alphanumeric(), "_");
        let serialized = serde_json::to_string_pretty(&record).map_err(|error| {
            Error::serialize(format!("unable to serialize semantic finding: {error}"))
        })?;
        fs::write(finding_dir.join(format!("semantic-{file_stem}.json")), format!("{serialized}\n"))?;
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
        let occurrences = self.records.signatures.entry(signature.clone()).or_insert(0);
        *occurrences += 1;
        if *occurrences > 1 {
            return Ok(());
        }
        let _timer = PhaseTimer::start(Phase::IoFinding);

        let source = source_context(state);
        let source_label = seed_source_label(&source).to_string();
        let kind = sanitizer_evidence
            .map(|evidence| evidence.kind.clone())
            .unwrap_or_else(|| exit_kind_label(exit_kind).to_string());
        let sanitizer_log = sanitizer_evidence
            .map(|evidence| evidence.path.display().to_string());
        let parent_id = parent_hash(state);
        let (digest, finding_dir, seed_path) = self.write_reproducer(&source_label, input)?;

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
        let mut metadata = StSeedMetadata::new(source.clone(), *state.corpus().current());
        metadata.content_hash = digest.clone();
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
        let known = (self.records.signatures.len(), self.records.seen_sanitizer_logs.len());
        let result = self.record_findings(state, input, exit_kind);
        if known != (self.records.signatures.len(), self.records.seen_sanitizer_logs.len()) {
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
            report
                .lines()
                .find_map(|line| line.split_once("runtime error: ").map(|(_, rest)| rest.trim().to_string()))
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

#[cfg(test)]
mod mutation_regression_tests {
    use super::*;

    fn variable(name: &str) -> serde_json::Value {
        serde_json::json!({"kind": {"kind": "variable", "qualified_name": name}, "reads": [name], "operands": []})
    }

    fn integer(value: &str) -> serde_json::Value {
        serde_json::json!({"kind": {"kind": "literal", "value": {"kind": "integer", "value": value}}})
    }

    fn binary(operator: &str, left: serde_json::Value, right: serde_json::Value) -> serde_json::Value {
        serde_json::json!({"kind": {"kind": "binary", "operator": operator}, "operands": [left, right], "reads": ["P.X"]})
    }

    fn compiler_member_variable(name: &str, ty: &str) -> serde_json::Value {
        // Shape emitted by the real ALGORITHM_BRANCH compiler plan, including
        // the single variable operand, both read sets, and both type names.
        serde_json::json!({
            "kind": {"kind": "member_access", "member": name.rsplit('.').next().unwrap()},
            "operands": [{
                "kind": {"kind": "variable", "qualified_name": name},
                "operands": [], "reads": [name], "type_name": ty
            }],
            "reads": [name], "type_name": ty
        })
    }

    #[test]
    fn branch_prerequisite_is_not_mutated_as_the_terminal_division() {
        let goal = binary(">", variable("P.X"), variable("P.Y"));
        let context = TargetContext {
            target_id: Some("branch:true".into()),
            target_type: Some("branch_outcome".into()),
            hazard_kind: Some("division".into()),
            goal_expression: Some(goal.clone()),
            ..TargetContext::default()
        };
        let mut rand = StdRand::with_seed(12);
        let mut records = parse_structured_records("X,DINT,0\nY,DINT,5\n");
        assert!(!mutate_hazard_goal(&mut rand, &mut records, &context));
        assert_eq!(serialize_records(&records), "X,DINT,0\nY,DINT,5\n");
        assert!(semantic_mutate_records(&mut rand, &mut records, &context));
        assert_eq!(evaluate_expression_goal(&records, &goal, &BTreeMap::new(), 0), Some(true));
        // Also reject a mismatched expression in a hazard-labelled context.
        let invalid = TargetContext { target_type: Some("hazard_reach".into()), ..context };
        assert!(!mutate_hazard_goal(&mut rand, &mut records, &invalid));
    }

    #[test]
    fn sanitizer_signature_is_stable_across_runs_and_skips_libc_frames() {
        let report = |dir: &str| format!(
            "==1==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x1 at pc 0x2\n\
             \x20   #0 0x55 ({dir}/fuzz_target+0xd06ad) (BuildId: aa)\n\
             \x20   #1 0x56 (/lib/x86_64-linux-gnu/libc.so.6+0x2a1c9) (BuildId: bb)\n\
             \x20   #2 0x57 ({dir}/fuzz_target+0x11d49f) (BuildId: aa)\n\
             \x20   #3 0x58 ({dir}/fuzz_target+0x1192a0) (BuildId: aa)\n\
             \x20   #4 0x59 ({dir}/fuzz_target+0x11846b) (BuildId: aa)\n\
             SUMMARY: AddressSanitizer: stack-buffer-overflow ({dir}/fuzz_target+0xd06ad) (BuildId: aa)\n");
        let first = sanitizer_signature("asan", &report("/runs/trial_01"));
        assert_eq!(
            first,
            "asan|AddressSanitizer: stack-buffer-overflow|fuzz_target+0xd06ad,fuzz_target+0x11d49f,fuzz_target+0x1192a0"
        );
        assert_eq!(first, sanitizer_signature("asan", &report("/runs/trial_02")));
        let other = report("/runs/trial_01").replace("0x11d49f", "0x11d4a0");
        assert_ne!(first, sanitizer_signature("asan", &other));
        assert_eq!(
            sanitizer_signature("ubsan", "x.st:3:1: runtime error: division by zero\n"),
            "ubsan|division by zero|"
        );
    }

    #[test]
    fn sanitizer_history_dedup_preserves_new_and_updated_reports() {
        let root = env::temp_dir().join(format!("semantist-log-dedup-{}-{}", std::process::id(), current_nanos()));
        fs::create_dir_all(&root).unwrap();
        let old = root.join("asan.old");
        fs::write(&old, b"ERROR: AddressSanitizer: old report\n").unwrap();
        let mut feedback = RuntimeFindingFeedback::new(
            root.join("findings"), root.join("corpus"), None, root.join("events.jsonl"), root.clone());
        assert!(feedback.take_new_sanitizer_evidence().is_none());
        fs::write(root.join("ubsan.new"), b"runtime error: division by zero\n").unwrap();
        fs::write(root.join("asan.new"), b"ERROR: AddressSanitizer: new report\n").unwrap();
        fs::write(root.join("asan.noise"), b"LeakSanitizer has encountered a fatal error\nLeakSanitizer does not work under ptrace\n").unwrap();
        fs::write(root.join("irrelevant.txt"), b"runtime error: ignored name\n").unwrap();
        let mut kinds = Vec::new();
        while let Some(evidence) = feedback.take_new_sanitizer_evidence() {
            kinds.push(evidence.kind);
        }
        kinds.sort();
        assert_eq!(kinds, vec!["asan", "ubsan"]);
        // A changed existing file must still be consumed, even on a later scan.
        fs::write(&old, b"ERROR: AddressSanitizer: updated and longer report\n").unwrap();
        let updated = feedback.take_new_sanitizer_evidence().unwrap();
        assert_eq!(updated.path, old);
        assert_eq!(updated.kind, "asan");
        assert!(feedback.take_new_sanitizer_evidence().is_none());
        // An initially irrelevant/incomplete log can become a valid report.
        fs::write(root.join("asan.noise"), b"runtime error: new report after noise\n").unwrap();
        assert_eq!(feedback.take_new_sanitizer_evidence().unwrap().kind, "ubsan");
        assert!(feedback.take_new_sanitizer_evidence().is_none());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn compiler_member_plan_goals_use_the_controllable_input() {
        let defs = BTreeMap::new();
        let mut records = parse_structured_records("X,DINT,0\nY,DINT,0\n");
        let goal = serde_json::json!({
            "kind": {"kind": "binary", "operator": "="},
            "operands": [compiler_member_variable("ALGORITHM_BRANCH.X", "DINT"), {
                "kind": {"kind": "literal", "value": {"kind": "integer", "value": "42"}},
                "operands": [], "reads": [], "type_name": "DINT"
            }],
            "reads": ["ALGORITHM_BRANCH.X"], "type_name": "BOOL"
        });
        assert_eq!(evaluate_expression_goal(&records, &goal, &defs, 0), Some(false));
        assert!(mutate_expression_goal(&mut records, &goal, true, &defs, 0));
        assert_eq!(serialize_records(&records), "X,DINT,42\nY,DINT,0\n");
        assert_eq!(evaluate_expression_goal(&records, &goal, &defs, 0), Some(true));

        let mut records = parse_structured_records("X,DINT,0\nY,DINT,20\n");
        let conjunction = binary("AND",
            binary(">", compiler_member_variable("ALGORITHM_BRANCH.X", "DINT"), integer("10")),
            binary("<", compiler_member_variable("ALGORITHM_BRANCH.Y", "DINT"), integer("20")));
        assert!(mutate_expression_goal(&mut records, &conjunction, true, &defs, 0));
        assert_eq!(serialize_records(&records), "X,DINT,11\nY,DINT,19\n");
        assert_eq!(evaluate_expression_goal(&records, &conjunction, &defs, 0), Some(true));
    }

    #[test]
    fn compiler_member_wrappers_reject_nonidentity_shapes() {
        let valid = compiler_member_variable("ALGORITHM_BRANCH.X", "DINT");
        for (pointer, replacement) in [
            ("/kind/kind", serde_json::json!("call")),
            ("/kind/kind", serde_json::json!("cast")),
            ("/kind/kind", serde_json::json!("array_index")),
            ("/kind/member", serde_json::json!("Y")),
            ("/type_name", serde_json::json!("REAL")),
            ("/type_name", serde_json::Value::Null),
            ("/reads", serde_json::json!(["OTHER.X"])),
            ("/reads", serde_json::json!(["ALGORITHM_BRANCH.X", "ALGORITHM_BRANCH.Y"])),
            ("/operands/0/reads", serde_json::json!(["OTHER.X"])),
            ("/operands/0/kind/qualified_name", serde_json::json!("OTHER.X")),
            ("/operands/0/operands", serde_json::json!([integer("1")])),
            ("/operands/0", binary("+", variable("ALGORITHM_BRANCH.X"), integer("1"))),
            ("/operands", serde_json::json!([valid["operands"][0], valid["operands"][0]])),
        ] {
            let mut invalid = valid.clone();
            *invalid.pointer_mut(pointer).unwrap() = replacement;
            let mut records = parse_structured_records("X,DINT,0\n");
            let goal = binary("=", invalid.clone(), integer("42"));
            assert!(expression_variable(&invalid).is_none(), "{pointer}: {invalid}");
            assert_eq!(evaluate_expression_goal(&records, &goal, &BTreeMap::new(), 0), None);
            assert!(!mutate_expression_goal(&mut records, &goal, true, &BTreeMap::new(), 0));
            assert_eq!(records[0].value, "0");
        }
    }

    #[test]
    fn compiler_member_wrappers_preserve_scope_and_written_input_rejection() {
        let records = parse_structured_records("X,DINT,0\n");
        let nested = compiler_member_variable("P.INSTANCE.X", "DINT");
        assert_eq!(expression_variable(&nested).as_deref(), Some("P.INSTANCE.X"));
        assert_eq!(expression_record_index(&records, &nested), None);
        let definitions = BTreeMap::from([
            ("P.INSTANCE.X".into(), integer("42")), ("X".into(), integer("99")),
        ]);
        assert_eq!(integer_expression_wide(&records, &nested, &definitions, 0), Some(42));
        assert_eq!(integer_expression_wide(&records,
            &compiler_member_variable("Q.INSTANCE.X", "DINT"), &definitions, 0), None);

        for ty in ["DINT", "ULINT", "REAL", "LREAL", "BOOL"] {
            let input = compiler_member_variable("ALGORITHM_BRANCH.X", ty);
            for definition in [integer("42"), binary("+", input.clone(), integer("1")), serde_json::Value::Null] {
                let definitions = BTreeMap::from([("ALGORITHM_BRANCH.X".into(), definition)]);
                let mut records = parse_structured_records(&format!("X,{ty},0\n"));
                let goal = if ty == "BOOL" { input.clone() } else { binary("=", input.clone(), integer("42")) };
                assert!(seed_variable_was_written(&input, &definitions));
                assert_eq!(integer_expression_wide(&records, &input, &definitions, 0), None);
                assert_eq!(expression_number_value(&records, &input, &definitions, 0), None);
                assert_eq!(evaluate_expression_goal(&records, &goal, &definitions, 0), None);
                assert!(!mutate_expression_goal(&mut records, &goal, true, &definitions, 0));
                assert!(!mutate_expression_to_value(&mut records, &input, 1, &definitions, 0));
                assert_eq!(records[0].value, "0");
            }
        }
    }

    #[test]
    fn strict_integer_goals_cross_the_actual_discrete_boundary() {
        for (operator, expected) in [(">", "11"), ("<", "9"), ("<>", "9")] {
            let mut records = parse_structured_records("X,INT,10\n");
            let goal = binary(operator, variable("P.X"), integer("10"));
            assert!(mutate_expression_goal(&mut records, &goal, true, &BTreeMap::new(), 0));
            assert_eq!(records[0].value, expected);
        }
        let mut records = parse_structured_records("X,INT,-10\n");
        let reversed = binary("<", integer("-10"), variable("P.X"));
        assert!(mutate_expression_goal(&mut records, &reversed, true, &BTreeMap::new(), 0));
        assert_eq!(records[0].value, "-9");
    }

    #[test]
    fn integer_limits_are_exact_and_unsatisfiable_goals_fall_back() {
        for (ty, initial, op, fixed, expected) in [
            ("ULINT", "0", ">", "18446744073709551614", Some("18446744073709551615")),
            ("LINT", "0", "<", "-9223372036854775807", Some("-9223372036854775808")),
            ("SINT", "0", ">", "127", None),
            ("USINT", "1", "<", "0", None),
            ("LINT", "0", "<", "-9223372036854775808", None),
            ("ULINT", "0", ">", "18446744073709551615", None),
        ] {
            let mut records = parse_structured_records(&format!("X,{ty},{initial}\n"));
            let goal = binary(op, variable("P.X"), integer(fixed));
            assert_eq!(mutate_expression_goal(&mut records, &goal, true, &BTreeMap::new(), 0), expected.is_some(), "{ty} {op} {fixed}");
            assert_eq!(records[0].value, expected.unwrap_or(initial));
        }
        let mut records = parse_structured_records("X,ULINT,18446744073709551615\n");
        let equal = binary("=", variable("P.X"), integer("18446744073709551615"));
        assert!(!mutate_expression_goal(&mut records, &equal, true, &BTreeMap::new(), 0));
    }

    #[test]
    fn float_goals_survive_real_and_lreal_roundtrip() {
        for ty in ["REAL", "LREAL"] {
            for op in [">", "<", "<>"] {
                let mut records = parse_structured_records(&format!("X,{ty},10\n"));
                let goal = binary(op, variable("P.X"), serde_json::json!({
                    "kind": {"kind": "literal", "value": {"kind": "real", "value": "10.0"}}, "type_name": ty
                }));
                assert!(mutate_expression_goal(&mut records, &goal, true, &BTreeMap::new(), 0));
                let actual = if ty == "REAL" { records[0].value.parse::<f32>().unwrap() as f64 }
                    else { records[0].value.parse::<f64>().unwrap() };
                assert!(comparison_holds(op, actual, 10.0).unwrap());
                assert_eq!(actual, if ty == "REAL" {
                    if op == ">" { 10_f32.next_up() as f64 } else { 10_f32.next_down() as f64 }
                } else if op == ">" { 10_f64.next_up() } else { 10_f64.next_down() });
            }
        }
        let mut record = StRecord { name: "X".into(), ty: "REAL".into(), value: "0".into() };
        assert!(!mutate_float_comparison(&mut record, ">", f32::MAX as f64, true, true));
        assert_eq!(record.value, "0");
    }

    #[test]
    fn float_boundaries_skip_64_bit_integer_and_time_fields() {
        let field = |name: &str, ty: &str, kind: &str| -> ControllableSeedField {
            serde_json::from_value(serde_json::json!({
                "symbol": format!("P.{name}"), "seed_field": name, "type_name": ty,
                "type": {"name": ty, "kind": kind, "bit_width": 64},
            }))
            .unwrap()
        };
        let fields = [field("T", "TIME", "integer"), field("N", "LINT", "integer")];
        let mut records = parse_structured_records("T,TIME,0\nN,LINT,0\n");
        for (record, field) in records.iter().zip(&fields) {
            assert_eq!(semantic_float_type_name(record, Some(field)), None);
        }
        let mut rand = StdRand::with_seed(7);
        assert!(!mutate_float_boundary_group(&mut rand, &mut records, &fields));
        assert_eq!((records[0].value.as_str(), records[1].value.as_str()), ("0", "0"));

        let alias = field("Y", "MY_LREAL", "float");
        let record = StRecord { name: "Y".into(), ty: "MY_LREAL".into(), value: "0".into() };
        assert_eq!(semantic_float_type_name(&record, Some(&alias)), Some("LREAL"));
    }

    #[test]
    fn compound_reads_are_not_a_variable_and_definitions_keep_scope() {
        let records = parse_structured_records("X,INT,10\n");
        let arithmetic = binary("+", variable("P.X"), integer("2"));
        assert!(expression_variable(&arithmetic).is_none());
        assert_eq!(integer_expression_wide(&records, &arithmetic, &BTreeMap::new(), 0), Some(12));
        let definitions = BTreeMap::from([
            ("P.TMP".into(), arithmetic), ("Q.TMP".into(), integer("50")),
            ("TMP".into(), integer("99")),
        ]);
        assert_eq!(integer_expression_wide(&records, &variable("P.TMP"), &definitions, 0), Some(12));
        assert_eq!(integer_expression_wide(&records, &variable("Q.TMP"), &definitions, 0), Some(50));
        assert_eq!(integer_expression_wide(&records, &variable("P.INSTANCE.TMP"), &definitions, 0), None);
        assert_eq!(expression_record_index(&records, &variable("P.INSTANCE.X")), None);
        let opaque = serde_json::json!({"kind": {"kind": "call"}, "reads": ["P.X"], "operands": [variable("P.X")]});
        assert_eq!(integer_expression_wide(&records, &opaque, &definitions, 0), None);
    }

    #[test]
    fn conjunction_uses_wide_integers_and_rejects_contradictions() {
        let defs = BTreeMap::new();
        let mut records = parse_structured_records("X,ULINT,0\n");
        let goal = binary("AND",
            binary(">", variable("P.X"), integer("18446744073709551613")),
            binary("<", variable("P.X"), integer("18446744073709551615")));
        assert!(mutate_expression_goal(&mut records, &goal, true, &defs, 0));
        assert_eq!(records[0].value, "18446744073709551614");
        for ty in ["INT", "REAL"] {
            let mut records = parse_structured_records(&format!("X,{ty},2\n"));
            let impossible = binary("AND", binary(">", variable("P.X"), integer("10")), binary("<", variable("P.X"), integer("0")));
            assert!(!mutate_expression_goal(&mut records, &impossible, true, &defs, 0));
            assert_eq!(records[0].value, "2");
        }
        let mut records = parse_structured_records("X,INT,2\n");
        let impossible = binary("<", variable("P.X"), variable("P.X"));
        assert!(!mutate_expression_goal(&mut records, &impossible, true, &defs, 0));
        assert_eq!(records[0].value, "2");
    }

    #[test]
    fn inverse_arithmetic_does_not_claim_success_for_aliasing_operands() {
        let mut records = parse_structured_records("X,INT,2\n");
        let expression = binary("+", variable("P.X"), variable("P.X"));
        assert!(!mutate_expression_to_value(&mut records, &expression, 0, &BTreeMap::new(), 0));
        assert_eq!(records[0].value, "2");
        let definitions = BTreeMap::from([("P.DEN".into(), binary("+", variable("P.X"), integer("1")))]);
        assert!(mutate_expression_to_value(&mut records, &variable("P.DEN"), 0, &definitions, 0));
        assert_eq!(records[0].value, "-1");
    }

    #[test]
    fn assigned_seed_fields_are_not_treated_as_current_program_values() {
        for ty in ["INT", "ULINT", "REAL", "LREAL", "BOOL"] {
            for definition in [binary("+", variable("P.X"), integer("1")), serde_json::Value::Null] {
                let mut records = parse_structured_records(&format!("X,{ty},0\n"));
                let definitions = BTreeMap::from([("P.X".into(), definition)]);
                let goal = if ty == "BOOL" { variable("P.X") }
                    else { binary("=", variable("P.X"), integer("10")) };
                assert_eq!(integer_expression_wide(&records, &variable("P.X"), &definitions, 0), None);
                assert_eq!(expression_number_value(&records, &variable("P.X"), &definitions, 0), None);
                assert_eq!(evaluate_expression_goal(&records, &goal, &definitions, 0), None);
                assert!(!mutate_expression_goal(&mut records, &goal, true, &definitions, 0));
                assert!(!mutate_expression_to_value(&mut records, &variable("P.X"), 1, &definitions, 0));
                assert_eq!(records[0].value, "0");
            }
        }
        // A written bound is equally unsafe, even when the mutated side was
        // not assigned. No fake equality may be inferred from its seed value.
        let mut records = parse_structured_records("X,INT,5\nY,INT,0\n");
        let definitions = BTreeMap::from([("P.Y".into(), integer("10"))]);
        let goal = binary("=", variable("P.X"), variable("P.Y"));
        assert!(!mutate_expression_goal(&mut records, &goal, true, &definitions, 0));
        assert_eq!(serialize_records(&records), "X,INT,5\nY,INT,0\n");
    }

    #[test]
    fn cycle_limit_configuration_is_bounded_without_global_env_races() {
        assert_eq!(parse_fb_max_cycles(None), 256);
        assert_eq!(parse_fb_max_cycles(Some("0")), 256);
        assert_eq!(parse_fb_max_cycles(Some("-1")), 256);
        assert_eq!(parse_fb_max_cycles(Some("bad")), 256);
        assert_eq!(parse_fb_max_cycles(Some("100")), 100);
        assert_eq!(parse_fb_max_cycles(Some("100000")), HARD_FB_MAX_CYCLES);
    }

    #[test]
    fn short_prefix_grows_past_64_and_preserves_terminal_input() {
        let mut rand = StdRand::with_seed(37);
        let limit = fb_max_cycles();
        if limit < 2 {
            let mut seed = parse_cycle_aware_seed("0.X,INT,9\n");
            cycle_clone_cycle(&mut rand, &mut seed);
            cycle_insert_cycle(&mut rand, &mut seed);
            assert_eq!(seed.cycles.len(), 1);
            return;
        }
        let mut seed = parse_cycle_aware_seed("0.X,INT,1\n1.X,INT,9\n");
        while seed.cycles.len() < limit {
            assert!(expand_state_prefix(&mut rand, &mut seed));
        }
        assert_eq!(seed.cycles.len(), limit);
        assert_eq!(seed.cycles.values().next_back().unwrap()[0].value, "9");
        assert!(!expand_state_prefix(&mut rand, &mut seed));
        cycle_clone_cycle(&mut rand, &mut seed);
        cycle_insert_cycle(&mut rand, &mut seed);
        assert_eq!(seed.cycles.len(), limit);
        if env::var("SEMANTIST_FB_MAX_CYCLES").is_err() { assert!(limit > 64); }
    }
}
