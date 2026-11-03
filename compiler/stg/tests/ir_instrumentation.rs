use std::fs;

use semantist_stg::ir::{instrument, InstrumentOptions};
use serde_json::json;

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
fn typed_risk_probes_use_unsigned_arithmetic_float_range_and_each_array_dimension() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("input.ll");
    let output = directory.path().join("output.ll");
    let runtime_ids = directory.path().join("runtime-ids.json");
    let events = vec![
        json!({
            "runtime_id": 0, "stable_id": "target:00000000000000000000000000000000",
            "semantic_edge_ids": [], "kind": "hazard_violation",
            "hazard": "arithmetic_boundary", "modeling": "exact",
            "violation_predicate": {"kind": "integer_overflow", "operator": "-",
                                    "signed": false, "bit_width": 32}
        }),
        json!({
            "runtime_id": 1, "stable_id": "target:00000000000000000000000000000001",
            "semantic_edge_ids": [], "kind": "hazard_violation",
            "hazard": "dangerous_conversion", "modeling": "conservative",
            "violation_predicate": {"kind": "float_to_integer",
                                    "source_bit_width": 32, "target_bit_width": 32,
                                    "signed": true}
        }),
        json!({
            "runtime_id": 2, "stable_id": "target:00000000000000000000000000000002",
            "semantic_edge_ids": [], "kind": "hazard_violation",
            "hazard": "array_access", "modeling": "exact",
            "violation_predicate": {"kind": "array_bounds", "dimensions": [
                {"lower": 1, "upper": 2}, {"lower": 0, "upper": 3}
            ]}
        }),
    ];
    fs::write(&input, format!(r#"source_filename = "typed-risks"
@items = global [2 x [4 x i32]] zeroinitializer
define i32 @SEMANTIC_TEST(i32 %x, float %r, i64 %row, i64 %col) {{
entry:
  %difference = sub i32 %x, 3, !semantist.semantic !0
  %converted = fptosi float %r to i32, !semantist.semantic !1
  %address = getelementptr [2 x [4 x i32]], ptr @items, i64 0, i64 %row, i64 %col, !semantist.semantic !2
  %value = load i32, ptr %address
  %sum = add i32 %difference, %converted
  ret i32 %sum
}}
!0 = !{{!"{}"}}
!1 = !{{!"{}"}}
!2 = !{{!"{}"}}
"#, payload(vec![events[0].clone()]), payload(vec![events[1].clone()]),
        payload(vec![events[2].clone()]))).unwrap();
    fs::write(&runtime_ids, serde_json::to_vec(&json!({
        "schema_version": "semantist.runtime-ids/1.0.0",
        "stg_schema_version": "semantist.stg/1.2.0",
        "entries": events.iter().enumerate().map(|(index, event)| json!({
            "runtime_id": index, "stable_id": event["stable_id"],
            "pou": "SEMANTIC_TEST", "node_id": format!("node:{index:032x}"),
            "semantic_edge_ids": [], "kind": "hazard_violation",
            "hazard": event["hazard"], "source": null,
            "modeling": event["modeling"],
            "violation_predicate": event["violation_predicate"]
        })).collect::<Vec<_>>()
    })).unwrap()).unwrap();
    let artifact = instrument(&InstrumentOptions {
        input_ir: input, output_ir: output.clone(), runtime_ids,
        mapping_output: directory.path().join("mapping.json"),
        diagnostics_output: None,
    }).unwrap();
    assert_eq!(artifact.statistics.hazard_violation_count, 3);
    let ir = fs::read_to_string(&output).unwrap();
    assert!(ir.contains("llvm.usub.with.overflow"));
    assert!(ir.contains("stg.float_to_int.invalid"));
    assert!(ir.contains("fpext float"));
    assert!(ir.contains("fcmp ole double"));
    assert_eq!(ir.lines().filter(|line|
        line.trim_start().starts_with("%stg.array.dimension_oob")
            && line.contains("= or i1")).count(), 2);
    let driver = directory.path().join("driver.c");
    let binary = directory.path().join("probe-check");
    fs::write(&driver, r#"
#include <math.h>
#include <stdbool.h>
static int hits[3];
void __semantist_semantic_hit(int id) { (void)id; }
void __semantist_semantic_hazard(int id, bool bad) {
    if (bad && id >= 0 && id < 3) hits[id]++;
}
extern int SEMANTIC_TEST(int, float, long, long);
int main(void) {
    if (SEMANTIC_TEST(4, -0.5f, 0, 0) != 1) return 1;
    if (hits[0] || hits[1] || hits[2]) return 2;
    (void)SEMANTIC_TEST(0, 0.0f, 0, 0);
    if (!hits[0] || hits[1] || hits[2]) return 3;
    (void)SEMANTIC_TEST(4, NAN, 0, 0);
    if (!hits[1] || hits[2]) return 4;
    return 0;
}
"#).unwrap();
    if std::process::Command::new("clang").arg("--version").output().is_ok() {
        let compile = std::process::Command::new("clang")
            .args([output.as_os_str(), driver.as_os_str()])
            .arg("-o").arg(&binary).output().unwrap();
        assert!(compile.status.success(), "{}", String::from_utf8_lossy(&compile.stderr));
        let execution = std::process::Command::new(binary).output().unwrap();
        assert!(execution.status.success(), "safe/unsafe probe check exited {:?}", execution.status);
    }
}

