use std::fs;
use std::path::PathBuf;

use semantist_stg::instrumentation::{RuntimeIdTable, ViolationPredicate};
use semantist_stg::model::{
    ConfigurationSource, EdgeKind, HazardKind, NodeKind, OutcomeRole, PouKind, PredicateKind,
    SCHEMA_VERSION,
};
use semantist_stg::{generate, GenerateOptions};

fn fixture() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/control_flow.st")
}

fn generate_fixture(output: PathBuf) -> semantist_stg::model::ProjectSemanticModel {
    let fixture = fixture();
    let root = fixture.parent().unwrap().to_path_buf();
    generate(&GenerateOptions {
        inputs: vec![fixture.file_name().unwrap().into()],
        project_root: root,
        output_dir: output.clone(),
        pou_filters: Vec::new(),
        pretty: true,
        emit_dot: true,
        emit_debug_sidecars: true,
    })
    .unwrap();
    serde_json::from_slice(&fs::read(output.join("stg-model.json")).unwrap()).unwrap()
}

fn generate_inline(source: &str) -> (tempfile::TempDir, RuntimeIdTable) {
    let directory = tempfile::tempdir().unwrap();
    fs::write(directory.path().join("variant.st"), source).unwrap();
    let output = directory.path().join("model");
    generate(&GenerateOptions {
        inputs: vec!["variant.st".into()],
        project_root: directory.path().to_path_buf(),
        output_dir: output.clone(),
        pou_filters: Vec::new(),
        pretty: true,
        emit_dot: false,
        emit_debug_sidecars: true,
    }).unwrap();
    let runtime_ids = serde_json::from_slice(
        &fs::read(output.join("stg-runtime-ids.json")).unwrap()).unwrap();
    (directory, runtime_ids)
}

#[test]
fn array_risk_in_condition_tracks_renames_bounds_and_struct_layout() {
    for (name, lower, upper, field_first) in [
        ("ARRAY_ALPHA", 0, 3, true),
        ("ARRAY_BETA", 2, 6, false),
    ] {
        let fields = if field_first {
            format!("DATA : ARRAY[{lower}..{upper}] OF DINT; PAD : DINT;")
        } else {
            format!("PAD : DINT; DATA : ARRAY[{lower}..{upper}] OF DINT;")
        };
        let source = format!(
            "TYPE LAYOUT : STRUCT {fields} END_STRUCT END_TYPE\n\
             FUNCTION {name} : DINT\nVAR_INPUT I : DINT; END_VAR\n\
             VAR S : LAYOUT; END_VAR\n\
             IF S.DATA[I] > 0 THEN {name} := 1; END_IF;\nEND_FUNCTION\n"
        );
        let (_directory, ids) = generate_inline(&source);
        let arrays = ids.entries.iter().filter(|entry|
            entry.pou == name && entry.kind == semantist_stg::model::TargetKind::HazardViolation
                && entry.hazard == Some(HazardKind::ArrayAccess)).collect::<Vec<_>>();
        assert_eq!(arrays.len(), 1, "{name}");
        assert!(matches!(arrays[0].violation_predicate.as_ref(),
            Some(ViolationPredicate::ArrayBounds { dimensions })
                if dimensions.len() == 1
                    && dimensions[0].lower == Some(lower)
                    && dimensions[0].upper == Some(upper)));
    }
}

