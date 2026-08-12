from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzzer.semantic.planning import (
    SemanticTaskPlanner,
    SeedProfile,
    TaskRuntime,
    bounded_paths,
    build_seed_profile,
    choose_active_path,
    defer_task,
    guide_target,
    rank_tasks,
    refresh_task_runtime,
    strongly_connected_components,
)


def target(target_id: str, node_id: str, kind: str, modeling: str = "exact"):
    return {
        "id": target_id,
        "node_id": node_id,
        "kind": kind,
        "modeling": modeling,
    }


def pou(
    name: str,
    nodes: list[dict],
    edges: list[dict],
    targets: list[dict],
    *,
    expressions: list[dict] | None = None,
    symbols: list[dict] | None = None,
    dependencies: dict[str, list[str]] | None = None,
    def_use: list[dict] | None = None,
    persistent: list[str] | None = None,
    types: list[dict] | None = None,
):
    return {
        "model_id": f"model:{name}",
        "pou": {"qualified_name": name},
        "graph": {
            "entry_id": nodes[0]["id"],
            "nodes": nodes,
            "edges": edges,
        },
        "targets": targets,
        "environment": {
            "expressions": expressions or [],
            "symbols": symbols or [],
            "target_dependencies": dependencies or {},
            "def_use": def_use or [],
            "persistent_state": {"symbols": persistent or []},
            "types": types or [],
        },
    }


