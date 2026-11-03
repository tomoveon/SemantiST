import json
import subprocess
from pathlib import Path

import pytest

from compiler.instrumentation.compile import write_call_effect_model
from fuzzer.pipeline.goal_tasks import build_goal_tasks, cycle_effects, write_goal_tasks


ROOT = Path(__file__).resolve().parents[2]


def test_stg_extracts_transitive_pure_function_effects(tmp_path):
    binary = ROOT / "artifacts" / "cargo-target" / "release" / "semantist-stg"
    if not binary.is_file():
        pytest.skip("semantist-stg release binary is unavailable")
    (tmp_path / "effect_chain.st").write_text("""
FUNCTION DIFFERENT_LEAF : DINT
VAR_INPUT VALUE : DINT; END_VAR
DIFFERENT_LEAF := VALUE + 1;
END_FUNCTION

FUNCTION DIFFERENT_MIDDLE : DINT
VAR_INPUT VALUE : DINT; END_VAR
DIFFERENT_MIDDLE := DIFFERENT_LEAF(VALUE);
END_FUNCTION

FUNCTION DIFFERENT_ROOT : DINT
VAR_INPUT VALUE : DINT; END_VAR
DIFFERENT_ROOT := DIFFERENT_MIDDLE(VALUE);
END_FUNCTION
""", encoding="utf-8")
    project = tmp_path / "plc.json"
    project.write_text(json.dumps({"name": "effect_chain", "files": ["effect_chain.st"],
                                   "compile_type": "IR", "output": "effect.ll",
                                   "libraries": []}), encoding="utf-8")

    output = tmp_path / "stg"
    subprocess.run([str(binary), "--project-root", str(tmp_path), "--output", str(output),
                    "--pretty", "false", "--pou", "DIFFERENT_ROOT", str(project)],
                   check=True, capture_output=True, timeout=20)
    root_model = json.loads((output / "stg-model.json").read_text(encoding="utf-8"))
    assert len(root_model["pous"]) == 1
    model = write_call_effect_model(config_path=project, stg_output=output,
                                    function="DIFFERENT_ROOT", project_root=tmp_path,
                                    budget_s=30, executable=binary)
    assert model == json.loads((output / "stg-effect-model.json").read_text(encoding="utf-8"))
    assert len(model["pous"]) == 3
    assert model["effect_resolution"]["status"] == "COMPLETE"
    abi = {"target": {"name": "DIFFERENT_ROOT", "kind": "FUNCTION"},
           "frame_fields": [], "shared_fields": [], "state_fields": []}
    effects = cycle_effects(abi, model)
    assert effects["mode"] == "single_cycle"
    tasks = build_goal_tasks({"tasks": [{"task_id": "renamed-risk", "terminal_kind": "branch",
                                        "candidate_paths": [], "control_obligations": []}]},
                             abi, {"regions": []}, stg_model=root_model, effect_model=model)
    assert tasks["single_cycle"]
    plan_path = tmp_path / "semantic-task-plan.json"
    abi_path = tmp_path / "abi.json"
    layout_path = tmp_path / "state-layout.json"
    plan_path.write_text(json.dumps({"tasks": [{"task_id": "renamed-risk",
                                                "terminal_kind": "branch",
                                                "candidate_paths": [],
                                                "control_obligations": []}]}), encoding="utf-8")
    abi_path.write_text(json.dumps(abi), encoding="utf-8")
    layout_path.write_text(json.dumps({"regions": []}), encoding="utf-8")
    written = write_goal_tasks(plan_path, abi_path, layout_path,
                               tmp_path / "goal-tasks.json", stg_model_path=output / "stg-model.json",
                               stg_effect_model_path=output / "stg-effect-model.json")
    assert written["single_cycle"]
    assert written["cycle_effects"]["effect_resolution"]["status"] == "COMPLETE"
