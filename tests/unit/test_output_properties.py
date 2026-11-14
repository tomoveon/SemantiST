from pathlib import Path

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from fuzzer.corpus.frames import build_testcase
from fuzzer.pipeline.probe import Observation
from fuzzer.pipeline.properties import evaluate_properties, load_properties


SPEC_FILE = (Path(__file__).resolve().parents[2] /
             "benchmarks/oscat_basic/source/filter_mav_dw.properties.json")
ABI = TargetAbi("FUNCTION_BLOCK", "FILTER_MAV_DW", [
    Param("X", scalar_spec("DWORD"), "VAR_INPUT"),
    Param("N", scalar_spec("UINT"), "VAR_INPUT"),
    Param("RST", scalar_spec("BOOL"), "VAR_INPUT"),
])


def _case(window: int = 2, reset_last: int = 0) -> bytes:
    return build_testcase(ABI, [
        {"X": "100", "N": str(window), "RST": "1"},
        {"X": "0", "N": str(window), "RST": str(reset_last)},
    ])


def test_initialized_window_property_distinguishes_output_error_from_wrap():
    specs = load_properties(SPEC_FILE, "FILTER_MAV_DW")
    broken = Observation(0, False, "", states=[{"Y": 100}, {"Y": 2147483698}])
    info = evaluate_properties(broken, _case(), ABI, specs)
    assert info["oracle"] == "output_invariant"
    assert info["property_id"] == "initialized-window-output-range"
    assert info["cycle"] == 1
    assert info["expected_range"] == [0, 100]
    correct = Observation(0, False, "", states=[{"Y": 100}, {"Y": 0}])
    assert evaluate_properties(correct, _case(window=1), ABI, specs) is None
    assert evaluate_properties(correct, _case(reset_last=1), ABI, specs) is None
    assert evaluate_properties(Observation(0, False, ""), _case(), ABI, specs) is None
    assert evaluate_properties(Observation(0, False, "", states=broken.states,
                                           scan_budget=True), _case(), ABI, specs) is None


def test_property_only_applies_to_selected_function():
    assert load_properties(SPEC_FILE, "SEQUENCE_64") == []