def semantic_fixture():
    function = pou(
        "FUNCTION_F",
        [{"id": "f.entry"}, {"id": "f.true"}, {"id": "f.exit"}],
        [
            {
                "id": "f.eval",
                "from": "f.entry",
                "to": "f.true",
                "kind": "evaluation",
                "guard": "f.gt",
            },
            {"id": "f.flow", "from": "f.true", "to": "f.exit", "kind": "flow"},
        ],
        [
            target("f.true.target", "f.true", "branch_outcome"),
            target("f.property", "f.exit", "property_violation"),
        ],
        expressions=[
            {
                "id": "f.gt",
                "kind": "greater_than",
                "type_name": "BOOL",
                "modeling": "exact",
                "reads": ["FUNCTION_F.X"],
                "operands": ["f.x", "f.zero"],
            },
            {
                "id": "f.x",
                "kind": "variable",
                "type_name": "INT",
                "modeling": "exact",
                "reads": ["FUNCTION_F.X"],
                "operands": [],
            },
            {
                "id": "f.zero",
                "kind": "literal",
                "type_name": "INT",
                "modeling": "exact",
                "reads": [],
                "operands": [],
            },
        ],
        symbols=[
            {
                "qualified_name": "FUNCTION_F.X",
                "name": "X",
                "role": "input",
                "type_name": "INT",
            }
        ],
        dependencies={"f.property": ["FUNCTION_F.X"]},
        def_use=[
            {
                "defined_symbol": "FUNCTION_F.X",
                "used_symbol": "FUNCTION_F.X",
            }
        ],
        types=[
            {
                "name": "INT",
                "kind": "integer",
                "bit_width": 16,
                "signed": True,
            }
        ],
    )

    count_dr = pou(
        "COUNT_DR",
        [
            {"id": "c.entry"},
            {"id": "c.update"},
            {"id": "c.guard"},
            {
                "id": "c.mod",
                "hazard": {"kind": "modulo_by_zero", "expression_id": "c.mod.expr"},
            },
        ],
        [
            {
                "id": "c.transfer",
                "from": "c.entry",
                "to": "c.update",
                "kind": "flow",
                "transfer": {
                    "reads": ["COUNT_DR.UP", "COUNT_DR.last_up", "COUNT_DR.CNT"],
                    "writes": ["COUNT_DR.last_up", "COUNT_DR.CNT"],
                    "versions_in": {"COUNT_DR.CNT": 0, "COUNT_DR.last_up": 0},
                    "versions_out": {"COUNT_DR.CNT": 1, "COUNT_DR.last_up": 1},
                    "operations": [{"kind": "assign"}],
                    "modeling": "exact",
                },
            },
            {
                "id": "c.temporal",
                "from": "c.update",
                "to": "c.guard",
                "kind": "temporal",
                "temporal": {
                    "persistent_symbols": ["COUNT_DR.last_up", "COUNT_DR.CNT"],
                    "rule": "next_cycle",
                    "modeling": "exact",
                },
            },
            {
                "id": "c.eval",
                "from": "c.guard",
                "to": "c.mod",
                "kind": "evaluation",
                "guard": "c.mod.expr",
            },
        ],
        [
            target("c.cycle", "c.update", "cycle"),
            target("c.mod.reach", "c.mod", "hazard_reach"),
            target("c.mod.violation", "c.mod", "hazard_violation"),
        ],
        expressions=[
            {
                "id": "c.mod.expr",
                "kind": "modulo",
                "type_name": "INT",
                "modeling": "exact",
                "reads": ["COUNT_DR.CNT", "COUNT_DR.DIVISOR"],
                "operands": ["c.cnt", "c.divisor"],
            },
            {
                "id": "c.cnt",
                "kind": "variable",
                "type_name": "INT",
                "modeling": "exact",
                "reads": ["COUNT_DR.CNT"],
                "operands": [],
            },
            {
                "id": "c.divisor",
                "kind": "variable",
                "type_name": "INT",
                "modeling": "exact",
                "reads": ["COUNT_DR.DIVISOR"],
                "operands": [],
            },
        ],
        symbols=[
            {
                "qualified_name": "COUNT_DR.UP",
                "name": "UP",
                "role": "input",
                "type_name": "BOOL",
            },
            {
                "qualified_name": "COUNT_DR.DIVISOR",
                "name": "DIVISOR",
                "role": "input",
                "type_name": "INT",
            },
            {
                "qualified_name": "COUNT_DR.last_up",
                "name": "last_up",
                "role": "state",
                "type_name": "BOOL",
            },
            {
                "qualified_name": "COUNT_DR.CNT",
                "name": "CNT",
                "role": "state",
                "type_name": "INT",
            },
        ],
        dependencies={
            "c.mod.violation": [
                "COUNT_DR.UP",
                "COUNT_DR.DIVISOR",
                "COUNT_DR.last_up",
                "COUNT_DR.CNT",
            ]
        },
        persistent=["COUNT_DR.last_up", "COUNT_DR.CNT"],
    )

    store_8 = pou(
        "STORE_8",
        [
            {"id": "s.entry"},
            {"id": "s.outer"},
            {"id": "s.inner"},
            {"id": "s.exit"},
        ],
        [
            {
                "id": "s.outer.eval",
                "from": "s.entry",
                "to": "s.outer",
                "kind": "evaluation",
                "guard": "s.outer.expr",
            },
            {
                "id": "s.inner.eval",
                "from": "s.outer",
                "to": "s.inner",
                "kind": "evaluation",
                "guard": "s.inner.expr",
                "transfer": {
                    "reads": ["STORE_8.STATE"],
                    "writes": ["STORE_8.STATE"],
                    "versions_in": {"STORE_8.STATE": 2},
                    "versions_out": {"STORE_8.STATE": 3},
                    "operations": [{"kind": "nested_state_update"}],
                    "modeling": "conservative",
                },
            },
            {"id": "s.flow", "from": "s.inner", "to": "s.exit", "kind": "flow"},
        ],
        [
            target("s.outer.target", "s.outer", "branch_outcome"),
            target("s.inner.target", "s.inner", "case_outcome"),
            target("s.property", "s.exit", "property_violation", "conservative"),
        ],
        expressions=[
            {
                "id": "s.outer.expr",
                "kind": "equal",
                "modeling": "exact",
                "reads": ["STORE_8.MODE"],
                "operands": [],
            },
            {
                "id": "s.inner.expr",
                "kind": "equal",
                "modeling": "conservative",
                "reads": ["STORE_8.STATE"],
                "operands": [],
            },
        ],
        symbols=[
            {
                "qualified_name": "STORE_8.MODE",
                "name": "MODE",
                "role": "input",
                "type_name": "USINT",
            },
            {
                "qualified_name": "STORE_8.STATE",
                "name": "STATE",
                "role": "state",
                "type_name": "USINT",
            },
        ],
        dependencies={"s.property": ["STORE_8.MODE", "STORE_8.STATE"]},
        persistent=["STORE_8.STATE"],
    )

    scheduler_2 = pou(
        "SCHEDULER_2",
        [
            {"id": "q.entry"},
            {
                "id": "q.mod",
                "hazard": {"kind": "modulo", "expression_id": "q.mod.expr"},
            },
        ],
        [
            {
                "id": "q.eval",
                "from": "q.entry",
                "to": "q.mod",
                "kind": "evaluation",
                "guard": "q.mod.expr",
            }
        ],
        [
            target("q.mod.reach", "q.mod", "hazard_reach"),
            target("q.mod.violation", "q.mod", "hazard_violation"),
        ],
        expressions=[
            {
                "id": "q.mod.expr",
                "kind": {"kind": "binary", "operator": "MOD"},
                "type_name": "INT",
                "modeling": "exact",
                "reads": ["SCHEDULER_2.CNT", "SCHEDULER_2.N"],
                "operands": ["q.cnt", "q.n"],
            },
            {
                "id": "q.cnt",
                "kind": {"kind": "variable", "qualified_name": "SCHEDULER_2.CNT"},
                "type_name": "INT",
                "modeling": "exact",
                "reads": ["SCHEDULER_2.CNT"],
                "operands": [],
            },
            {
                "id": "q.n",
                "kind": {"kind": "variable", "qualified_name": "SCHEDULER_2.N"},
                "type_name": "INT",
                "modeling": "exact",
                "reads": ["SCHEDULER_2.N"],
                "operands": [],
            },
        ],
        symbols=[
            {
                "qualified_name": "SCHEDULER_2.CNT",
                "name": "CNT",
                "role": "input",
                "type_name": "INT",
            },
            {
                "qualified_name": "SCHEDULER_2.N",
                "name": "N",
                "role": "input",
                "type_name": "INT",
            },
        ],
        dependencies={
            "q.mod.violation": ["SCHEDULER_2.CNT", "SCHEDULER_2.N"]
        },
    )

    exploration = pou(
        "OPAQUE_EXPLORATION",
        [{"id": "o.entry"}, {"id": "o.target"}],
        [{"id": "o.flow", "from": "o.entry", "to": "o.target", "kind": "flow"}],
        [target("o.loop", "o.target", "loop_outcome", "opaque")],
    )

    model = {
        "schema_version": "semantist.stg/1.2.0",
        "pous": [function, count_dr, store_8, scheduler_2, exploration],
    }
    target_ids = [
        item["id"]
        for item_pou in model["pous"]
        for item in item_pou["targets"]
    ]
    runtime_ids = {
        "schema_version": "semantist.runtime-ids/1.0.0",
        "entries": [
            {"stable_id": stable_id, "runtime_id": index}
            for index, stable_id in enumerate(target_ids, 1)
        ],
    }
    return model, runtime_ids


