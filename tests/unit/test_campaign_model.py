import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from fuzzer.corpus.frames import parse_testcase
from fuzzer.pipeline.campaign import Campaign
from fuzzer.pipeline.analysis import collect_literals
from fuzzer.pipeline.goal_tasks import (
    MAX_PLANNED_PREFIXES_PER_TASK,
    _array_guard_values,
    _planned_prefixes,
    build_goal_tasks,
)
from fuzzer.pipeline.model import InputField, TargetModel, _AbiLike


def test_stg_real_array_guards_become_valid_concrete_elements():
    def literal(value):
        return {"kind": {"kind": "literal", "value": {"kind": "real", "value": str(value)}}, "operands": []}

    array = {"kind": {"kind": "array_index"}, "operands": [
        {"kind": {"kind": "member_access", "member": "Values"}, "operands": []},
        {"kind": {"kind": "literal", "value": {"kind": "integer", "value": "1"}}, "operands": []},
    ]}
    comparisons = [
        {"kind": {"kind": "binary", "operator": op}, "operands": [array, literal(value)]}
        for op, value in ((">", 2), ("<", 2.14))
    ]
    expression = {"kind": {"kind": "binary", "operator": "AND"}, "operands": comparisons}
    spec = {"kind": "array", "name": "ARRAY OF LREAL", "count": 3, "byte_size": 24,
            "element": {"kind": "scalar", "name": "LREAL", "byte_size": 8}}
    assert collect_literals(expression) == {1, 2.0, 2.14}
    values = _array_guard_values(expression, {"Values": spec})
    payload = bytes.fromhex(values["Values"][2:])
    assert len(payload) == 24
    assert 2 < struct.unpack_from("<d", payload, 8)[0] < 2.14
    abi = {"frame_fields": [{"name": "Mode", "spec": {"kind": "scalar", "name": "SINT", "size": 1}}],
           "shared_fields": [{"name": "Values", "block_kind": "VAR_IN_OUT", "spec": spec}]}
    task = {"control_obligations": [{"goal_expression": expression}]}
    prefixes = _planned_prefixes(abi, task, [])
    assert any(len(prefix) == 6 and prefix[1]["Mode"] == "1"
               and prefix[1]["Values"] == values["Values"] for prefix in prefixes)
    wider = {**spec, "count": 7, "byte_size": 56}
    wider_abi = {**abi, "shared_fields": [{"name": "Values", "block_kind": "VAR_IN_OUT",
                                           "spec": wider}]}
    wider_prefixes = _planned_prefixes(wider_abi, task, [])
    assert any(len(prefix) == 10 for prefix in wider_prefixes)


def test_persistent_array_boundary_prefixes_follow_types_bounds_and_names():
    for suffix, upper in (("ALPHA", 5), ("BETA", 9)):
        array = f"VALUES_{suffix}"
        state = f"CURSOR_{suffix}"
        limit = f"LIMIT_{suffix}"
        unrelated = f"UNRELATED_{suffix}"
        gate = f"GATE_{suffix}"
        abi = {"frame_fields": [
            {"name": array, "spec": {"kind": "array", "count": upper + 1}},
            {"name": limit, "spec": {"kind": "scalar", "name": "INT", "size": 2}},
            {"name": unrelated, "spec": {"kind": "scalar", "name": "INT", "size": 2}},
            {"name": gate, "spec": {"kind": "scalar", "name": "BOOL", "size": 1}},
        ]}
        task = {
            "terminal_hazard": "array_access",
            "terminal_goal_expression": {"kind": {"kind": "array_index"}, "operands": [
                {"kind": {"kind": "member_access", "member": array}},
                {"kind": {"kind": "member_access", "member": state},
                 "reads": [f"VARIANT.{state}"], "type_name": "INT"},
            ]},
            "temporal_requirements": [{"persistent_symbols": [f"VARIANT.{state}"]}],
            "data_dependencies": [{"symbol": f"VARIANT.{name}"}
                                  for name in (array, limit, unrelated, gate)],
            "controllable_seed_fields": [
                {"seed_field": array, "symbol": f"VARIANT.{array}", "role": "input",
                 "type": {"kind": "array", "dimensions": [{"lower": 0, "upper": upper}]}},
                {"seed_field": limit, "symbol": f"VARIANT.{limit}", "role": "input",
                 "type_name": "INT"},
                {"seed_field": unrelated, "symbol": f"VARIANT.{unrelated}", "role": "input",
                 "type_name": "INT"},
                {"seed_field": gate, "symbol": f"VARIANT.{gate}", "role": "input",
                 "type_name": "BOOL"},
            ],
        }
        update_reads = {f"VARIANT.{state}": {f"VARIANT.{state}", f"VARIANT.{limit}"}}
        prefixes = _planned_prefixes(abi, task, [], state_update_reads=update_reads)
        assert any(len(prefix) == upper + 5 and all(
            frame[limit] == str(upper + 1) and frame[unrelated] == "0"
            and frame[gate] == "1"
            for frame in prefix[1:]) for prefix in prefixes)
        assert not any(len(prefix) == upper + 5 for prefix in _planned_prefixes(
            abi, task, [], state_update_reads={f"VARIANT.{state}": {f"VARIANT.{unrelated}"}}
        ) if prefix[1][limit] == str(upper + 1))
        task["temporal_requirements"] = []
        assert not any(len(prefix) == upper + 5 for prefix in _planned_prefixes(
            abi, task, [], state_update_reads=update_reads))