#[test]
fn nested_stateful_condition_keeps_array_risk_target() {
    for (name, upper) in [("SCAN_ALPHA", 5), ("SCAN_BETA", 9)] {
        let source = format!(
            "FUNCTION_BLOCK {name}\n\
             VAR_INPUT START : BOOL; RESET : BOOL; TABLE : ARRAY[0..{upper}] OF DINT; END_VAR\n\
             VAR_OUTPUT POSITION : INT := -1; END_VAR\n\
             IF RESET THEN POSITION := -1;\n\
             ELSIF START THEN POSITION := 0;\n\
             ELSIF POSITION >= 0 THEN\n\
                 IF TABLE[POSITION] <= 0 THEN POSITION := POSITION + 1; END_IF;\n\
             END_IF;\nEND_FUNCTION_BLOCK\n"
        );
        let (_directory, ids) = generate_inline(&source);
        let risks = ids.entries.iter().filter(|entry| {
            entry.pou == name && entry.hazard == Some(HazardKind::ArrayAccess)
        }).collect::<Vec<_>>();
        assert_eq!(risks.len(), 2, "missing array reach/violation for {name}");
        assert!(risks.iter().any(|entry| matches!(entry.violation_predicate.as_ref(),
            Some(ViolationPredicate::ArrayBounds { dimensions }) if
                dimensions.len() == 1 && dimensions[0].lower == Some(0)
                    && dimensions[0].upper == Some(upper))));
    }
}

#[test]
fn time_array_in_nested_stateful_guard_keeps_array_risk_target() {
    for (name, upper) in [("TIMED_ALPHA", 5), ("TIMED_BETA", 9)] {
        let source = format!(
            "FUNCTION_BLOCK {name}\n\
             VAR_INPUT START : BOOL; RESET : BOOL; TABLE : ARRAY[0..{upper}] OF TIME; TICK : TIME; END_VAR\n\
             VAR_OUTPUT POSITION : INT := -1; END_VAR\n\
             VAR LAST : TIME; EDGE : BOOL; END_VAR\n\
             IF RESET THEN POSITION := -1;\n\
             ELSIF START AND NOT EDGE THEN POSITION := 0; LAST := TICK;\n\
             ELSIF POSITION >= 0 THEN\n\
                 IF (TICK - LAST) >= TABLE[POSITION] THEN\n\
                     POSITION := POSITION + 1; LAST := TICK;\n\
                 END_IF;\n\
             END_IF;\n\
             EDGE := START;\nEND_FUNCTION_BLOCK\n"
        );
        let (_directory, ids) = generate_inline(&source);
        let risks = ids.entries.iter().filter(|entry| {
            entry.pou == name && entry.hazard == Some(HazardKind::ArrayAccess)
        }).collect::<Vec<_>>();
        assert_eq!(risks.len(), 2, "missing array reach/violation for {name}");
        assert!(risks.iter().any(|entry| matches!(entry.violation_predicate.as_ref(),
            Some(ViolationPredicate::ArrayBounds { dimensions }) if
                dimensions.len() == 1 && dimensions[0].lower == Some(0)
                    && dimensions[0].upper == Some(upper))));
    }
}

#[test]
fn arithmetic_probe_uses_unsigned_target_type_for_literal_subtraction() {
    for (name, ty, width) in [("SUB_DWORD", "DWORD", 32), ("SUB_WORD", "WORD", 16)] {
        let source = format!(
            "FUNCTION {name} : {ty}\nVAR_INPUT VALUE : {ty}; END_VAR\n\
             {name} := VALUE - 3;\nEND_FUNCTION\n"
        );
        let (_directory, ids) = generate_inline(&source);
        assert!(ids.entries.iter().any(|entry|
            entry.pou == name && entry.kind == semantist_stg::model::TargetKind::HazardViolation
                && matches!(entry.violation_predicate.as_ref(),
                    Some(ViolationPredicate::IntegerOverflow {
                        operator, signed: Some(false), bit_width: Some(bits)
                    }) if operator == "-" && *bits == width)));
    }
}