def test_plan_extracts_function_guide_and_preserves_stable_ids():
    model, runtime_ids = semantic_fixture()
    plan = SemanticTaskPlanner(model, runtime_ids).build()
    task = next(task for task in plan["tasks"] if task["terminal_target_id"] == "f.property")
    assert task["ordered_waypoints"] == ["f.true.target", "f.property"]
    assert guide_target(task["ordered_waypoints"], []) == "f.true.target"
    assert guide_target(task["ordered_waypoints"], ["f.true.target"]) == "f.property"
    assert task["controllable_seed_fields"][0]["seed_field"] == "X"
    runtime_stable_ids = {entry["stable_id"] for entry in runtime_ids["entries"]}
    assert set(task["ordered_waypoints"]) <= runtime_stable_ids


def test_count_dr_multicycle_state_and_mod_hazard_prerequisite():
    model, runtime_ids = semantic_fixture()
    plan = SemanticTaskPlanner(model, runtime_ids).build()
    task = next(
        task for task in plan["tasks"] if task["terminal_target_id"] == "c.mod.violation"
    )
    assert task["hazard_prerequisites"] == ["c.mod.reach"]
    assert task["ordered_waypoints"][-2:] == ["c.mod.reach", "c.mod.violation"]
    assert task["terminal_hazard"] == "modulo_by_zero"
    assert task["terminal_goal_expression"]["kind"] == "modulo"
    assert {"COUNT_DR.last_up", "COUNT_DR.CNT"} <= {
        symbol
        for requirement in task["temporal_requirements"]
        for symbol in requirement["persistent_symbols"]
    }
    transfer = task["state_transfer_chains"][0]
    assert transfer["writes"] == ["COUNT_DR.CNT", "COUNT_DR.last_up"]


