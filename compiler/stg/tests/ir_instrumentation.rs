use std::fs;

use serde_json::json;
use semantist_stg::ir::{instrument, InstrumentOptions};

fn llvm_metadata_string(value: &serde_json::Value) -> String {
    serde_json::to_string(value)
        .unwrap()
        .bytes()
        .map(|byte| match byte {
            b'"' => "\\22".to_string(),
            b'\\' => "\\5C".to_string(),
            0x20..=0x7e => (byte as char).to_string(),
            _ => format!("\\{byte:02X}"),
        })
        .collect()
}

fn event(
    runtime_id: u32,
    kind: &str,
    role: Option<&str>,
    successor_hint: Option<u32>,
) -> serde_json::Value {
    json!({
        "runtime_id": runtime_id,
        "stable_id": format!("target:{runtime_id:032x}"),
        "semantic_edge_ids": [format!("edge:{runtime_id:032x}")],
        "kind": kind,
        "role": role,
        "hazard": if kind.starts_with("hazard_") { Some("division") } else { None },
        "successor_hint": successor_hint,
        "modeling": "exact"
    })
}

fn payload(events: Vec<serde_json::Value>) -> String {
    llvm_metadata_string(&json!({
        "schema_version": "semantist.llvm-semantic/1.0.0",
        "pou": "SEMANTIC_TEST",
        "lowered_ast_id": 1,
        "events": events
    }))
}

fn narrowing_event(runtime_id: u32, source_constant: Option<&str>) -> serde_json::Value {
    json!({
        "runtime_id": runtime_id,
        "stable_id": format!("target:{runtime_id:032x}"),
        "semantic_edge_ids": [format!("edge:{runtime_id:032x}")],
        "kind": "hazard_violation",
        "role": null,
        "hazard": "dangerous_conversion",
        "violation_predicate": {
            "kind": "narrowing",
            "source_bit_width": 32,
            "target_bit_width": 8,
            "signed": true,
            "source_signed": true,
            "target_signed": false,
            "source_constant": source_constant
        },
        "successor_hint": null,
        "modeling": "exact"
    })
}

#[test]
fn splits_each_semantic_cfg_edge_and_instruments_hazards() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("input.ll");
    let output = directory.path().join("output.ll");
    let runtime_ids = directory.path().join("runtime-ids.json");
    let mapping = directory.path().join("mapping.json");
    let diagnostics = directory.path().join("diagnostics.json");

    let branch = payload(vec![
        event(0, "branch_outcome", Some("true"), Some(0)),
        event(1, "branch_outcome", Some("false"), Some(1)),
    ]);
    let switch = payload(vec![
        event(2, "case_outcome", Some("case_default"), Some(0)),
        event(3, "case_outcome", Some("case_label"), Some(1)),
        event(4, "case_outcome", Some("case_label"), Some(2)),
    ]);
    let hazard = payload(vec![
        event(5, "hazard_reach", None, None),
        event(6, "hazard_violation", None, None),
    ]);
    let terminal = payload(vec![event(7, "control_transfer", Some("return"), None)]);
    fs::write(
        &input,
        format!(
            r#"source_filename = "semantic-test"

define i32 @SEMANTIC_TEST(i32 %x, i32 %d) {{
entry:
  %condition = icmp slt i32 %x, 0
  br i1 %condition, label %join, label %join, !semantist.semantic !0

join:
  switch i32 %x, label %arithmetic [
    i32 1, label %arithmetic
    i32 2, label %arithmetic
  ], !semantist.semantic !1

arithmetic:
  %quotient = sdiv i32 %x, %d, !semantist.semantic !2
  ret i32 %quotient, !semantist.semantic !3
}}

!0 = !{{!"{branch}"}}
!1 = !{{!"{switch}"}}
!2 = !{{!"{hazard}"}}
!3 = !{{!"{terminal}"}}
"#
        ),
    )
    .unwrap();

    let entries = (0..8)
        .map(|runtime_id| {
            json!({
                "runtime_id": runtime_id,
                "stable_id": format!("target:{runtime_id:032x}"),
                "pou": "SEMANTIC_TEST",
                "node_id": format!("node:{runtime_id:032x}"),
                "semantic_edge_ids": [format!("edge:{runtime_id:032x}")],
                "kind": match runtime_id {
                    0 | 1 => "branch_outcome",
                    2..=4 => "case_outcome",
                    5 => "hazard_reach",
                    6 => "hazard_violation",
                    _ => "control_transfer",
                },
                "role": match runtime_id {
                    0 => Some("true"),
                    1 => Some("false"),
                    2 => Some("case_default"),
                    3 | 4 => Some("case_label"),
                    7 => Some("return"),
                    _ => None,
                },
                "hazard": if matches!(runtime_id, 5 | 6) { Some("division") } else { None },
                "source": null,
                "modeling": "exact"
            })
        })
        .collect::<Vec<_>>();
    fs::write(
        &runtime_ids,
        serde_json::to_vec_pretty(&json!({
            "schema_version": "semantist.runtime-ids/1.0.0",
            "stg_schema_version": "semantist.stg/1.2.0",
            "entries": entries
        }))
        .unwrap(),
    )
    .unwrap();

    let artifact = instrument(&InstrumentOptions {
        input_ir: input.clone(),
        output_ir: output.clone(),
        runtime_ids: runtime_ids.clone(),
        mapping_output: mapping,
        diagnostics_output: Some(diagnostics),
    })
    .unwrap();

    assert_eq!(artifact.statistics.target_count, 8);
    assert_eq!(artifact.statistics.mapped_target_count, 8);
    assert_eq!(artifact.statistics.unmapped_target_count, 0);
    assert_eq!(artifact.statistics.cfg_edge_mapping_count, 5);
    assert_eq!(artifact.statistics.probe_block_count, 5);
    assert_eq!(artifact.statistics.hazard_reach_count, 1);
    assert_eq!(artifact.statistics.hazard_violation_count, 1);
    assert_eq!(artifact.statistics.terminal_mapping_count, 1);

    let ir = fs::read_to_string(&output).unwrap();
    for runtime_id in 0..8 {
        assert!(
            ir.contains(&format!("i32 {runtime_id}")),
            "missing runtime ID {runtime_id}"
        );
    }
    assert!(ir.contains("stg.div.zero"));
    assert!(ir.contains("stg.div.overflow"));
    assert_eq!(
        artifact
            .mappings
            .iter()
            .filter(|entry| matches!(entry.runtime_id, 3 | 4))
            .map(|entry| entry.probe_block.clone().unwrap())
            .collect::<std::collections::BTreeSet<_>>()
            .len(),
        2,
        "CASE labels sharing one destination still need distinct probe blocks"
    );

    let second_output = directory.path().join("output-second.ll");
    instrument(&InstrumentOptions {
        input_ir: input,
        output_ir: second_output.clone(),
        runtime_ids,
        mapping_output: directory.path().join("mapping-second.json"),
        diagnostics_output: Some(directory.path().join("diagnostics-second.json")),
    })
    .unwrap();
    assert_eq!(
        fs::read(output).unwrap(),
        fs::read(second_output).unwrap(),
        "semantic IR instrumentation must be byte deterministic"
    );
}

