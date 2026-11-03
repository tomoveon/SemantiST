import json
from pathlib import Path

from fuzzer.pipeline.campaign import Campaign
from fuzzer.pipeline.probe import Observation, classify, load_runtime_index


def test_float_to_integer_requires_typed_predicate_and_native_hit() -> None:
    source = {"file": "renamed.st", "start_line": 9}
    entry = {"kind": "hazard_violation", "hazard": "dangerous_conversion",
             "modeling": "conservative", "source": source,
             "violation_predicate": {"kind": "float_to_integer", "target_bit_width": 16,
                                     "signed": True}}
    observation = Observation(0, False, "", hits={4: [0]})
    assert classify(observation, {4: entry}) == {
        "oracle": "conversion_out_of_range", "hazard": "dangerous_conversion",
        "runtime_id": 4, "cycle": 0, "source": source,
        "kind": "hazard_violation", "diagnostic": None,
        "modeling": "conservative", "observed_predicate": "float_to_integer",
    }
    assert classify(Observation(0, False, ""), {4: entry}) is None
    assert classify(observation, {4: {**entry, "violation_predicate": {"kind": "narrowing"}}}) is None


def test_pointer_width_annotation_requires_source_effect_chain(tmp_path: Path) -> None:
    source = {"file": "renamed.st", "start_line": 14}
    entry = {"runtime_id": 7, "pou": "RENAMED", "kind": "hazard_violation",
             "hazard": "dangerous_conversion", "modeling": "conservative",
             "source": source,
             "violation_predicate": {"kind": "narrowing", "source_bit_width": 64,
                                     "target_bit_width": 32, "signed": False}}
    runtime = tmp_path / "stg-runtime-ids.json"
    runtime.write_text(json.dumps({"entries": [entry, {**entry, "runtime_id": 8, "pou": "OTHER"}]}),
                       encoding="utf-8")
    symbols = [{"qualified_name": "RENAMED.PT", "type_name": "P"},
               {"qualified_name": "RENAMED.temp", "type_name": "LWORD"},
               {"qualified_name": "RENAMED.end", "type_name": "DWORD"}]
    types = [{"name": "P", "kind": "pointer", "bit_width": 64},
             {"name": "LWORD", "kind": "integer", "bit_width": 64},
             {"name": "DWORD", "kind": "integer", "bit_width": 32}]
    operations = [
        {"kind": "assign", "target_symbol": "RENAMED.temp", "reads": ["RENAMED.PT"],
         "source": {"file": "renamed.st", "start_line": 13}},
        {"kind": "assign", "target_symbol": "RENAMED.end", "reads": ["RENAMED.temp"],
         "source": source},
    ]
    model = {"pous": [{"pou": {"qualified_name": "RENAMED"},
                       "environment": {"symbols": symbols, "types": types},
                       "graph": {"edges": [{"transfer": {"operations": operations}}]}}]}
    model_path = tmp_path / "stg-model.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    index = load_runtime_index(runtime)
    assert index[7]["platform_pointer_narrowing"] is True
    assert not index[8].get("platform_pointer_narrowing")
    assert classify(Observation(0, False, "", hits={7: [0]}), index)["observed_predicate"] == "narrowing"
    model["pous"][0]["graph"]["edges"][0]["transfer"]["operations"][0]["reads"] = []
    model_path.write_text(json.dumps(model), encoding="utf-8")
    assert classify(Observation(0, False, "", hits={7: [0]}), load_runtime_index(runtime)) is None


def test_float_anchor_uses_target_width_and_controllable_field() -> None:
    task = {"goal": {"runtime_id": 3}, "controllable_fields": ["RENAMED_IN"]}
    abi = {"frame_fields": [{"name": "RENAMED_IN", "spec": {"name": "REAL"}},
                            {"name": "OTHER", "spec": {"name": "REAL"}}]}
    runtime = {3: {"violation_predicate": {"kind": "float_to_integer",
                                           "target_bit_width": 16, "signed": True}}}
    assert Campaign._float_conversion_anchor(task, abi, runtime) == ("RENAMED_IN", "32768.0")
    assert Campaign._float_conversion_anchor(task, abi, {3: {"violation_predicate": {"kind": "narrowing"}}}) is None