def test_semantic_task_plan_defaults_to_compact_json(tmp_path, monkeypatch):
    monkeypatch.delenv("SEMANTIST_SEMANTIC_PLAN_PRETTY", raising=False)
    model, runtime_ids = semantic_fixture()
    path = tmp_path / "semantic-task-plan.json"

    SemanticTaskPlanner(model, runtime_ids).write(path)

    text = path.read_text(encoding="utf-8")
    assert json.loads(text)["schema_version"] == "semantist.semantic-task-plan/1.0.0"
    assert "\n  " not in text


def test_semantic_task_plan_pretty_json_is_debug_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIST_SEMANTIC_PLAN_PRETTY", "1")
    model, runtime_ids = semantic_fixture()
    path = tmp_path / "semantic-task-plan.json"

    SemanticTaskPlanner(model, runtime_ids).write(path)

    text = path.read_text(encoding="utf-8")
    assert json.loads(text)["schema_version"] == "semantist.semantic-task-plan/1.0.0"
    assert "\n  " in text


def test_semantic_task_plan_write_skips_unchanged_contents(tmp_path, monkeypatch):
    monkeypatch.delenv("SEMANTIST_SEMANTIC_PLAN_PRETTY", raising=False)
    model, runtime_ids = semantic_fixture()
    path = tmp_path / "semantic-task-plan.json"
    planner = SemanticTaskPlanner(model, runtime_ids)

    planner.write(path)
    first_mtime = path.stat().st_mtime_ns
    planner.write(path)

    assert path.stat().st_mtime_ns == first_mtime


def test_transfer_operations_embed_typed_expression_dags_for_bounded_state_solving():
    model, runtime_ids = semantic_fixture()
    count_dr = next(
        item for item in model["pous"] if item["pou"]["qualified_name"] == "COUNT_DR"
    )
    transfer = next(
        edge["transfer"]
        for edge in count_dr["graph"]["edges"]
        if edge["id"] == "c.transfer"
    )
    transfer["operations"] = [
        {
            "kind": "assign",
            "target": "c.cnt",
            "target_symbol": "COUNT_DR.CNT",
            "value": "c.divisor",
        }
    ]
    plan = SemanticTaskPlanner(model, runtime_ids).build()
    task = next(
        item for item in plan["tasks"] if item["terminal_target_id"] == "c.mod.violation"
    )
    operation = task["state_transfer_chains"][0]["operations"][0]
    assert operation["target_expression"]["kind"] == "variable"
    assert operation["value_expression"]["kind"] == "variable"
    assert operation["value_expression"]["reads"] == ["COUNT_DR.DIVISOR"]


def test_store_8_nested_state_path_and_modeling_are_preserved():
    model, runtime_ids = semantic_fixture()
    plan = SemanticTaskPlanner(model, runtime_ids).build()
    task = next(task for task in plan["tasks"] if task["terminal_target_id"] == "s.property")
    assert task["ordered_waypoints"] == [
        "s.outer.target",
        "s.inner.target",
        "s.property",
    ]
    assert task["state_transfer_chains"][0]["operations"] == [
        {"kind": "nested_state_update"}
    ]
    assert task["modeling_status"] == "conservative"
    opaque = next(task for task in plan["tasks"] if task["terminal_target_id"] == "o.loop")
    assert opaque["task_kind"] == "exploration"
    assert opaque["modeling_status"] == "opaque"


