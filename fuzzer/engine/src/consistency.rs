// Observations of one input for tests/integration/execution_consistency.py.
// Every execution mode must report the same record for the same input; the
// harness writes the target's values to its own trace separately.
fn consistency_record(
    index: usize,
    input: &Path,
    exit_kind: &ExitKind,
    edges: &[u8],
    semantic: &[u8],
    telemetry: &[u8],
) -> serde_json::Value {
    fn nonzero(map: &[u8]) -> Vec<[u64; 2]> {
        map.iter()
            .enumerate()
            .filter(|(_, value)| **value != 0)
            .map(|(index, value)| [index as u64, u64::from(*value)])
            .collect()
    }
    let word = |offset: usize| {
        telemetry
            .get(offset..offset + 4)
            .map_or(0, |bytes| u32::from_ne_bytes(bytes.try_into().unwrap()))
    };
    let wide = |offset: usize| {
        telemetry
            .get(offset..offset + 8)
            .map_or(0, |bytes| u64::from_ne_bytes(bytes.try_into().unwrap()))
    };
    let event_count = word(4) as usize;
    let state_count = word(8) as usize;
    let events = (0..event_count.min(TELEMETRY_EVENT_CAPACITY))
        .map(|event| [word(16 + event * 8), word(20 + event * 8)])
        .collect::<Vec<_>>();
    let state_base = 16 + TELEMETRY_EVENT_CAPACITY * 8;
    let states = (0..state_count.min(TELEMETRY_STATE_CAPACITY))
        .map(|state| [wide(state_base + state * 16), wide(state_base + state * 16 + 8)])
        .collect::<Vec<_>>();
    serde_json::json!({
        "index": index,
        "input": input.display().to_string(),
        "exit_kind": exit_kind_label(exit_kind),
        "edges": nonzero(edges),
        "semantic": nonzero(semantic),
        "telemetry": {
            "scan_count": word(0),
            "event_count": event_count,
            "state_count": state_count,
            "flags": word(12),
            "events": events,
            "states": states,
        },
    })
}

fn consistency_input_paths(list: &Path) -> Result<Vec<PathBuf>, Error> {
    Ok(fs::read_to_string(list)?
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(PathBuf::from)
        .collect())
}

#[cfg(test)]
mod consistency_record_tests {
    use super::*;

    #[test]
    fn record_reads_only_the_filled_telemetry_prefix() {
        let mut telemetry = vec![0u8; TELEMETRY_SIZE];
        telemetry[0..4].copy_from_slice(&2u32.to_ne_bytes());
        telemetry[4..8].copy_from_slice(&1u32.to_ne_bytes());
        telemetry[8..12].copy_from_slice(&1u32.to_ne_bytes());
        telemetry[12..16].copy_from_slice(&4u32.to_ne_bytes());
        telemetry[16..20].copy_from_slice(&7u32.to_ne_bytes());
        telemetry[20..24].copy_from_slice(&1u32.to_ne_bytes());
        let state_base = 16 + TELEMETRY_EVENT_CAPACITY * 8;
        telemetry[state_base..state_base + 8].copy_from_slice(&5u64.to_ne_bytes());
        telemetry[state_base + 8..state_base + 16].copy_from_slice(&6u64.to_ne_bytes());
        let record = consistency_record(3, Path::new("a"), &ExitKind::Ok, &[0, 2, 0], &[1], &telemetry);
        assert_eq!(record["edges"], serde_json::json!([[1, 2]]));
        assert_eq!(record["semantic"], serde_json::json!([[0, 1]]));
        assert_eq!(record["telemetry"]["events"], serde_json::json!([[7, 1]]));
        assert_eq!(record["telemetry"]["states"], serde_json::json!([[5, 6]]));
        assert_eq!(record["telemetry"]["scan_count"], 2);
        assert_eq!(record["exit_kind"], "ok");
    }
}