#[test]
fn float_to_integer_call_has_its_own_range_predicate() {
    let source = "FUNCTION REAL_TO_DINT : DINT\n\
                  VAR_INPUT IN : REAL; END_VAR\n\
                  REAL_TO_DINT := 0;\nEND_FUNCTION\n\
                  FUNCTION CONVERT_VARIANT : DINT\n\
                  VAR_INPUT R : REAL; END_VAR\n\
                  CONVERT_VARIANT := REAL_TO_DINT(R);\nEND_FUNCTION\n";
    let (_directory, ids) = generate_inline(source);
    assert!(ids.entries.iter().any(|entry|
        entry.kind == semantist_stg::model::TargetKind::HazardViolation
            && entry.hazard == Some(HazardKind::DangerousConversion)
            && matches!(entry.violation_predicate.as_ref(),
                Some(ViolationPredicate::FloatToInteger {
                    source_bit_width: Some(32), target_bit_width: Some(32),
                    signed: Some(true),
                }))));
}

#[test]
fn nested_float_to_integer_call_remains_a_risk_target() {
    let source = "FUNCTION REAL_TO_DINT : DINT\n\
                  VAR_INPUT IN : REAL; END_VAR\n\
                  REAL_TO_DINT := 0;\nEND_FUNCTION\n\
                  FUNCTION WRAP_DINT : DINT\n\
                  VAR_INPUT IN : DINT; END_VAR\n\
                  WRAP_DINT := IN;\nEND_FUNCTION\n\
                  FUNCTION CONVERT_NESTED : DINT\n\
                  VAR_INPUT R : REAL; END_VAR\n\
                  CONVERT_NESTED := WRAP_DINT(REAL_TO_DINT(R));\nEND_FUNCTION\n";
    let (_directory, ids) = generate_inline(source);
    assert!(ids.entries.iter().any(|entry|
        entry.pou == "CONVERT_NESTED"
            && entry.kind == semantist_stg::model::TargetKind::HazardViolation
            && matches!(entry.violation_predicate.as_ref(),
                Some(ViolationPredicate::FloatToInteger { .. }))));
}

#[test]
fn preserves_folded_implicit_narrowing_semantics() {
    let directory = tempfile::tempdir().unwrap();
    let fixture =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/implicit_narrowing.st");
    let output = directory.path().join("model");
    generate(&GenerateOptions {
        inputs: vec![fixture.file_name().unwrap().into()],
        project_root: fixture.parent().unwrap().to_path_buf(),
        output_dir: output.clone(),
        pou_filters: Vec::new(),
        pretty: true,
        emit_dot: true,
        emit_debug_sidecars: true,
    })
    .unwrap();

    let runtime_ids: RuntimeIdTable =
        serde_json::from_slice(&fs::read(output.join("stg-runtime-ids.json")).unwrap()).unwrap();
    let violation = runtime_ids
        .entries
        .iter()
        .find(|entry| {
            entry.kind == semantist_stg::model::TargetKind::HazardViolation
                && entry.hazard == Some(HazardKind::DangerousConversion)
        })
        .unwrap();
    assert_eq!(
        violation.modeling,
        semantist_stg::model::ModelingStatus::Exact
    );
    assert!(matches!(
        violation.violation_predicate.as_ref(),
        Some(ViolationPredicate::Narrowing {
            source_bit_width: Some(32),
            target_bit_width: Some(8),
            source_signed: Some(true),
            target_signed: Some(false),
            source_constant: Some(value),
            ..
        }) if value == "112"
    ));
}