def test_goal_tasks_emit_only_runtime_consumed_prefixes():
    abi = {"target": {"name": "ARRAY_TARGET", "kind": "FUNCTION_BLOCK"},
           "frame_fields": [{"name": "Mode", "block_kind": "VAR_INPUT",
                             "spec": {"kind": "scalar", "name": "INT", "size": 2}}],
           "shared_fields": [], "state_fields": []}
    plan = {"tasks": [{"task_id": "array-goal", "terminal_kind": "hazard_violation",
                       "terminal_hazard": "array_access", "terminal_runtime_id": 1,
                       "candidate_paths": [], "control_obligations": []}]}
    prefixes = build_goal_tasks(plan, abi, {"regions": []})["tasks"][0]["seed_prefixes"]
    assert len(prefixes) == MAX_PLANNED_PREFIXES_PER_TASK
    assert all(len(prefix) == 5 for prefix in prefixes)


def _write_minimal_exchange(tmp_path):
    run_dir = tmp_path / "run"
    bundle = run_dir / "bundle"
    stg = run_dir / "stg"
    bundle.mkdir(parents=True)
    stg.mkdir()
    (bundle / "abi.json").write_text(json.dumps({
        "schema_version": "semantist.abi/1.0.0",
        "target": {"name": "F", "kind": "FUNCTION"},
        "frame": {"size": 1, "align": 1, "fields": []},
        "frame_fields": [], "shared_fields": [], "state_fields": [],
    }), encoding="utf-8")
    (bundle / "state-layout.json").write_text(json.dumps({
        "schema_version": "semantist.state-layout/1.0.0",
        "snapshot_schema_version": 1, "total_size": 0,
        "harness_region_index": 0, "regions": [],
    }), encoding="utf-8")
    (run_dir / "semantic-task-plan.json").write_text(json.dumps({
        "schema_version": "semantist.semantic-task-plan/1.0.0", "tasks": [],
    }), encoding="utf-8")
    (stg / "stg-runtime-ids.json").write_text(json.dumps({"entries": []}), encoding="utf-8")
    (stg / "stg-model.json").write_text(json.dumps({
        "schema_version": "semantist.stg/1.2.0", "pous": [],
    }), encoding="utf-8")
    return run_dir, bundle


def test_target_model_rejects_missing_exchange_fields(tmp_path):
    run_dir, bundle = _write_minimal_exchange(tmp_path)
    loaded = TargetModel.load(run_dir, bundle)
    assert loaded.fields == []

    abi = json.loads((bundle / "abi.json").read_text(encoding="utf-8"))
    del abi["shared_fields"]
    (bundle / "abi.json").write_text(json.dumps(abi), encoding="utf-8")
    with pytest.raises(ValueError, match="shared_fields"):
        TargetModel.load(run_dir, bundle)


