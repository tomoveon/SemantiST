"""Frozen build and pre-search planning inputs for independent trials."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fuzzer.experiments.matrix import ROOT
from fuzzer.llm.config import load_config
from fuzzer.llm.oracle_synthesis import synthesize_oracle
from fuzzer.pipeline.campaign import Campaign
from fuzzer.pipeline.plan_batches import PLANNER_SYSTEM_MESSAGE, run_plan_batches


BUILD_INPUT_DIRS = ("compiler", "fuzzer/engine", "fuzzer/llm", "fuzzer/pipeline", "fuzzer/corpus",
                    "fuzzer/runtime", "fuzzer/semantic", "fuzzer/stages", "fuzzer/scheduler")
BUILD_INPUT_SUFFIXES = {".py", ".rs", ".c", ".h", ".sh", ".st", ".json", ".toml"}
BUILD_CONFIGURATION = {"engine": "rust-v3", "instrumentation": "stg-ir", "profile": "release"}


def build_input_stats(target: dict) -> dict[str, list[int]]:
    inputs = {ROOT / target["st_file"], ROOT / "Cargo.lock", ROOT / "Dockerfile"}
    for key in ("source_file", "normalized_file", "compatibility_manifest", "property_file"):
        if target.get(key):
            inputs.add(ROOT / target[key])
    if target.get("compatibility_manifest"):
        project = (ROOT / target["compatibility_manifest"]).parent
        inputs.update(path for path in project.rglob("*") if path.is_file())
    for directory in BUILD_INPUT_DIRS:
        inputs.update(path for path in (ROOT / directory).rglob("*")
                      if path.is_file() and path.suffix in BUILD_INPUT_SUFFIXES)
    return {str(path.relative_to(ROOT)): [path.stat().st_size, path.stat().st_mtime_ns]
            for path in sorted(inputs)}


def validate_build_inputs(package: Path, target: dict) -> None:
    manifest = json.loads((package / "build.json").read_text(encoding="utf-8"))
    if (manifest.get("target") != target
            or manifest.get("build_configuration") != BUILD_CONFIGURATION
            or manifest.get("input_stats") != build_input_stats(target)):
        raise RuntimeError(f"build inputs changed for {target['id']}; use a new experiment root and rebuild")


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _seal_tree(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o555 if executable else 0o444)
    for path in sorted((item for item in directory.rglob("*")
                        if item.is_dir() and not item.is_symlink()),
                       key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555)
    directory.chmod(0o555)


def build_path(root: Path, target_id: str) -> Path:
    return root / "builds" / target_id / "semantist"


def plan_path(root: Path, target_id: str) -> Path:
    return root / "plans" / target_id / "semantist"


def _copy_template(build: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"run directory already exists: {destination}")
    shutil.copytree(build / "template", destination)
    for path in (destination, *(item for item in destination.rglob("*") if item.is_dir())):
        path.chmod(0o755)
    for name in ("metrics", "findings", "unified-corpus/seeds"):
        (destination / name).mkdir(parents=True, exist_ok=True)


def prepare_build(root: Path, target: dict, *, timeout_s: int = 86400) -> Path:
    package = build_path(root, target["id"])
    if (package / "build.json").is_file():
        validate_build_inputs(package, target)
        return package
    if package.exists():
        raise FileExistsError(f"incomplete build package: {package}")
    staging = package.with_name(package.name + ".building")
    if staging.exists():
        raise FileExistsError(f"incomplete build staging directory: {staging}")
    staging.mkdir(parents=True)
    input_stats = build_input_stats(target)
    # Network and Building exports have a normalized form with cross-library
    # qualifiers flattened for RuSTy's single-project compiler.  Use it for
    # the compiler input while retaining the original target in the package.
    source = ROOT / target.get("normalized_file", target["st_file"])
    command = [sys.executable, "-m", "fuzzer.pipeline.core", "--st-file", str(source),
               "--function", target["function"], "--run-dir", str(staging / "run"),
               "--build-timeout", str(timeout_s), "--build-only", "--skip-report"]
    if target.get("compatibility_manifest"):
        command.extend(["--compatibility-manifest",
                        str(ROOT / target["compatibility_manifest"])])
    env = os.environ.copy()
    env["SEMANTIST_ARTIFACT_DIR"] = str(staging / "artifacts")
    env["SEMANTIST_BUILD_COMMON"] = "1"
    started = time.monotonic()
    with (staging / "build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                stderr=subprocess.STDOUT,
                                check=False)
    if result.returncode:
        raise RuntimeError(f"build failed for {target['id']}; see {staging / 'build.log'}")
    if input_stats != build_input_stats(target):
        raise RuntimeError(f"build inputs changed while constructing {target['id']}")
    target_builds = list((staging / "artifacts" / "build" / "targets").iterdir())
    if (len(target_builds) != 1 or not (target_builds[0] / "fuzz_target").is_file()
            or not (target_builds[0] / "common" / "fuzz_target").is_file()):
        raise RuntimeError(f"build did not produce a unique target executable: {staging}")
    shutil.move(str(target_builds[0]), str(staging / "build"))
    template = staging / "template"
    template.mkdir()
    for name in ("stg", "bundle", "semantic-task-plan.json", "unified-corpus"):
        source_path = staging / "run" / name
        if source_path.exists():
            shutil.move(str(source_path), str(template / name))
    frozen_source = staging / "source"
    frozen_source.mkdir()
    shutil.copy2(source, frozen_source / "target.st")
    if target.get("property_file"):
        shutil.copy2(ROOT / target["property_file"], frozen_source / "output-properties.json")
    original_source = None
    if target.get("source_file"):
        original = ROOT / target["source_file"]
        original_source = "source/original-library.st"
        shutil.copy2(original, staging / original_source)
    if target.get("normalized_file"):
        shutil.copy2(ROOT / target["normalized_file"], frozen_source / "normalized.st")
    compatibility_model = None
    if target.get("compatibility_manifest"):
        manifest = ROOT / target["compatibility_manifest"]
        shutil.copytree(manifest.parent, frozen_source / "compatibility-project")
        compatibility_model = f"source/compatibility-project/{manifest.name}"
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              text=True, capture_output=True, check=False).stdout.strip()
    write_json(staging / "build.json", {
        "schema_version": "semantist.experiment-build/1",
        "target": target,
        "build_configuration": BUILD_CONFIGURATION,
        "input_stats": input_stats,
        "source_revision": revision,
        "source_file": "source/target.st",
        "original_source": original_source,
        "normalized_source": "source/target.st",
        "declared_normalized_source": "source/normalized.st" if target.get("normalized_file") else None,
        "compatibility_model": compatibility_model,
        "abi": "template/bundle/abi.json",
        "harness": "build/harness.c",
        "common_harness": "build/common/harness.c",
        "common_binary": "build/common/fuzz_target",
        "semantic_sequence_adapter": "compiler/harness/semantist_adapter.py",
        "baseline_input_format": "common-raw",
        "instrumentation": "stg-ir",
        "baseline_instrumentation": "edge-only",
        "tool": "semantist",
        "build_seconds": round(time.monotonic() - started, 3),
        "tool_versions": {
            "python": sys.version.split()[0],
            "rustc": _version("rustc"),
            "clang": _version("clang"),
            "klee": _version("klee"),
            "rusty": _version(os.environ.get("RUSTY_COMPILER", "/opt/rusty-semantic/target/release/plc")),
        },
        "image": os.environ.get("SEMANTIST_EXPERIMENT_IMAGE", "unknown"),
    })
    _seal_tree(staging)
    staging.rename(package)
    return package


def _version(command: str) -> str:
    try:
        result = subprocess.run([command, "--version"], text=True, capture_output=True,
                                timeout=5, check=False)
        return result.stdout.splitlines()[0] if result.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "unavailable"


def _prepare_checked_binary(root: Path, target: dict, build: Path, staging: Path) -> Path:
    checked = staging / "checked-build"
    checked.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "CARGO_TARGET_DIR": str(root / "cargo-target"),
        "SEMANTIST_ARTIFACT_DIR": str(staging / "checked-artifacts"),
        "SEMANTIST_BUILD_ARTIFACT_DIR": str(checked),
        "SEMANTIST_STG_DIR": str(build / "template" / "stg"),
        "SEMANTIST_ORACLE_SPEC": str(staging / "generated-oracle.json"),
        "SEMANTIST_SYMBOLIC": "0",
        "SEMANTIST_BUILD_COMMON": "0",
        "SEMANTIST_INPROCESS_LIB": str(root / "cargo-target" / "release" / "libsemantist_inprocess.a"),
        "OUT_LL": str(build / "build" / "target.ll"),
        "PREPARED_LL": str(checked / "target.prepared.ll"),
        "STATE_LL": str(checked / "target.prepared.state.ll"),
        "OUT_OBJ": str(checked / "target.o"),
        "OUT_BIN": str(checked / "fuzz_target"),
        "HARNESS_OUT": str(checked / "harness.c"),
    })
    if target.get("compatibility_manifest"):
        env["SEMANTIST_COMPATIBILITY_MANIFEST"] = str(ROOT / target["compatibility_manifest"])
    started = time.monotonic()
    with (staging / "checked-build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(["./compiler/scripts/build_target.sh",
                                 str(build / "source" / "target.st"), target["function"]],
                                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                check=False)
    if result.returncode:
        raise RuntimeError(f"generated checker build failed for {target['id']}; see {staging / 'checked-build.log'}")
    for name in ("abi.json", "state-layout.json"):
        original = json.loads((build / "template" / "bundle" / name).read_text(encoding="utf-8"))
        generated = json.loads((checked / "bundle" / name).read_text(encoding="utf-8"))
        if original != generated:
            raise RuntimeError(f"generated checker changed the {name} execution contract")
    if not (checked / "semantist-inprocess").is_file():
        raise RuntimeError("generated checker build lacks the in-process engine")
    return checked


def prepare_plan(root: Path, target: dict, *, timeout_s: int | None = None,
                 max_calls: int = 40, token_budget: int = 20000) -> Path:
    if not load_config().available:
        raise RuntimeError("LLM endpoint, key, or model is not configured")
    build = build_path(root, target["id"])
    if not (build / "build.json").is_file():
        raise FileNotFoundError(f"build package missing for {target['id']}: {build}")
    validate_build_inputs(build, target)
    package = plan_path(root, target["id"])
    if (package / "plan.json").is_file():
        return package
    if package.exists():
        raise FileExistsError(f"incomplete planning package: {package}")
    staging = package.with_name(package.name + ".planning")
    if staging.exists():
        raise FileExistsError(f"incomplete planning staging directory: {staging}")
    staging.parent.mkdir(parents=True, exist_ok=True)
    _copy_template(build, staging)
    oracle_started = time.monotonic()
    oracle_summary = synthesize_oracle(root, target, build, staging)
    oracle_seconds = round(time.monotonic() - oracle_started, 3)
    checker_started = time.monotonic()
    target_binary = (build / "build" / "fuzz_target")
    if oracle_summary["properties"]:
        target_binary = _prepare_checked_binary(root, target, build, staging) / "fuzz_target"
    checker_seconds = round(time.monotonic() - checker_started, 3)
    config = {
        "engine": "rust", "contract_id": _contract_id(staging),
        "ablations": {"llm": True}, "llm_max_calls": max_calls,
        "llm_plan_rounds": 5, "llm_plan_batch_size": 3,
        "llm_plan_token_budget": token_budget,
        "llm_strict_planning": True,
        "llm_preparation_unbounded": timeout_s is None,
        "llm_cache_dirs": [str(staging / "llm" / "cache")],
        "generated_oracle_file": str(staging / "generated-oracle.json"),
    }
    if target.get("property_file"):
        frozen_property = build / "source" / "output-properties.json"
        if not frozen_property.is_file():
            raise FileNotFoundError(f"frozen output property missing: {frozen_property}")
        config["property_file"] = str(frozen_property)
    campaign = Campaign(staging, staging / "bundle", target_binary,
                        function=target["function"], st_file=ROOT / target["st_file"],
                        config=config, time_budget_s=timeout_s or 86400, jobs=2, seed=0,
                        max_evaluations=0)
    started = time.monotonic()
    planning_deadline = started + timeout_s if timeout_s is not None else float("inf")
    campaign.startup_deadline = planning_deadline
    campaign.planned_deadline = planning_deadline
    campaign.deadline = planning_deadline
    planning_retried = False
    try:
        run_plan_batches(campaign)
    except RuntimeError as error:
        if "no native-valid plan" not in str(error):
            raise
        planning_retried = True
        campaign.config["llm_plan_token_budget"] = max(60_000, token_budget * 3)
        run_plan_batches(campaign)
    planning_seconds = round(time.monotonic() - started, 3)
    if campaign.metrics.summary().get("llm_calls", 0) < 1:
        raise RuntimeError(f"{target['id']} has no real LLM planning call")
    offline_started = time.monotonic()
    campaign.max_evaluations = 2
    campaign.symbolic_startup_deadline = min(planning_deadline, offline_started + 20.0)
    campaign.run_symbolic_startup()
    offline_solver_seconds = round(time.monotonic() - offline_started, 3)
    seeds = staging / "unified-corpus" / "seeds"
    seeds.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(sorted((staging / "solver" / "reinject").glob("*.seed")), start=1):
        shutil.copy2(path, seeds / f"offline-solver-{index:04}.seed")
    campaign.deadline = time.monotonic() + 60
    review_started = time.monotonic()
    campaign._run_confirmation()
    planning_review_seconds = round(time.monotonic() - review_started, 3)
    campaign.metrics.write_summary()
    campaign.engine_status = "NOT_RUN"
    campaign._write_report()
    write_json(staging / "plan.json", {
        "schema_version": "semantist.experiment-plan/1",
        "target_id": target["id"],
        "plan_id": f"{target['id']}-frozen-plan",
        "summary": campaign.llm_planning_summary,
        "symbolic_suggestions": campaign.llm_symbolic_suggestions,
        "solver_hints": campaign.llm_solver_hints,
        "planning_seconds": planning_seconds,
        "oracle_generation_seconds": oracle_seconds,
        "checker_build_seconds": checker_seconds,
        "oracle_generation": oracle_summary,
        "checked_binary": ("checked-build/fuzz_target" if oracle_summary["properties"] else None),
        "planning_retried": planning_retried,
        "offline_solver_seconds": offline_solver_seconds,
        "offline_solver_results": [
            {key: value for key, value in result.items() if key != "candidate_file"}
            for result in campaign.symbolic_results
        ],
        "planning_review_seconds": planning_review_seconds,
        "tokens": campaign.metrics.summary().get("llm_tokens", 0),
        "calls": campaign.metrics.summary().get("llm_calls", 0),
        "planner_prompt": PLANNER_SYSTEM_MESSAGE,
        "model": load_config().model,
        "seed_files": sorted(path.name for path in seeds.glob("*.seed")),
        "planning_confirmed": campaign.confirmed,
    })
    _seal_tree(staging)
    staging.rename(package)
    return package


def _contract_id(run_dir: Path) -> str | None:
    path = run_dir / "bundle" / "execution-contract.json"
    return json.loads(path.read_text(encoding="utf-8")).get("contract_id")


def prepare_external_build(root: Path, target: dict, tool_name: str,
                           adapter: dict, *, timeout_s: int | None = None) -> Path:
    """Run a configured external builder; it must describe its real support."""
    package = root / "builds" / target["id"] / tool_name
    if (package / "build-result.json").is_file():
        return package
    command = adapter.get("build_command")
    if not isinstance(command, list) or not command:
        raise RuntimeError(f"{tool_name} build adapter is not configured")
    common = build_path(root, target["id"])
    if not (common / "build.json").is_file():
        raise FileNotFoundError(f"common replay build is missing: {common}")
    validate_build_inputs(common, target)
    staging = package.with_name(package.name + ".building")
    if package.exists() or staging.exists():
        raise FileExistsError(f"existing incomplete external build: {package}")
    staging.mkdir(parents=True)
    normalized = common / "source" / "normalized.st"
    manifest = json.loads((common / "build.json").read_text(encoding="utf-8"))
    values = {"common_build": str(common),
              "source": str(common / "source" / "target.st"),
              "normalized_source": str(normalized if normalized.is_file()
                                       else common / "source" / "target.st"),
              "original_source": str(common / (manifest.get("original_source") or "source/target.st")),
              "function": target["function"], "output": str(staging),
              "compatibility_manifest": str(common / (manifest.get("compatibility_model") or "source/target.st")),
              "abi": str(common / "template" / "bundle" / "abi.json"),
              "contract": str(common / "template" / "bundle" / "execution-contract.json"),
              "initial_corpus": str(common / "template" / "unified-corpus" / "seeds")}
    argv = [str(token).format_map(values) for token in command]
    started = time.monotonic()
    with (staging / "build.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=timeout_s, check=False)
    result_file = staging / "build-result.json"
    if not result_file.is_file():
        write_json(result_file, {"status": "failed", "reason":
                   f"adapter exited {completed.returncode} without build-result.json"})
    result = json.loads(result_file.read_text(encoding="utf-8"))
    result["tool"] = tool_name
    result["target_id"] = target["id"]
    result["build_seconds"] = round(time.monotonic() - started, 3)
    if completed.returncode:
        result["status"] = "failed"
        result["reason"] = f"adapter exited {completed.returncode}: {result.get('reason', '')}"
    write_json(result_file, result)
    _seal_tree(staging)
    staging.rename(package)
    return package
