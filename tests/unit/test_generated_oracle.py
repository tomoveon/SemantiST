import pytest
from types import SimpleNamespace

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from compiler.harness.oracle import OracleRejected, render_monitor, validate_document
from fuzzer.pipeline.campaign import Campaign
from fuzzer.pipeline.probe import Observation, classify


ABI = TargetAbi(
    "FUNCTION_BLOCK", "FILTER_MAV_DW",
    [Param("X", scalar_spec("DWORD"), "VAR_INPUT", index=1),
     Param("N", scalar_spec("UINT"), "VAR_INPUT", index=2),
     Param("RST", scalar_spec("BOOL"), "VAR_INPUT", index=3)],
    outputs=[Param("Y", scalar_spec("DWORD"), "VAR_OUTPUT", index=4)],
)


def _field(name, at):
    return {"field": name, "at": at}


def _op(operator, *args):
    return {"op": operator, "args": list(args)}


DOCUMENT = {
    "schema_version": "semantist.generated-oracle/1",
    "properties": [{
        "id": "reset-window-range", "precision": "exact",
        "evidence_refs": ["manual:basic:p272"],
        "when": _op("and", _field("RST", "previous_input"),
                    _op("not", _field("RST", "input"))),
        "must": _op("between", _field("Y", "output"),
                    _op("min", _field("X", "previous_input"),
                        _field("X", "input")),
                    _op("max", _field("X", "previous_input"),
                        _field("X", "input"))),
    }],
}


def test_generated_oracle_compiles_to_guarded_monitor():
    assert len(validate_document(ABI, DOCUMENT)) == 1
    definitions, hook = render_monitor(ABI, DOCUMENT)
    assert "oracle_previous_valid" in definitions
    assert "SEMANTIST_PROPERTY_VIOLATION" in definitions
    assert "oracle_prev_i1" in hook
    assert "semantist_oracle_check(frame);" in hook


def test_generated_oracle_rejects_unknown_field_and_c_expression():
    invalid = {**DOCUMENT, "properties": [{**DOCUMENT["properties"][0],
               "must": _op("gt", _field("MISSING", "output"), {"const": 0})}]}
    with pytest.raises(OracleRejected, match="unknown scalar"):
        validate_document(ABI, invalid)
    invalid = {**DOCUMENT, "properties": [{**DOCUMENT["properties"][0],
               "must": {"c_code": "abort()"}}]}
    with pytest.raises(OracleRejected, match="oracle node"):
        validate_document(ABI, invalid)


def test_generated_oracle_abort_has_separate_classification():
    observation = Observation(-6, False, "SEMANTIST_PROPERTY_VIOLATION:reset-window-range:1\n",
                              crashed=True)
    result = classify(observation, {})
    assert result["oracle"] == "generated_property"
    assert result["property_id"] == "reset-window-range"
    assert result["cycle"] == 1
    assert classify(Observation(-6, False, "unrelated abort\n", crashed=True), {}).get("oracle") != "generated_property"


def test_engine_candidate_and_cold_confirmation_use_same_property_key():
    campaign = Campaign.__new__(Campaign)
    campaign.model = SimpleNamespace(runtime_index={})
    campaign.generated_properties = {"reset-window-range": DOCUMENT["properties"][0]}
    observation = Observation(-6, False, "SEMANTIST_PROPERTY_VIOLATION:reset-window-range:1\n",
                              crashed=True)
    campaign._observe_again = lambda *args, **kwargs: observation
    _, initial = campaign._replay_engine_candidate(b"case")
    replayed = campaign._classify_testcase(observation, b"case",
                                          expected_property_id="reset-window-range")
    assert campaign._same_candidate(initial, replayed)
    assert initial["source"]["file"] == "manual:basic:p272"