#[test]
fn extracts_complete_control_and_state_semantics() {
    let directory = tempfile::tempdir().unwrap();
    let model = generate_fixture(directory.path().join("model"));
    assert_eq!(model.schema_version, SCHEMA_VERSION);
    assert_eq!(model.pous.len(), 5);
    assert_eq!(model.statistics.error_count, 0, "{:#?}", model.diagnostics);

    let function = model
        .pous
        .iter()
        .find(|pou| pou.pou.qualified_name == "CONTROL_FLOW")
        .unwrap();
    for expected in [
        PredicateKind::If,
        PredicateKind::Elsif,
        PredicateKind::Case,
        PredicateKind::While,
        PredicateKind::Repeat,
        PredicateKind::For,
    ] {
        assert!(function
            .graph
            .nodes
            .iter()
            .any(|node| matches!(&node.kind, NodeKind::Predicate(kind) if kind == &expected)));
    }
    assert!(function
        .graph
        .nodes
        .iter()
        .filter_map(|node| node.outcome.as_ref())
        .any(|outcome| outcome.role == OutcomeRole::CaseDefault));
    assert!(function
        .graph
        .nodes
        .iter()
        .filter_map(|node| node.outcome.as_ref())
        .any(|outcome| outcome.role == OutcomeRole::LoopContinue));
    assert!(function.graph.edges.iter().any(|edge| edge.is_back_edge));
    assert!(function
        .graph
        .edges
        .iter()
        .all(|edge| { edge.kind != EdgeKind::ControlTransfer || edge.transfer.is_some() }));
    assert!(function.graph.nodes.iter().any(|node| {
        node.hazard
            .as_ref()
            .is_some_and(|hazard| hazard.kind == HazardKind::Division)
    }));
    assert!(function.graph.nodes.iter().any(|node| {
        node.hazard
            .as_ref()
            .is_some_and(|hazard| hazard.kind == HazardKind::ArrayAccess)
    }));
    assert!(function
        .mappings
        .nodes
        .iter()
        .any(|mapping| !mapping.lowered_ast_ids.is_empty()));

    let function_block = model
        .pous
        .iter()
        .find(|pou| pou.pou.kind == PouKind::FunctionBlock)
        .unwrap();
    let cycle_entry = function_block.graph.cycle_entry_id.as_ref().unwrap();
    let cycle_exit = function_block.graph.cycle_exit_id.as_ref().unwrap();
    assert!(function_block.graph.edges.iter().any(|edge| {
        edge.kind == EdgeKind::Temporal && &edge.from == cycle_exit && &edge.to == cycle_entry
    }));
    let state = function_block
        .environment
        .persistent_state
        .as_ref()
        .unwrap();
    assert!(state
        .symbols
        .iter()
        .any(|symbol| symbol.ends_with(".state")));
    assert!(state
        .symbols
        .iter()
        .any(|symbol| symbol.ends_with(".out_value")));
    assert!(!state
        .symbols
        .iter()
        .any(|symbol| symbol.ends_with(".enable") || symbol.ends_with(".reset")));
    assert!(state
        .cycle_inputs
        .iter()
        .any(|symbol| symbol.ends_with(".enable")));
    assert!(state
        .configuration_symbols
        .iter()
        .any(|symbol| symbol.ends_with(".limit")));
    assert!(function_block.environment.symbols.iter().any(|symbol| {
        symbol.name.eq_ignore_ascii_case("limit")
            && !symbol.constant
            && symbol.configuration_source == Some(ConfigurationSource::VarInputConstantAnnotation)
    }));
}

#[test]
fn return_terminates_flow_and_dependencies_cross_transfer_edges() {
    let directory = tempfile::tempdir().unwrap();
    let model = generate_fixture(directory.path().join("model"));

    let early_return = model
        .pous
        .iter()
        .find(|pou| pou.pou.qualified_name == "EARLY_RETURN")
        .unwrap();
    let return_node = early_return
        .graph
        .nodes
        .iter()
        .find(|node| {
            node.outcome
                .as_ref()
                .is_some_and(|outcome| outcome.role == OutcomeRole::Return)
        })
        .unwrap();
    assert!(early_return.targets.iter().any(|target| {
        target.node_id == return_node.id && target.role == Some(OutcomeRole::Return)
    }));
    assert!(early_return.graph.edges.iter().any(|edge| {
        edge.to == return_node.id
            && edge.transfer.as_ref().is_some_and(|summary| {
                summary.operations.iter().any(|operation| {
                    matches!(
                        operation,
                        semantist_stg::model::TransferOperation::Return { .. }
                    )
                })
            })
    }));
    assert!(early_return
        .graph
        .edges
        .iter()
        .any(|edge| edge.from == return_node.id && edge.to == early_return.graph.exit_id));

    let dependency = model
        .pous
        .iter()
        .find(|pou| pou.pou.qualified_name == "TRANSITIVE_DEPENDENCY")
        .unwrap();
    let input_name = dependency
        .environment
        .symbols
        .iter()
        .find(|symbol| symbol.name.eq_ignore_ascii_case("input_value"))
        .unwrap()
        .qualified_name
        .clone();
    let enabled_name = dependency
        .environment
        .symbols
        .iter()
        .find(|symbol| symbol.name.eq_ignore_ascii_case("enabled"))
        .unwrap()
        .qualified_name
        .clone();
    let branch_targets = dependency
        .targets
        .iter()
        .filter(|target| target.kind == semantist_stg::model::TargetKind::BranchOutcome)
        .collect::<Vec<_>>();
    assert!(!branch_targets.is_empty());
    assert!(branch_targets.iter().all(|target| {
        target.backward_dependencies.contains(&input_name)
            && target.backward_dependencies.contains(&enabled_name)
    }));
}