def test_scheduler_2_mod_reach_precedes_exact_violation():
    model, runtime_ids = semantic_fixture()
    plan = SemanticTaskPlanner(model, runtime_ids).build()
    task = next(
        task for task in plan["tasks"] if task["terminal_target_id"] == "q.mod.violation"
    )
    assert task["pou"] == "SCHEDULER_2"
    assert task["ordered_waypoints"] == ["q.mod.reach", "q.mod.violation"]
    assert task["hazard_prerequisites"] == ["q.mod.reach"]
    assert task["terminal_hazard"] == "modulo"
    assert task["terminal_goal_expression"]["kind"]["operator"] == "MOD"
    assert task["modeling_status"] == "exact"


def test_unmapped_violation_keeps_mapped_hazard_reach_explorable():
    model, runtime_ids = semantic_fixture()
    mapped = {
        entry["stable_id"]
        for entry in runtime_ids["entries"]
        if entry["stable_id"] != "q.mod.violation"
    }
    plan = SemanticTaskPlanner(model, runtime_ids, mapped).build()
    violation = next(
        task for task in plan["tasks"] if task["terminal_target_id"] == "q.mod.violation"
    )
    reach = next(
        task
        for task in plan["tasks"]
        if task["terminal_target_id"] == "q.mod.reach"
        and task["task_kind"] == "exploration"
    )
    assert violation["initial_state"] == "unmapped"
    assert reach["initial_state"] == "ready"


def test_seed_profile_uses_observer_events_not_seed_text():
    model, runtime_ids = semantic_fixture()
    plan = SemanticTaskPlanner(model, runtime_ids).build()
    profile = build_seed_profile(
        b"c.mod.violation,BOOL,1\n0.UP,BOOL,1\n",
        [
            {"target_id": "c.cycle", "cycle": 0, "state_signature": "state-a"},
            {"target_id": "c.mod.reach", "cycle": 1, "state_signature": "state-b"},
        ],
        parent_id="parent",
        tasks=plan["tasks"],
    )
    assert profile.covered_target_ids == ["c.cycle", "c.mod.reach"]
    assert "c.mod.violation" not in profile.covered_target_ids
    assert profile.cycle_ids == [0, 1]
    assert profile.parent_id == "parent"


def test_multiple_paths_scc_ranking_decay_and_deferred_reactivation():
    graph = {
        "nodes": [{"id": node} for node in ["entry", "loop.a", "loop.b", "short", "end"]],
        "edges": [
            {"id": "e1", "from": "entry", "to": "loop.a"},
            {"id": "e2", "from": "loop.a", "to": "loop.b"},
            {"id": "e3", "from": "loop.b", "to": "loop.a"},
            {"id": "e4", "from": "loop.b", "to": "end"},
            {"id": "e5", "from": "entry", "to": "short"},
            {"id": "e6", "from": "short", "to": "end"},
        ],
    }
    components = strongly_connected_components(
        [node["id"] for node in graph["nodes"]], graph["edges"]
    )
    assert ["loop.a", "loop.b"] in components
    paths = bounded_paths(graph, "entry", "end")
    assert len(paths) == 2
    assert all(len(path["node_ids"]) <= len(graph["nodes"]) for path in paths)

    task = {
        "task_id": "task-a",
        "task_kind": "violation",
        "pou": "P",
        "terminal_target_id": "violation",
        "candidate_paths": [
            {"target_ids": ["a", "b", "violation"]},
            {"target_ids": ["short", "violation"]},
        ],
        "ordered_waypoints": ["a", "b", "violation"],
        "control_obligations": [],
        "data_dependencies": [],
        "state_transfer_chains": [],
        "temporal_requirements": [],
        "hazard_prerequisites": [],
        "modeling_status": "exact",
        "unlocked_targets": ["violation"],
    }
    assert choose_active_path(task, []) == ["short", "violation"]
    seed = SeedProfile(content_hash="seed")
    undecayed = rank_tasks([task], [seed])[0]
    decayed = rank_tasks([task], [seed], failed_attempts={"task-a": 4})[0]
    assert decayed["priority"] < undecayed["priority"]

    runtime = TaskRuntime(task_id="task-a", status="active")
    defer_task(runtime, coverage_epoch=7)
    assert runtime.status == "deferred"
    refresh_task_runtime(
        runtime,
        task,
        ["new-target"],
        coverage_epoch=8,
        mapped_target_ids=["violation"],
    )
    assert runtime.status == "ready"
    assert asdict(seed)["content_hash"] == "seed"