def test_target_model_build_preserves_prefix_shared_records_and_delta_t(tmp_path):
    abi = {
        "frame_fields": [{
            "name": "Step",
            "block_kind": "VAR_INPUT",
            "spec": {"kind": "scalar", "name": "INT", "size": 2, "c_type": "int16_t"},
        }],
        "shared_fields": [{
            "name": "Counter",
            "block_kind": "VAR_IN_OUT",
            "spec": {"kind": "scalar", "name": "UINT", "size": 2, "c_type": "uint16_t"},
        }],
    }
    model = TargetModel(
        run_dir=tmp_path,
        bundle=tmp_path,
        function="F",
        abi=abi,
        layout={},
        tasks={},
        runtime_index={},
        stg_model={},
        stg_types={},
        literals=set(),
        fields=[InputField("Step", abi["frame_fields"][0]["spec"]),
                InputField("Counter", abi["shared_fields"][0]["spec"])],
    )
    testcase = model.build(
        [{"Step": "1", "Counter": "7"}, {"Step": "2", "Counter": "8"}],
        shared={"Counter": "7"},
        frame_dt_ns=[11, 22],
    )
    parsed = parse_testcase(testcase, _AbiLike(model))
    assert parsed.shared == {"Counter": "7"}
    assert [frame.dt_ns for frame in parsed.frames] == [11, 22]
    assert [frame.values["Counter"] for frame in parsed.frames] == ["7", "8"]


def test_inout_only_model_build_keeps_backing_storage_controllable(tmp_path):
    abi = {
        "frame_fields": [],
        "shared_fields": [{
            "name": "Counter",
            "block_kind": "VAR_IN_OUT",
            "spec": {"kind": "scalar", "name": "UINT", "size": 2, "c_type": "uint16_t"},
        }],
    }
    model = TargetModel(
        run_dir=tmp_path,
        bundle=tmp_path,
        function="FB",
        abi=abi,
        layout={},
        tasks={},
        runtime_index={},
        stg_model={},
        stg_types={},
        literals=set(),
        fields=[InputField("Counter", abi["shared_fields"][0]["spec"])],
    )

    testcase = model.build(
        [{"Counter": "65535"}, {"Counter": "0"}],
        shared={"Counter": "7"},
        frame_dt_ns=[10, 25],
    )
    parsed = parse_testcase(testcase, _AbiLike(model))
    assert parsed.shared == {"Counter": "7"}
    assert [frame.values["Counter"] for frame in parsed.frames] == ["65535", "0"]
    assert [frame.dt_ns for frame in parsed.frames] == [10, 25]


def test_input_field_string_values_respect_declared_capacity():
    values = InputField("Wide", {
        "kind": "string", "name": "WSTRING", "size": 2, "length": 3,
    }).values(set())
    assert max(len(value) for value in values) <= 3


def test_inout_array_goal_prefix_has_valid_native_and_symbolic_initial_records(tmp_path):
    array_spec = {
        "kind": "array", "name": "ARRAY OF LREAL", "size": 0,
        "byte_size": 64, "count": 8,
        "element": {"kind": "scalar", "name": "LREAL", "size": 8, "byte_size": 8},
    }
    abi = {"frame_fields": [], "shared_fields": [
        {"name": "InRecv", "block_kind": "VAR_IN_OUT", "spec": array_spec},
        {"name": "Mode", "block_kind": "VAR_IN_OUT",
         "spec": {"kind": "scalar", "name": "SINT", "size": 1, "c_type": "int8_t"}},
    ]}
    plan = {"tasks": [{
        "task_id": "array-goal", "terminal_kind": "hazard_violation",
        "terminal_hazard": "array_access", "terminal_runtime_id": 1,
        "terminal_goal_expression": {}, "candidate_paths": [],
    }]}
    tasks = build_goal_tasks(plan, abi, {"regions": []})["tasks"]
    frames = tasks[0]["seed_prefixes"][0]
    assert frames[0]["InRecv"] == "0x00"
    assert Campaign._default_action_frame(abi)["InRecv"] == "0x00"
    assert Campaign._shared_records_for_frames(abi, frames).startswith("InRecv,ARRAY,0x00\n")

    model = TargetModel(
        run_dir=tmp_path, bundle=tmp_path, function="FB", abi=abi, layout={},
        tasks={}, runtime_index={}, stg_model={}, stg_types={}, literals=set(),
        fields=[InputField("InRecv", array_spec),
                InputField("Mode", abi["shared_fields"][1]["spec"])],
    )
    parsed = parse_testcase(model.build(frames), _AbiLike(model))
    assert parsed.shared["InRecv"] == "0x00"
    assert parsed.frames[0].values["InRecv"] == "0x00"
    assert parsed.types["InRecv"] == "ARRAY"