#[test]
fn instruments_folded_and_dynamic_implicit_narrowing_exactly() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("input.ll");
    let output = directory.path().join("output.ll");
    let runtime_ids = directory.path().join("runtime-ids.json");
    let mapping = directory.path().join("mapping.json");
    let diagnostics = directory.path().join("diagnostics.json");

    let out_of_range = payload(vec![narrowing_event(0, Some("300"))]);
    let in_range = payload(vec![narrowing_event(1, Some("112"))]);
    let dynamic = payload(vec![narrowing_event(2, None)]);
    fs::write(
        &input,
        format!(
            r#"source_filename = "narrowing-test"

define void @NARROWING_TEST(i32 %dynamic) {{
entry:
  %out = alloca i8, align 1
  store i8 44, ptr %out, align 1, !semantist.semantic !0
  store i8 112, ptr %out, align 1, !semantist.semantic !1
  %narrowed = trunc i32 %dynamic to i8, !semantist.semantic !2
  store i8 %narrowed, ptr %out, align 1
  ret void
}}

!0 = !{{!"{out_of_range}"}}
!1 = !{{!"{in_range}"}}
!2 = !{{!"{dynamic}"}}
"#
        ),
    )
    .unwrap();

    let entries = (0..3)
        .map(|runtime_id| {
            json!({
                "runtime_id": runtime_id,
                "stable_id": format!("target:{runtime_id:032x}"),
                "pou": "NARROWING_TEST",
                "node_id": format!("node:{runtime_id:032x}"),
                "semantic_edge_ids": [format!("edge:{runtime_id:032x}")],
                "kind": "hazard_violation",
                "role": null,
                "hazard": "dangerous_conversion",
                "source": null,
                "modeling": "exact"
            })
        })
        .collect::<Vec<_>>();
    fs::write(
        &runtime_ids,
        serde_json::to_vec_pretty(&json!({
            "schema_version": "semantist.runtime-ids/1.0.0",
            "stg_schema_version": "semantist.stg/1.2.0",
            "entries": entries
        }))
        .unwrap(),
    )
    .unwrap();

    let artifact = instrument(&InstrumentOptions {
        input_ir: input,
        output_ir: output.clone(),
        runtime_ids,
        mapping_output: mapping,
        diagnostics_output: Some(diagnostics),
    })
    .unwrap();

    assert_eq!(artifact.statistics.target_count, 3);
    assert_eq!(artifact.statistics.mapped_target_count, 3);
    assert_eq!(artifact.statistics.unmapped_target_count, 0);
    assert_eq!(artifact.statistics.hazard_violation_count, 3);

    let ir = fs::read_to_string(output).unwrap();
    assert!(
        ir.contains("@__semantist_semantic_hazard(i32 0, i1 true)"),
        "300 does not fit in BYTE and must report a violation"
    );
    assert!(
        ir.contains("@__semantist_semantic_hazard(i32 1, i1 false)"),
        "112 fits in BYTE and must not report a violation"
    );
    assert!(
        ir.contains("zext i8 %narrowed to i32"),
        "DINT to unsigned BYTE must restore with zero extension"
    );
}
