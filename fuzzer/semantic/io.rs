fn append_runtime_finding_event(path: &Path, event: &RuntimeFindingEvent) -> Result<(), Error> {
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    serde_json::to_writer(&mut file, event).map_err(|error| {
        Error::serialize(format!(
            "unable to serialize runtime finding event: {error}"
        ))
    })?;
    file.write_all(b"\n")?;
    Ok(())
}

fn append_json_value(path: &Path, value: &serde_json::Value) -> Result<(), Error> {
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    serde_json::to_writer(&mut file, value).map_err(|error| {
        Error::serialize(format!(
            "unable to serialize semantic coverage event: {error}"
        ))
    })?;
    file.write_all(b"\n")?;
    Ok(())
}

fn load_runtime_id_table(path: Option<&Path>) -> Result<RuntimeIdTable, Error> {
    let Some(path) = path else {
        return Ok(RuntimeIdTable {
            schema_version: "semantist.runtime-ids/1.0.0".to_string(),
            entries: Vec::new(),
        });
    };
    let contents = fs::read_to_string(path)?;
    let table: RuntimeIdTable = serde_json::from_str(&contents).map_err(|error| {
        Error::serialize(format!(
            "unable to parse semantic runtime ID table {}: {error}",
            path.display()
        ))
    })?;
    if table.schema_version != "semantist.runtime-ids/1.0.0" {
        return Err(Error::illegal_argument(format!(
            "unsupported semantic runtime ID schema {}",
            table.schema_version
        )));
    }
    Ok(table)
}

fn load_mapped_semantic_targets(
    runtime_ids_path: Option<&Path>,
    table: &RuntimeIdTable,
) -> Result<HashSet<String>, Error> {
    let Some(mapping_path) =
        runtime_ids_path.map(|path| path.with_file_name("stg-ir-mapping.json"))
    else {
        return Ok(table
            .entries
            .iter()
            .map(|entry| entry.stable_id.clone())
            .collect());
    };
    if !mapping_path.exists() {
        return Ok(table
            .entries
            .iter()
            .map(|entry| entry.stable_id.clone())
            .collect());
    }
    let mapping: serde_json::Value = serde_json::from_str(&fs::read_to_string(&mapping_path)?)
        .map_err(|error| {
            Error::serialize(format!(
                "unable to parse semantic IR mapping {}: {error}",
                mapping_path.display()
            ))
        })?;
    Ok(mapping
        .get("mappings")
        .and_then(|mappings| mappings.as_array())
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.get("stable_id").and_then(|value| value.as_str()))
        .map(str::to_string)
        .collect())
}

fn read_runtime_cycle_trace_window(path: &Path, offset: u64) -> (u64, BTreeMap<u32, Vec<i32>>) {
    let (end_offset, text) = read_text_window(path, offset);
    let mut cycles = BTreeMap::<u32, Vec<i32>>::new();
    for line in text.lines() {
        let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        let Some(runtime_id) = value
            .get("runtime_id")
            .and_then(|value| value.as_u64())
            .and_then(|value| u32::try_from(value).ok())
        else {
            continue;
        };
        let Some(cycle) = value
            .get("cycle")
            .and_then(|value| value.as_i64())
            .and_then(|value| i32::try_from(value).ok())
        else {
            continue;
        };
        cycles.entry(runtime_id).or_default().push(cycle);
    }
    for values in cycles.values_mut() {
        values.sort_unstable();
        values.dedup();
    }
    (end_offset, cycles)
}

fn read_state_signature_window(path: &Path, offset: u64) -> (u64, Vec<String>) {
    let (end_offset, text) = read_text_window(path, offset);
    let mut signatures = Vec::new();
    let mut sequence = Vec::new();
    for line in text.lines() {
        let mut cycle = None;
        let mut state_hash_after = None;
        for item in line.split_whitespace() {
            let Some((key, value)) = item.split_once('=') else {
                continue;
            };
            match key {
                "cycle" => cycle = value.parse::<i32>().ok(),
                "state_hash_after" if !value.is_empty() => {
                    state_hash_after = Some(value.to_string())
                }
                _ => {}
            }
        }
        if let Some(hash) = state_hash_after {
            signatures.push(hash.clone());
            sequence.push(format!("{}:{hash}", cycle.unwrap_or(-1)));
        }
    }
    if !sequence.is_empty() {
        signatures.push(format!(
            "state-sequence:{}",
            content_hash(sequence.join("|").as_bytes())
        ));
    }
    signatures.sort();
    signatures.dedup();
    (end_offset, signatures)
}

fn read_text_window(path: &Path, offset: u64) -> (u64, String) {
    let end_offset = fs::metadata(path)
        .map(|metadata| metadata.len())
        .unwrap_or(offset);
    let Ok(bytes) = fs::read(path) else {
        return (end_offset, String::new());
    };
    let start = usize::try_from(offset)
        .unwrap_or(bytes.len())
        .min(bytes.len());
    let end = usize::try_from(end_offset)
        .unwrap_or(bytes.len())
        .min(bytes.len())
        .max(start);
    (
        end_offset,
        String::from_utf8_lossy(&bytes[start..end]).into_owned(),
    )
}