#[test]
fn generation_is_byte_deterministic() {
    let directory = tempfile::tempdir().unwrap();
    let first = directory.path().join("first");
    let second = directory.path().join("second");
    generate_fixture(first.clone());
    generate_fixture(second.clone());
    assert_eq!(
        fs::read(first.join("stg-model.json")).unwrap(),
        fs::read(second.join("stg-model.json")).unwrap()
    );
    assert_eq!(
        fs::read(first.join("stg-codegen-map.json")).unwrap(),
        fs::read(second.join("stg-codegen-map.json")).unwrap()
    );
    assert_eq!(
        fs::read(first.join("stg-runtime-ids.json")).unwrap(),
        fs::read(second.join("stg-runtime-ids.json")).unwrap()
    );
}

#[test]
fn accepts_a_rusty_project_directory() {
    let directory = tempfile::tempdir().unwrap();
    let fixtures = fixture().parent().unwrap().to_path_buf();
    let project = fixtures.join("project");
    let output = directory.path().join("project-model");
    generate(&GenerateOptions {
        inputs: vec![project],
        project_root: fixtures,
        output_dir: output.clone(),
        pou_filters: vec!["STATEFUL_COUNTER".to_string()],
        pretty: true,
        emit_dot: true,
        emit_debug_sidecars: true,
    })
    .unwrap();
    let model: semantist_stg::model::ProjectSemanticModel =
        serde_json::from_slice(&fs::read(output.join("stg-model.json")).unwrap()).unwrap();
    assert_eq!(model.pous.len(), 1);
    assert_eq!(model.pous[0].pou.kind, PouKind::FunctionBlock);
    assert!(model
        .metadata
        .inputs
        .iter()
        .any(|input| input.relative_path.ends_with("project/plc.json")));
}

#[test]
fn accepts_multiple_pou_filters_in_one_compilation() {
    let directory = tempfile::tempdir().unwrap();
    let output = directory.path().join("filtered-model");
    generate(&GenerateOptions {
        inputs: vec![fixture().file_name().unwrap().into()],
        project_root: fixture().parent().unwrap().to_path_buf(),
        output_dir: output.clone(),
        pou_filters: vec!["EARLY_RETURN".to_string(), "STATEFUL_COUNTER".to_string()],
        pretty: true,
        emit_dot: true,
        emit_debug_sidecars: true,
    })
    .unwrap();
    let model: semantist_stg::model::ProjectSemanticModel =
        serde_json::from_slice(&fs::read(output.join("stg-model.json")).unwrap()).unwrap();
    assert_eq!(
        model
            .pous
            .iter()
            .map(|pou| pou.pou.qualified_name.as_str())
            .collect::<Vec<_>>(),
        vec!["EARLY_RETURN", "STATEFUL_COUNTER"]
    );
}
