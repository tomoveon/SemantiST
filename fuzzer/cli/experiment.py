"""Prepare, schedule, and summarize the six experiment comparisons."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fuzzer.experiments.artifacts import (build_path, plan_path, prepare_build,
                                          prepare_external_build, prepare_plan,
                                          validate_build_inputs, write_json)
from fuzzer.experiments.matrix import CONFIGURATIONS, Job, ROOT, make_matrix
from fuzzer.experiments.summary import summarize
from fuzzer.experiments.worker import result_path, run_job
from fuzzer.llm.config import load_config


def _physical_cpus(count: int) -> list[list[int]]:
    output = subprocess.run(["lscpu", "-p=CPU,CORE,SOCKET"], check=True,
                            capture_output=True, text=True).stdout
    allowed = os.sched_getaffinity(0)
    physical: dict[tuple[int, int], int] = {}
    for line in output.splitlines():
        if line.startswith("#") or not line:
            continue
        cpu, core, socket = map(int, line.split(",")[:3])
        if cpu in allowed:
            physical.setdefault((socket, core), cpu)
    available = list(physical.values())
    if len(available) < count * 2:
        raise RuntimeError(f"need {count * 2} distinct physical cores, have {len(available)}")
    return [available[index * 2:index * 2 + 2] for index in range(count)]


FORMAL_CPU_POOLS = {"semantist": list(range(0, 10)),
                    "aflpp": list(range(10, 20)),
                    "icsquartz": list(range(20, 30))}


def _validate_formal_root(root: Path) -> None:
    parent = (ROOT / "artifacts" / "experiments").resolve()
    if root.parent != parent or re.fullmatch(r"overall-\d{8}T\d{6}", root.name) is None:
        raise ValueError(f"formal overall root must be {parent}/overall-YYYYMMDDTHHMMSS")


def _formal_cpu_pairs(tool: str) -> list[list[int]]:
    requested = FORMAL_CPU_POOLS[tool]
    output = subprocess.run(["lscpu", "-p=CPU,CORE,SOCKET"], check=True,
                            capture_output=True, text=True).stdout
    topology = {int(parts[0]): (int(parts[2]), int(parts[1]))
                for line in output.splitlines() if line and not line.startswith("#")
                for parts in [line.split(",")]}
    if (not set(requested) <= os.sched_getaffinity(0)
            or len({topology.get(cpu) for cpu in requested}) != 10
            or None in {topology.get(cpu) for cpu in requested}):
        raise RuntimeError(f"{tool} requires ten available distinct physical CPUs: {requested}")
    return [requested[index:index + 2] for index in range(0, 10, 2)]


def _container_command(args: argparse.Namespace, action: str, *, cpu_ids: list[int],
                       extra: list[str]) -> list[str]:
    network = "none" if action == "run" else "host"
    source_mount = f"type=bind,src={ROOT},dst=/work/SemantiST"
    root_mount = f"type=bind,src={args.root},dst=/experiments"
    if action == "run":
        source_mount += ",readonly"
        root_mount += ",readonly"
    command = ["taskset", "-c", ",".join(map(str, cpu_ids)),
               args.engine, "run", "--rm", "--init", f"--network={network}",
               f"--cpus={len(cpu_ids)}",
               "--memory=" + args.memory,
               "--pids-limit=1024",
               "--mount", source_mount,
               "--mount", root_mount,
               "-w", "/work/SemantiST",
               "-e", "CARGO_TARGET_DIR=/experiments/cargo-target",
               "-e", f"SEMANTIST_EXPERIMENT_IMAGE={args.image}"]
    mutation_mode = getattr(args, "mutation_mode", None)
    if action == "run" and mutation_mode:
        command.extend(["-e", f"SEMANTIST_MUTATION_MODE={mutation_mode}"])
    semantic_ratio = getattr(args, "semantic_ratio", None)
    if action == "run" and semantic_ratio is not None:
        command.extend(["-e", f"SEMANTIST_SEMANTIC_RATIO={semantic_ratio}"])
    if action == "run":
        request = json.loads((args.root / extra[extra.index("--request") + 1]).read_text(encoding="utf-8"))
        output = Path(request["output_dir"])
        if (output.is_absolute() or ".." in output.parts or not output.parts
                or output.parts[0] != "runs"
                or output != Path("runs") / request["job_id"]):
            raise ValueError(f"invalid trial output directory: {output}")
        writable = args.root / output.parent
        writable.mkdir(parents=True, exist_ok=True)
        command.extend(["--mount",
                        f"type=bind,src={writable},dst=/experiments/{output.parent}"])
    if args.llm_env_file and action == "plan":
        command.extend(["--env-file", str(args.llm_env_file)])
    command.extend([args.image, "python3", "-m", "fuzzer.cli.experiment",
                    "_worker", action, "--root", "/experiments", *extra])
    return command


def _invoke(args: argparse.Namespace, action: str, *, cpu_ids: list[int],
            extra: list[str], log: Path, timeout_s: int | None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    command = (_container_command(args, action, cpu_ids=cpu_ids, extra=extra)
               if not args.local else
               [sys.executable, "-m", "fuzzer.cli.experiment", "_worker", action,
                "--root", str(args.root), *extra])
    environment = os.environ.copy()
    if args.local:
        environment["CARGO_TARGET_DIR"] = str(args.root / "cargo-target")
    with log.open("w", encoding="utf-8") as output:
        try:
            completed = subprocess.run(command, cwd=ROOT, stdout=output,
                                       stderr=subprocess.STDOUT, env=environment,
                                       timeout=timeout_s,
                                       check=False)
            return completed.returncode
        except subprocess.TimeoutExpired:
            output.write(f"\nworker exceeded {timeout_s}s\n")
            return 124
        except OSError as error:
            output.write(f"\nworker launch failed: {error}\n")
            return 127


def _bootstrap(args: argparse.Namespace, cpus: list[int]) -> None:
    library = args.root / "cargo-target" / "release" / "libsemantist_inprocess.a"
    if library.is_file():
        return
    if args.local:
        raise RuntimeError("local mode requires a prebuilt CARGO_TARGET_DIR")
    command = ["taskset", "-c", ",".join(map(str, cpus)),
               args.engine, "run", "--rm", "--init", "--network=host",
               f"--cpus={len(cpus)}",
               "--memory=" + args.memory,
               "--mount", f"type=bind,src={ROOT},dst=/work/SemantiST",
               "--mount", f"type=bind,src={args.root},dst=/experiments",
               "-w", "/work/SemantiST", args.image, "bash", "-lc",
               "cp -a /opt/semantist/cargo-target/. /experiments/cargo-target/ && "
               "CARGO_TARGET_DIR=/experiments/cargo-target cargo build --workspace --release --locked"]
    (args.root / "cargo-target").mkdir(parents=True, exist_ok=True)
    log = args.root / "logs" / "bootstrap.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=ROOT, stdout=output,
                                stderr=subprocess.STDOUT, check=False)
    if result.returncode or not library.is_file():
        raise RuntimeError(f"common engine build failed; see {log}")


def _select(args: argparse.Namespace) -> list[Job]:
    frozen = args.root / "matrix.json"
    if frozen.is_file():
        records = json.loads(frozen.read_text(encoding="utf-8"))["jobs"]
        jobs = [Job(item["target"], item["tool"], item["configuration"],
                    item["seed"], item["online_seconds"], item["experiments"])
                for item in records]
    else:
        jobs = make_matrix()
    if args.experiment:
        jobs = [job for job in jobs if args.experiment in job.experiments]
    if args.targets:
        target_ids = {item.strip() for item in args.targets.split(",") if item.strip()}
        if not target_ids:
            raise ValueError("--targets must contain at least one target id")
        jobs = [job for job in jobs if job.target["id"] in target_ids]
    for field, expected in (("target", args.target), ("tool", args.tool),
                            ("configuration", args.configuration), ("seed", args.seed),
                            ("online_seconds", args.duration)):
        if expected is not None:
            jobs = [job for job in jobs if
                    (job.target["id"] if field == "target" else getattr(job, field)) == expected]
    if args.smoke_seconds is not None:
        jobs = [Job(job.target, job.tool, job.configuration, job.seed,
                    args.smoke_seconds, list(job.experiments)) for job in jobs]
    if args.limit is not None:
        jobs = jobs[:args.limit]
    return jobs


def _scheduled(root: Path) -> list[Job]:
    path = root / "scheduled-jobs.json"
    if not path.is_file():
        return []
    return [Job(item["target"], item["tool"], item["configuration"],
                item["seed"], item["online_seconds"], item["experiments"])
            for item in json.loads(path.read_text(encoding="utf-8"))["jobs"]]


def _remember_jobs(root: Path, jobs: list[Job]) -> list[Job]:
    combined = {job.job_id: job for job in _scheduled(root)}
    combined.update((job.job_id, job) for job in jobs)
    result = list(combined.values())
    write_json(root / "scheduled-jobs.json", {"schema_version": "semantist.experiment-scheduled/1",
                                               "jobs": [job.as_dict() for job in result]})
    return result


def _validate_execution(args: argparse.Namespace, jobs: list[Job]) -> None:
    ratio = getattr(args, "semantic_ratio", 0.30)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 100:
        raise ValueError("--semantic-ratio must be between 0 and 1, or 0 and 100 percent")
    if not args.execute:
        return
    if args.smoke_seconds is None and not args.allow_formal:
        raise ValueError("formal search requires --execute --allow-formal")
    if args.smoke_seconds is not None:
        if not 1 <= args.smoke_seconds <= 10:
            raise ValueError("smoke search is limited to 1-10 online seconds")
        max_targets = 6 if args.targets else 2
        if len({job.target["id"] for job in jobs}) > max_targets or len(jobs) > 6:
            raise ValueError(f"smoke search is limited to {max_targets} targets and six jobs")
    if not jobs:
        raise ValueError("no jobs match the selected filters")


def _validate_target_waves(args: argparse.Namespace, jobs: list[Job]) -> None:
    if args.schedule != "target_waves":
        return
    if args.experiment == "overall" and args.smoke_seconds is None:
        raise ValueError("formal overall runs require three --tool commands with --schedule standard")
    if (args.command != "run" or args.experiment != "overall" or args.jobs != 15
            or args.local or args.smoke_seconds is not None or len(jobs) != 1500):
        raise ValueError("target waves require the complete formal overall matrix and --jobs 15")
    groups: dict[str, set[tuple[str, int]]] = {}
    for job in jobs:
        if job.configuration != "full" or job.online_seconds != 300:
            raise ValueError("target waves require full configurations with 300-second searches")
        groups.setdefault(job.target["id"], set()).add((job.tool, job.seed))
    expected = {(tool, seed) for tool in ("semantist", "icsquartz", "aflpp")
                for seed in range(5)}
    if len(groups) != 100 or any(group != expected for group in groups.values()):
        raise ValueError("target waves require five repeats of all three tools for each target")


def _validate_formal_prerequisites(args: argparse.Namespace, jobs: list[Job],
                                   adapters: dict) -> None:
    if args.smoke_seconds is not None:
        return
    if args.local:
        raise ValueError("formal runs require container CPU and memory limits")
    missing = sorted({job.tool for job in jobs if job.tool != "semantist"
                      and not all((adapters.get(job.tool) or {}).get(key)
                                  for key in ("build_command", "run_command"))})
    if missing:
        raise RuntimeError("formal baseline adapters are not configured: " + ", ".join(missing))
    if (args.command == "prepare" or args.smoke_seconds is not None) and any(
            job.tool == "semantist" and CONFIGURATIONS[job.configuration]["llm"]
            for job in jobs):
        if args.llm_env_file is not None and not args.llm_env_file.is_file():
            raise FileNotFoundError(f"LLM environment file is missing: {args.llm_env_file}")
        if not load_config(args.llm_env_file).available:
            raise RuntimeError("formal SemantiST planning requires an LLM endpoint and key")


def _run_parallel(args: argparse.Namespace, items: list[tuple[str, list[str], Path, int | None]],
                  *, workers: int) -> dict[str, int]:
    if not items:
        return {}
    cpu_sets = (_formal_cpu_pairs(args.tool) if args.command == "run"
                and args.experiment == "overall" and args.smoke_seconds is None
                and args.tool in FORMAL_CPU_POOLS and len(items) >= 1
                else _physical_cpus(workers) if not args.local else [[0, 1]] * workers)
    cpu_sets = cpu_sets[:workers]
    available: queue.Queue[list[int]] = queue.Queue()
    for cpu_set in cpu_sets:
        available.put(cpu_set)
    results: dict[str, int] = {}
    def call(item: tuple[str, list[str], Path, int | None]) -> tuple[str, int]:
        label, extra, log, timeout_s = item
        cpu_ids = available.get()
        try:
            if extra[0] == "run":
                request_file = args.root / extra[2]
                request = json.loads(request_file.read_text(encoding="utf-8"))
                request["resources"]["cpu_ids"] = cpu_ids
                write_json(request_file, request)
            code = _invoke(args, extra[0], cpu_ids=cpu_ids,
                           extra=extra[1:], log=log, timeout_s=timeout_s)
            if action_is_trial(label) and code != 0:
                _record_trial_failure(args.root, label, code)
            return label, code
        finally:
            available.put(cpu_ids)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(call, item) for item in items]
        for future in as_completed(futures):
            label, exit_code = future.result()
            results[label] = exit_code
            status = "completed" if exit_code == 0 else f"failed ({exit_code})"
            if exit_code == 0 and action_is_trial(label):
                result_file = args.root / "runs" / label / "result.json"
                if result_file.is_file():
                    status = json.loads(result_file.read_text(encoding="utf-8")).get("status", status)
            print(f"{label}: {status}", flush=True)
    return results


def action_is_trial(label: str) -> bool:
    return not label.startswith(("build:", "plan:"))


def _record_trial_failure(root: Path, job_id: str, code: int) -> None:
    result = root / "runs" / job_id / "result.json"
    if not result.is_file():
        request_file = root / "requests" / f"{job_id}.json"
        request = (json.loads(request_file.read_text(encoding="utf-8"))
                   if request_file.is_file() else {"job_id": job_id})
        write_json(result, {**request, "status": "failed", "supported": False,
                            "confirmed": None, "findings": [], "run_failures": 1,
                            "reason": f"worker exited {code}; see logs/runs/{job_id}.log"})


def _run_target_waves(args: argparse.Namespace, jobs: list[Job],
                      items: list[tuple[str, list[str], Path, int | None]]) -> None:
    if not items:
        return
    pairs = _physical_cpus(15)
    tools = ("semantist", "icsquartz", "aflpp")
    targets = list(dict.fromkeys(job.target["id"] for job in jobs))
    pending = {item[0]: item for item in items}
    for wave_index, target_id in enumerate(targets):
        wave = []
        for job in jobs:
            if job.target["id"] != target_id or job.job_id not in pending:
                continue
            band = (tools.index(job.tool) + wave_index) % 3
            cpu_ids = pairs[band * 5 + job.seed]
            label, extra, log, timeout_s = pending[job.job_id]
            request_file = args.root / extra[2]
            request = json.loads(request_file.read_text(encoding="utf-8"))
            request["resources"]["cpu_ids"] = cpu_ids
            request["resources"]["wave"] = wave_index + 1
            write_json(request_file, request)
            wave.append((label, extra, log, timeout_s, cpu_ids))
        if not wave:
            continue
        print(f"wave {wave_index + 1}/{len(targets)}: {target_id} ({len(wave)} trials)", flush=True)
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {pool.submit(_invoke, args, extra[0], cpu_ids=cpu_ids,
                                   extra=extra[1:], log=log, timeout_s=timeout_s): label
                       for label, extra, log, timeout_s, cpu_ids in wave}
            for future in as_completed(futures):
                label = futures[future]
                exit_code = future.result()
                if exit_code != 0:
                    _record_trial_failure(args.root, label, exit_code)
                result_file = args.root / "runs" / label / "result.json"
                status = f"failed ({exit_code})" if exit_code else "completed"
                if exit_code == 0 and result_file.is_file():
                    status = json.loads(result_file.read_text(encoding="utf-8")).get("status", status)
                print(f"{label}: {status}", flush=True)


def _prepare(args: argparse.Namespace, jobs: list[Job], adapters: dict) -> None:
    semantist_targets = {job.target["id"]: job.target for job in jobs
                         if job.tool == "semantist" or
                         (adapters.get(job.tool) or {}).get("run_command")}
    external = {(job.target["id"], job.tool): job.target for job in jobs
                if job.tool != "semantist" and
                (adapters.get(job.tool) or {}).get("build_command")}
    for target_id, target in semantist_targets.items():
        if (build_path(args.root, target_id) / "build.json").is_file():
            validate_build_inputs(build_path(args.root, target_id), target)
    needs_build = any(not (build_path(args.root, target_id) / "build.json").is_file()
                      for target_id in semantist_targets)
    if needs_build:
        _bootstrap(args, _physical_cpus(1)[0])
    build_items = []
    for target_id in semantist_targets:
        package = build_path(args.root, target_id)
        if (package / "build.json").is_file():
            continue
        if package.exists():
            if not args.retry_failed:
                raise RuntimeError(f"incomplete common build: {package}; use --retry-failed")
            _preserve(package)
        stale = build_path(args.root, target_id).with_name("semantist.building")
        if stale.exists() and args.retry_failed:
            _preserve(stale)
        build_items.append((f"build:{target_id}", ["build", "--target-id", target_id],
                            args.root / "logs" / "builds" / f"{target_id}.log", None))
    build_results = _run_parallel(args, build_items, workers=min(args.build_jobs, len(build_items) or 1))
    if any(code != 0 for code in build_results.values()):
        raise RuntimeError("common target builds failed; see logs/builds")
    external_items = []
    for target_id, tool in external:
        if not (build_path(args.root, target_id) / "build.json").is_file():
            continue
        package = args.root / "builds" / target_id / tool
        result_file = package / "build-result.json"
        if result_file.is_file():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            if result.get("status") == "supported":
                continue
            if not args.retry_failed:
                raise RuntimeError(f"baseline build failed or unsupported: {result_file}; see reason")
            _preserve(package)
        elif package.exists():
            if not args.retry_failed:
                raise RuntimeError(f"incomplete baseline build: {package}; use --retry-failed")
            _preserve(package)
        stale = args.root / "builds" / target_id / f"{tool}.building"
        if stale.exists() and args.retry_failed:
            _preserve(stale)
        external_items.append((f"build:{target_id}:{tool}",
                            ["build", "--target-id", target_id, "--tool", tool],
                            args.root / "logs" / "builds" / f"{target_id}-{tool}.log", None))
    external_results = _run_parallel(args, external_items, workers=min(args.build_jobs, len(external_items) or 1))
    if any(code != 0 for code in external_results.values()):
        raise RuntimeError("baseline adapter builds failed; see logs/builds")
    plan_targets = {job.target["id"]: job.target for job in jobs
                    if job.tool == "semantist" and CONFIGURATIONS[job.configuration]["llm"]}
    plan_items = []
    for target_id in plan_targets:
        if _plan_ready(args.root, target_id):
            continue
        package = plan_path(args.root, target_id)
        if package.exists():
            if not args.retry_failed:
                raise RuntimeError(f"frozen plan is incomplete: {package}; use --retry-failed")
            _preserve(package)
        if (build_path(args.root, target_id) / "build.json").is_file():
            plan_items.append((f"plan:{target_id}", ["plan", "--target-id", target_id],
                               args.root / "logs" / "plans" / f"{target_id}.log", None))
    if args.retry_failed:
        for target_id in plan_targets:
            stale = plan_path(args.root, target_id).with_name("semantist.planning")
            if stale.exists():
                _preserve(stale)
    plan_results = _run_parallel(args, plan_items, workers=min(args.plan_jobs, len(plan_items) or 1))
    if any(code != 0 for code in plan_results.values()):
        raise RuntimeError("frozen target plans failed; see logs/plans")


def _plan_ready(root: Path, target_id: str) -> bool:
    package = plan_path(root, target_id)
    plan_file = package / "plan.json"
    if not plan_file.is_file():
        return False
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    seeds = package / "unified-corpus" / "seeds"
    return (plan.get("calls", 0) >= 1
            and (plan.get("summary") or {}).get("native_valid_plans", 0) >= 1
            and bool(plan.get("seed_files"))
            and all((seeds / name).is_file() for name in plan["seed_files"]))


def _require_prepared(root: Path, jobs: list[Job]) -> None:
    missing = set()
    for job in jobs:
        target_id = job.target["id"]
        common = build_path(root, target_id)
        if not (common / "build.json").is_file():
            missing.add(f"build:{target_id}")
        if job.tool != "semantist":
            external = root / "builds" / target_id / job.tool / "build-result.json"
            metadata = json.loads(external.read_text(encoding="utf-8")) if external.is_file() else {}
            if (metadata.get("status") != "supported"
                    or metadata.get("canonical_testcases") is not True
                    or metadata.get("oracle_equivalent") is not True
                    or not str(metadata.get("version") or "").strip()):
                missing.add(f"build:{target_id}:{job.tool}")
        elif CONFIGURATIONS[job.configuration]["llm"]:
            if not _plan_ready(root, target_id):
                missing.add(f"plan:{target_id}")
    if missing:
        examples = ", ".join(sorted(missing)[:10])
        raise RuntimeError(f"prepare must finish before search ({len(missing)} missing): {examples}")


def _worker(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    adapters = json.loads((root / "adapters.json").read_text(encoding="utf-8"))
    records = json.loads((root / "matrix.json").read_text(encoding="utf-8"))["jobs"]
    targets = {item["target"]["id"]: item["target"] for item in records}
    if args.action == "build":
        target = targets[args.target_id]
        if args.tool == "semantist":
            prepare_build(root, target)
        else:
            prepare_external_build(root, target, args.tool, adapters.get(args.tool) or {})
    elif args.action == "plan":
        prepare_plan(root, targets[args.target_id])
    else:
        data = json.loads((root / args.request).read_text(encoding="utf-8"))
        job = Job(data["target"], data["tool"], data["configuration"],
                  data["seed"], data["online_seconds"], data["experiments"])
        _validate_trial_request(root, job, data)
        assigned_cpus = set(data["resources"]["cpu_ids"])
        os.sched_setaffinity(0, assigned_cpus)
        if os.sched_getaffinity(0) != assigned_cpus:
            raise RuntimeError(f"trial CPU affinity differs from request: {job.job_id}")
        run_job(root, job, adapter_config=adapters,
                mutation_mode=data.get("mutation_mode"),
                semantic_ratio=float(data.get("semantic_ratio", 0.30)))
    return 0


def _validate_trial_request(root: Path, job: Job, request: dict) -> None:
    expected_build = (build_path(root, job.target["id"]) if job.tool == "semantist"
                      else root / "builds" / job.target["id"] / job.tool)
    expected_plan = (plan_path(root, job.target["id"])
                     if job.tool == "semantist" and CONFIGURATIONS[job.configuration]["llm"] else None)
    expected = {
        "build_package": str(expected_build.relative_to(root)),
        "common_replay_package": str(build_path(root, job.target["id"]).relative_to(root)),
        "plan_package": str(expected_plan.relative_to(root)) if expected_plan else None,
        "output_dir": str(result_path(root, job).parent.relative_to(root)),
    }
    if any(request.get(key) != value for key, value in expected.items()):
        raise ValueError(f"trial request has inconsistent package or output paths: {job.job_id}")
    resources = request.get("resources") or {}
    if (resources.get("cpu_count") != 2 or not resources.get("memory_limit")
            or len(resources.get("cpu_ids") or []) != 2):
        raise ValueError(f"trial request has incomplete CPU or memory allocation: {job.job_id}")
    mutation_mode = request.get("mutation_mode")
    if mutation_mode is not None and mutation_mode not in {
            "semantic-only", "byte-only", "combined", "type", "byte"}:
        raise ValueError(f"trial request has invalid mutation mode: {job.job_id}")
    ratio = request.get("semantic_ratio", 0.30)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise ValueError(f"trial request has invalid semantic ratio: {job.job_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("matrix", "prepare", "run", "summary"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--root", type=Path, default=ROOT / "artifacts/experiments")
        sub.add_argument("--experiment", choices=("overall", "core_ablation", "multicycle", "budget"))
        sub.add_argument("--target")
        sub.add_argument("--targets", help="comma-separated target ids for an explicit bounded smoke set")
        sub.add_argument("--tool", choices=("semantist", "icsquartz", "aflpp"))
        sub.add_argument("--configuration", choices=tuple(CONFIGURATIONS))
        sub.add_argument("--seed", type=int)
        sub.add_argument("--duration", type=int)
        sub.add_argument("--limit", type=int)
        sub.add_argument("--smoke-seconds", type=int)
        sub.add_argument("--execute", action="store_true")
        sub.add_argument("--allow-formal", action="store_true")
        sub.add_argument("--image", default="semantist:experiment")
        sub.add_argument("--engine", default="podman")
        sub.add_argument("--memory", default="8g")
        sub.add_argument("--build-jobs", type=int, default=8)
        sub.add_argument("--plan-jobs", type=int, default=4)
        sub.add_argument("--jobs", type=int, default=16)
        sub.add_argument("--schedule", choices=("standard", "target_waves"),
                         default=None)
        sub.add_argument("--llm-env-file", type=Path)
        sub.add_argument("--adapters", type=Path,
                         default=ROOT / "benchmarks/experiments/adapters.json")
        sub.add_argument("--local", action="store_true")
        sub.add_argument("--retry-failed", action="store_true")
        sub.add_argument("--mutation-mode", choices=("semantic-only", "byte-only", "combined"),
                         default="combined",
                         help="SemantiST field mutation mode")
        sub.add_argument("--semantic-ratio", type=float, default=0.30,
                         help="fixed semantic share in combined mode (0..1 or percentage)")
    worker = subparsers.add_parser("_worker")
    worker.add_argument("action", choices=("build", "plan", "run"))
    worker.add_argument("--root", type=Path, required=True)
    worker.add_argument("--target-id")
    worker.add_argument("--tool", default="semantist")
    worker.add_argument("--request")
    args = parser.parse_args()
    if args.command == "_worker":
        return _worker(args)
    if args.schedule is None:
        args.schedule = "standard"
    args.root = args.root.resolve()
    jobs = _select(args)
    formal_tool_run = (args.command == "run" and args.execute and args.experiment == "overall"
                       and args.smoke_seconds is None and args.tool in FORMAL_CPU_POOLS
                       and args.target is None and args.targets is None
                       and args.configuration is None and args.seed is None
                       and args.duration is None and args.limit is None)
    formal_prepare = (args.command == "prepare" and args.execute
                      and args.experiment == "overall" and args.smoke_seconds is None
                      and args.target is None and args.targets is None
                      and args.tool is None and args.configuration is None
                      and args.seed is None and args.duration is None
                      and args.limit is None)
    if formal_prepare and len(jobs) != 1500:
        raise ValueError("formal overall preparation requires all 1500 jobs")
    if formal_prepare or formal_tool_run:
        _validate_formal_root(args.root)
    if formal_tool_run and (len(jobs) != 500 or args.jobs != 5 or args.memory != "8g"):
        raise ValueError("formal per-tool overall run requires 500 jobs, --jobs 5 and --memory 8g")
    if formal_tool_run and args.schedule != "standard":
        raise ValueError("formal per-tool overall run requires --schedule standard")
    if (args.command == "run" and args.execute and args.experiment == "overall"
            and args.smoke_seconds is None and len(jobs) == 1500):
        raise ValueError("formal overall runs require --tool semantist, icsquartz or aflpp")
    if (args.command == "run" and args.execute and args.experiment == "overall"
            and args.smoke_seconds is None and args.tool in FORMAL_CPU_POOLS
            and args.target is None and args.targets is None
            and args.duration is None and args.seed is None
            and args.configuration is None and not formal_tool_run):
        raise ValueError("formal per-tool overall runs cannot use --limit")
    print(f"selected {len(jobs)} unique search jobs")
    if args.command == "matrix":
        by_experiment = {name: sum(name in job.experiments for job in jobs)
                         for name in ("overall", "core_ablation", "multicycle", "budget")}
        print(json.dumps(by_experiment, sort_keys=True))
        return 0
    if args.command == "summary":
        if (args.experiment is None and args.target is None and args.tool is None
                and args.configuration is None and args.seed is None
                and args.duration is None and args.limit is None
                and args.smoke_seconds is None):
            jobs = _scheduled(args.root) or jobs
        result = summarize(args.root, jobs)
        print(f"wrote {args.root / 'summary.json'} ({len(result['groups'])} groups)")
        return 0
    _validate_execution(args, jobs)
    _validate_target_waves(args, jobs)
    if not args.execute:
        print("dry run only; add --execute --smoke-seconds 10 for a bounded smoke test")
        return 0
    adapters = json.loads(args.adapters.read_text(encoding="utf-8"))
    _validate_formal_prerequisites(args, jobs, adapters)
    args.root.mkdir(parents=True, exist_ok=True)
    matrix_file = args.root / "matrix.json"
    if not matrix_file.is_file():
        write_json(matrix_file, {"schema_version": "semantist.experiment-matrix/1",
                                 "jobs": [job.as_dict() for job in make_matrix()]})
    frozen_adapters = args.root / "adapters.json"
    if frozen_adapters.is_file():
        if json.loads(frozen_adapters.read_text(encoding="utf-8")) != adapters:
            raise ValueError("adapter configuration changed; use a new experiment root")
    else:
        write_json(frozen_adapters, adapters)
    if args.command == "prepare":
        _prepare(args, jobs, adapters)
        _require_prepared(args.root, jobs)
        return 0
    if args.smoke_seconds is not None:
        _prepare(args, jobs, adapters)
    else:
        _require_prepared(args.root, [job for job in _select_all_overall(args.root)]
                          if formal_tool_run else jobs)
    all_scheduled = jobs if formal_tool_run else _remember_jobs(args.root, jobs)
    items = []
    for job in jobs:
        prior = result_path(args.root, job)
        if prior.is_file():
            status = json.loads(prior.read_text(encoding="utf-8")).get("status")
            if status == "completed" or not args.retry_failed:
                continue
        if prior.parent.exists() and any(prior.parent.iterdir()):
            if not args.retry_failed:
                print(f"incomplete trial: {prior.parent}; use --retry-failed to rerun")
                _record_trial_failure(args.root, job.job_id, 125)
                continue
            _preserve(prior.parent)
        request = Path("requests") / f"{job.job_id}.json"
        trial_request = {
            **job.as_dict(),
            "build_package": str((build_path(args.root, job.target["id"])
                                  if job.tool == "semantist" else
                                  args.root / "builds" / job.target["id"] / job.tool).relative_to(args.root)),
            "common_replay_package": str(build_path(args.root, job.target["id"]).relative_to(args.root)),
            "plan_package": (str(plan_path(args.root, job.target["id"]).relative_to(args.root))
                             if job.tool == "semantist" and CONFIGURATIONS[job.configuration]["llm"]
                             else None),
            "output_dir": str(result_path(args.root, job).parent.relative_to(args.root)),
            "mutation_mode": args.mutation_mode,
            "semantic_ratio": args.semantic_ratio if args.semantic_ratio <= 1
                               else args.semantic_ratio / 100.0,
            "resources": {"cpu_count": 2, "memory_limit": args.memory, "cpu_ids": []},
        }
        write_json(args.root / request, trial_request)
        items.append((job.job_id, ["run", "--request", str(request)],
                      args.root / "logs" / "runs" / f"{job.job_id}.log",
                      job.online_seconds + 180))
    if args.schedule == "target_waves":
        _run_target_waves(args, jobs, items)
    else:
        _run_parallel(args, items, workers=min(args.jobs, len(items) or 1))
    if not formal_tool_run:
        summarize(args.root, all_scheduled)
    incomplete = [job.job_id for job in jobs if not result_path(args.root, job).is_file()
                  or json.loads(result_path(args.root, job).read_text(encoding="utf-8")).get("status")
                  != "completed"]
    if incomplete:
        location = "logs/runs and result.json" if formal_tool_run else "summary.json"
        print(f"{len(incomplete)} trials are incomplete or unsupported; see {location}")
    return 1 if incomplete else 0


def _select_all_overall(root: Path) -> list[Job]:
    matrix_file = root / "matrix.json"
    records = json.loads(matrix_file.read_text(encoding="utf-8"))["jobs"]
    return [Job(item["target"], item["tool"], item["configuration"],
                item["seed"], item["online_seconds"], item["experiments"])
            for item in records if "overall" in item["experiments"]]


def _preserve(path: Path) -> None:
    suffix = 1
    while path.with_name(f"{path.name}.previous-{suffix}").exists():
        suffix += 1
    path.rename(path.with_name(f"{path.name}.previous-{suffix}"))


if __name__ == "__main__":
    raise SystemExit(main())
