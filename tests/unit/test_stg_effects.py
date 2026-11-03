import time

from compiler.stg.effects import resolve_effect_model
from fuzzer.pipeline.goal_tasks import cycle_effects


def _pou(name, calls=(), symbols=(), edges=()):
    return {"pou": {"name": name, "qualified_name": name,
                    "kind": "function", "stateful": False},
            "environment": {"calls": list(calls), "symbols": list(symbols)},
            "graph": {"edges": list(edges)}}


def _call(name, writes=()):
    return {"callee": name, "kind": "function", "external": False,
            "writes": list(writes)}


def _abi(name):
    return {"target": {"name": name, "kind": "FUNCTION"},
            "frame_fields": [], "shared_fields": [], "state_fields": []}


def test_transitive_extracted_functions_prove_single_cycle():
    target = _pou("RENAMED_TARGET", [_call("RENAMED_HELPER", ["RENAMED_HELPER.ARG"])])
    helper = _pou("RENAMED_HELPER", [_call("ANOTHER_HELPER")])
    leaf = _pou("ANOTHER_HELPER")
    batches = []

    def extract(names):
        batches.append(names)
        available = {"RENAMED_HELPER": helper, "ANOTHER_HELPER": leaf}
        return {"pous": [available[name] for name in names if name in available]}

    model = resolve_effect_model({"pous": [target]}, extract,
                                 deadline=time.monotonic() + 5)
    assert batches == [["RENAMED_HELPER"], ["ANOTHER_HELPER"]]
    assert model["effect_resolution"]["status"] == "COMPLETE"
    assert cycle_effects(_abi("RENAMED_TARGET"), model)["mode"] == "single_cycle"


def test_unavailable_callee_remains_unknown():
    target = _pou("RENAMED_TARGET", [_call("MISSING_HELPER")])
    model = resolve_effect_model({"pous": [target]}, lambda names: {"pous": []},
                                 deadline=time.monotonic() + 5)
    assert model["effect_resolution"]["unresolved_callees"] == ["MISSING_HELPER"]
    assert cycle_effects(_abi("RENAMED_TARGET"), model)["mode"] == "unknown"


def test_failed_or_budgeted_effect_extraction_preserves_unknown():
    target = _pou("RENAMED_TARGET", [_call("RENAMED_HELPER")])

    def unavailable(names):
        raise RuntimeError("extractor unavailable")

    failed = resolve_effect_model({"pous": [target]}, unavailable,
                                  deadline=time.monotonic() + 5)
    budgeted = resolve_effect_model({"pous": [target]}, unavailable,
                                    deadline=time.monotonic() - 1)
    assert failed["effect_resolution"]["status"] == "INCOMPLETE"
    assert budgeted["effect_resolution"]["status"] == "BUDGET_EXHAUSTED"
    assert cycle_effects(_abi("RENAMED_TARGET"), failed)["mode"] == "unknown"
    assert cycle_effects(_abi("RENAMED_TARGET"), budgeted)["mode"] == "unknown"


def test_extracted_callee_global_write_keeps_multicycle():
    target = _pou("RENAMED_TARGET", [_call("RENAMED_HELPER")])
    helper = _pou("RENAMED_HELPER", symbols=[{"role": "global", "constant": False}])
    model = resolve_effect_model({"pous": [target]}, lambda names: {"pous": [helper]},
                                 deadline=time.monotonic() + 5)
    effects = cycle_effects(_abi("RENAMED_TARGET"), model)
    assert effects["mode"] == "multi_cycle"
    assert "global" in effects["reasons"]


def test_callee_inout_formal_without_alias_evidence_stays_unknown():
    target = _pou("RENAMED_TARGET", [_call("RENAMED_HELPER", ["RENAMED_HELPER.TEMP"])])
    helper = _pou("RENAMED_HELPER", symbols=[{"name": "TEMP", "role": "in_out"}])
    model = resolve_effect_model({"pous": [target]}, lambda names: {"pous": [helper]},
                                 deadline=time.monotonic() + 5)
    effects = cycle_effects(_abi("RENAMED_TARGET"), model)
    assert effects["mode"] == "unknown"
    assert "callee_inout_alias_unresolved" in effects["unknown_reasons"]


def test_extracted_stateful_callee_keeps_multicycle():
    target = _pou("RENAMED_TARGET", [{"callee": "RENAMED_BLOCK", "kind": "function_block",
                                      "external": False, "writes": []}])
    callee = _pou("RENAMED_BLOCK")
    callee["pou"].update(kind="function_block", stateful=True)
    model = resolve_effect_model({"pous": [target]}, lambda names: {"pous": [callee]},
                                 deadline=time.monotonic() + 5)
    effects = cycle_effects(_abi("RENAMED_TARGET"), model)
    assert effects["mode"] == "multi_cycle"
    assert "stg_stateful" in effects["reasons"]


def test_extracted_callee_pointer_write_keeps_multicycle(tmp_path):
    statement = b"P^ := VALUE;"
    source = tmp_path / "renamed.st"
    source.write_bytes(statement)
    target = _pou("RENAMED_TARGET", [_call("RENAMED_HELPER")])
    helper = _pou("RENAMED_HELPER", edges=[{"transfer": {"operations": [{
        "kind": "assign", "source": {"file": str(source), "start_offset": 0,
                                       "end_offset": len(statement)}}]}}])
    model = resolve_effect_model({"pous": [target]}, lambda names: {"pous": [helper]},
                                 deadline=time.monotonic() + 5)
    effects = cycle_effects(_abi("RENAMED_TARGET"), model)
    assert effects["mode"] == "multi_cycle"
    assert "pointer_write" in effects["reasons"]
