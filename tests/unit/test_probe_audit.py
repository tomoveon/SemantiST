import json

from fuzzer.pipeline.probe_audit import audit_probe_completeness


def test_audit_reports_missing_ir_probe_and_pointer_width_assumption(tmp_path):
    stg = tmp_path / "stg"
    stg.mkdir()
    expression = {"id": "expr-1", "kind": {"kind": "array_index"},
                  "source": {"file": "variant.st", "start_line": 8}}
    model = {"pous": [{
        "pou": {"qualified_name": "RENAMED"},
        "environment": {
            "types": [
                {"name": "PTR", "kind": "pointer", "bit_width": 64},
                {"name": "WORD", "kind": "integer", "semantic_bit_width": 16},
            ],
            "symbols": [
                {"qualified_name": "RENAMED.ADDRESS", "type_name": "PTR"},
                {"qualified_name": "RENAMED.COPY", "type_name": "WORD"},
            ],
            "expressions": [expression],
        },
        "graph": {
            "nodes": [{"id": "node-1", "hazard": {
                "kind": "array_access", "expression_id": "expr-1"}}],
            "edges": [{"transfer": {"operations": [{
                "kind": "assign", "target_symbol": "RENAMED.COPY",
                "reads": ["RENAMED.ADDRESS"],
                "source": {"file": "variant.st", "start_line": 5},
            }]}}],
        },
    }]}
    runtime = {"entries": [{"node_id": "node-1", "kind": "hazard_violation",
                            "runtime_id": 7,
                            "violation_predicate": {"kind": "array_bounds"}}]}
    (stg / "stg-model.json").write_text(json.dumps(model))
    (stg / "stg-runtime-ids.json").write_text(json.dumps(runtime))
    (stg / "stg-ir-mapping.json").write_text(json.dumps({"mappings": []}))
    result = audit_probe_completeness(tmp_path)
    assert result["status"] == "INCOMPLETE"
    assert result["risks"][0]["status"] == "MISSING_IR_PROBE"
    assert result["platform_assumptions"][0]["pointer_bits"] == 64
    assert result["platform_assumptions"][0]["integer_bits"] == 16


def test_audit_accepts_mapped_safe_array_target_without_platform_assumption(tmp_path):
    stg = tmp_path / "stg"
    stg.mkdir()
    model = {"pous": [{"pou": {"qualified_name": "SAFE_VARIANT"},
                       "environment": {"types": [], "symbols": [], "expressions": [{
                           "id": "expr-1", "kind": {"kind": "array_index"}}]},
                       "graph": {"nodes": [{"id": "node-1", "hazard": {
                           "kind": "array_access", "expression_id": "expr-1"}}],
                                 "edges": []}}]}
    runtime = {"entries": [{"node_id": "node-1", "kind": "hazard_violation",
                            "runtime_id": 3,
                            "violation_predicate": {"kind": "array_bounds"}}]}
    ir = {"mappings": [{"runtime_id": 3, "mapping_kind": "hazard_violation"}]}
    for name, document in (("stg-model.json", model), ("stg-runtime-ids.json", runtime),
                           ("stg-ir-mapping.json", ir)):
        (stg / name).write_text(json.dumps(document))
    result = audit_probe_completeness(tmp_path)
    assert result["status"] == "COMPLETE"
    assert result["incomplete_count"] == 0


def test_audit_distinguishes_missing_search_task_from_missing_ir_probe(tmp_path):
    stg = tmp_path / "stg"
    bundle = tmp_path / "bundle"
    stg.mkdir()
    bundle.mkdir()
    model = {"pous": [{"pou": {"qualified_name": "RENAMED"},
                       "environment": {"types": [], "symbols": [], "expressions": [
                           {"id": "expr", "kind": {"kind": "array_index"}}]},
                       "graph": {"nodes": [{"id": "node", "hazard": {
                           "kind": "array_access", "expression_id": "expr"}}], "edges": []}}]}
    runtime = {"entries": [{"node_id": "node", "kind": "hazard_violation",
                            "runtime_id": 42, "violation_predicate": {"kind": "array_bounds"}}]}
    ir = {"mappings": [{"runtime_id": 42, "mapping_kind": "hazard_violation"}]}
    for name, document in (("stg-model.json", model), ("stg-runtime-ids.json", runtime),
                           ("stg-ir-mapping.json", ir)):
        (stg / name).write_text(json.dumps(document))
    (bundle / "goal-tasks.json").write_text(json.dumps({"tasks": []}))
    result = audit_probe_completeness(tmp_path)
    assert result["risks"][0]["status"] == "MISSING_SEARCH_TASK"


def test_audit_requires_probe_for_implicit_narrowing_assignment(tmp_path):
    stg = tmp_path / "stg"
    stg.mkdir()
    model = {"pous": [{
        "pou": {"qualified_name": "RENAMED"},
        "environment": {"types": [], "symbols": [], "expressions": [{
            "id": "value", "kind": {"kind": "variable"},
            "source": {"file": "variant.st", "start_line": 3},
        }]},
        "graph": {"nodes": [], "edges": [{"transfer": {"operations": [{
            "kind": "assign", "value": "value",
            "conversion": {"from": "DINT", "to": "INT", "potentially_narrowing": True},
            "source": {"file": "variant.st", "start_line": 3},
        }]}}]},
    }]}
    for name, document in (("stg-model.json", model),
                           ("stg-runtime-ids.json", {"entries": []}),
                           ("stg-ir-mapping.json", {"mappings": []})):
        (stg / name).write_text(json.dumps(document))
    result = audit_probe_completeness(tmp_path)
    assert result["incomplete_count"] == 1
    assert result["risks"][0]["kind"] == "dangerous_conversion"
    assert result["risks"][0]["status"] == "MISSING_SOURCE_TARGET"