#[test]
fn flattened_multidimensional_access_checks_each_original_dimension() {
    let directory = tempfile::tempdir().unwrap();
    let input = directory.path().join("flat.ll");
    let output = directory.path().join("instrumented.ll");
    let runtime_ids = directory.path().join("ids.json");
    let risk = json!({
        "runtime_id": 0, "stable_id": "target:00000000000000000000000000000000",
        "semantic_edge_ids": [], "kind": "hazard_violation", "hazard": "array_access",
        "modeling": "exact", "violation_predicate": {"kind": "array_bounds", "dimensions": [
            {"lower": 1, "upper": 3}, {"lower": 0, "upper": 15}
        ]}
    });
    fs::write(&input, format!(r#"source_filename = "flat"
@items = global [48 x [4 x i8]] zeroinitializer
define i8 @SEMANTIC_TEST(i16 %row, i16 %col) {{
entry:
  %adjusted = sub i16 %row, 1
  %wide_row = sext i16 %adjusted to i32
  %row_base = mul i32 16, %wide_row
  %wide_col = sext i16 %col to i32
  %flat = add i32 %row_base, %wide_col
  %address = getelementptr [48 x [4 x i8]], ptr @items, i32 0, i32 %flat, !semantist.semantic !0
  %value = load i8, ptr %address
  ret i8 %value
}}
!0 = !{{!"{}"}}
"#, payload(vec![risk.clone()]))).unwrap();
    fs::write(&runtime_ids, serde_json::to_vec(&json!({
        "schema_version": "semantist.runtime-ids/1.0.0",
        "stg_schema_version": "semantist.stg/1.2.0",
        "entries": [{
            "runtime_id": 0, "stable_id": risk["stable_id"], "pou": "SEMANTIC_TEST",
            "node_id": "node:00000000000000000000000000000000",
            "semantic_edge_ids": [], "kind": "hazard_violation", "hazard": "array_access",
            "source": null, "modeling": "exact", "violation_predicate": risk["violation_predicate"]
        }]
    })).unwrap()).unwrap();
    let artifact = instrument(&InstrumentOptions {
        input_ir: input, output_ir: output.clone(), runtime_ids,
        mapping_output: directory.path().join("mapping.json"), diagnostics_output: None,
    }).unwrap();
    assert_eq!(artifact.statistics.hazard_violation_count, 1);
    let ir = fs::read_to_string(output).unwrap();
    assert_eq!(ir.lines().filter(|line|
        line.trim_start().starts_with("%stg.array.dimension_oob")
            && line.contains("= or i1")).count(), 2);
}

#[test]
fn unanchored_float_conversion_requires_a_unique_matching_call() {
    for call_count in [1, 2] {
        let directory = tempfile::tempdir().unwrap();
        let input = directory.path().join("input.ll");
        let output = directory.path().join("output.ll");
        let runtime_ids = directory.path().join("ids.json");
        let calls = (0..call_count).map(|index|
            format!("  %call{index} = call i32 @GENERIC_CAST(float %x)\n"))
            .collect::<String>();
        fs::write(&input, format!(
            "declare i32 @GENERIC_CAST(float)\n\
             define i32 @CONVERT_VARIANT(float %x) {{\nentry:\n{calls}  ret i32 %call0\n}}\n"
        )).unwrap();
        let entries = ["hazard_reach", "hazard_violation"].iter().enumerate()
            .map(|(runtime_id, kind)| json!({
                "runtime_id": runtime_id, "stable_id": format!("target:{runtime_id:032x}"),
                "pou": "CONVERT_VARIANT", "node_id": "node:00000000000000000000000000000000",
                "semantic_edge_ids": [], "kind": kind, "role": null,
                "hazard": "dangerous_conversion", "source_callee": "GENERIC_CAST",
                "source": null, "modeling": "conservative",
                "violation_predicate": {"kind": "float_to_integer", "source_bit_width": 32,
                                        "target_bit_width": 32, "signed": true},
            })).collect::<Vec<_>>();
        fs::write(&runtime_ids, serde_json::to_vec(&json!({
            "schema_version": "semantist.runtime-ids/1.0.0",
            "stg_schema_version": "semantist.stg/1.2.0", "entries": entries,
        })).unwrap()).unwrap();
        let artifact = instrument(&InstrumentOptions {
            input_ir: input, output_ir: output.clone(), runtime_ids,
            mapping_output: directory.path().join("mapping.json"),
            diagnostics_output: None,
        }).unwrap();
        let ir = fs::read_to_string(output).unwrap();
        if call_count == 1 {
            assert_eq!(artifact.statistics.hazard_violation_count, 1);
            assert!(ir.contains("stg.float_to_int.invalid"));
        } else {
            assert_eq!(artifact.statistics.hazard_violation_count, 0);
            assert!(artifact.diagnostics.iter().any(|entry| entry.code == "STG-IR-W007"));
        }
    }
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