def test_goal_prefix_literals_stay_inside_each_integer_field_domain(tmp_path):
    abi = {"frame_fields": [
        {"name": "Offset", "spec": {"kind": "scalar", "name": "UDINT",
                                     "size": 4, "c_type": "uint32_t"}},
        {"name": "Mode", "spec": {"kind": "scalar", "name": "SINT",
                                   "size": 1, "c_type": "int8_t"}},
    ], "shared_fields": []}
    plan = {"tasks": [{
        "task_id": "integer-goal", "terminal_kind": "hazard_violation",
        "terminal_hazard": "array_access", "terminal_runtime_id": 1,
        "terminal_goal_expression": {"kind": "literal", "value": {"value": "400"}},
        "data_dependencies": [{"kind": "literal", "value": {"value": "-1"}}],
        "candidate_paths": [],
    }]}
    frames_by_seed = build_goal_tasks(plan, abi, {"regions": []})["tasks"][0]["seed_prefixes"]
    model = TargetModel(
        run_dir=tmp_path, bundle=tmp_path, function="F", abi=abi, layout={},
        tasks={}, runtime_index={}, stg_model={}, stg_types={}, literals=set(),
        fields=[InputField(entry["name"], entry["spec"]) for entry in abi["frame_fields"]],
    )
    assert frames_by_seed
    for frames in frames_by_seed:
        model.build(frames)
        for frame in frames:
            assert 0 <= int(frame["Offset"]) <= 2**32 - 1
            assert -128 <= int(frame["Mode"]) <= 127


def test_goal_tasks_preserve_modeling_status():
    abi = {"frame_fields": [], "shared_fields": []}
    plan = {"tasks": [{
        "task_id": "conservative-division", "terminal_kind": "hazard_violation",
        "terminal_hazard": "division", "terminal_runtime_id": 43,
        "modeling_status": "conservative", "candidate_paths": [],
    }]}
    tasks = build_goal_tasks(plan, abi, {"regions": []})["tasks"]
    assert tasks[0]["goal"]["modeling"] == "conservative"
    tasks = build_goal_tasks(
        plan, abi, {"regions": []},
        runtime_index={43: {"modeling": "exact"}},
    )["tasks"]
    assert tasks[0]["goal"]["modeling"] == "exact"
    assert tasks[0]["plan_modeling_status"] == "conservative"
    with pytest.raises(ValueError, match="runtime ID 43 missing modeling status"):
        build_goal_tasks(plan, abi, {"regions": []}, runtime_index={})


def test_goal_tasks_keep_guard_conditions_on_their_own_paths():
    def condition(field, value):
        return {"kind": {"kind": "binary", "operator": "="}, "operands": [
            {"kind": {"kind": "member_access", "member": field}},
            {"kind": {"kind": "literal", "value": {"value": str(value)}}},
        ]}

    plan = {"tasks": [{
        "task_id": "risk", "terminal_kind": "hazard_violation",
        "terminal_hazard": "array_access", "terminal_runtime_id": 1,
        "control_obligations": [
            {"evaluation_edge_id": "a", "goal_expression": condition("A", 1)},
            {"evaluation_edge_id": "b", "goal_expression": condition("B", 2)},
        ],
        "candidate_paths": [
            {"path_id": "left", "edge_ids": ["a"]},
            {"path_id": "right", "edge_ids": ["b"]},
        ],
    }]}
    abi = {"frame_fields": [], "shared_fields": []}
    paths = build_goal_tasks(plan, abi, {"regions": []})["tasks"][0]["candidate_paths"]
    assert [path["conditions"][0]["lhs"]["field"] for path in paths] == ["A", "B"]


def test_goal_seed_order_is_independent_of_python_hash_seed():
    script = """
import json
from fuzzer.pipeline.goal_tasks import build_goal_tasks

abi = {"frame_fields": [
    {"name": "Level", "spec": {"kind": "scalar", "name": "INT", "size": 2}},
    {"name": "RecipeCode", "spec": {"kind": "scalar", "name": "INT", "size": 2}},
], "shared_fields": []}
plan = {"tasks": [{
    "task_id": "ordered-seeds", "terminal_kind": "hazard_violation",
    "terminal_hazard": "array_access", "terminal_runtime_id": 1,
    "terminal_goal_expression": {"kind": "literal", "value": {"value": "700"}},
    "candidate_paths": [],
}]}
print(json.dumps(build_goal_tasks(plan, abi, {"regions": []}), sort_keys=True))
"""
    root = Path(__file__).resolve().parents[2]
    results = []
    for seed in ("1", "2", "17"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=root, env=env,
            capture_output=True, text=True, check=True,
        )
        results.append(completed.stdout)
    assert results[0] == results[1] == results[2]
