"""Campaign orchestration: search, candidate persistence, confirmation, repro."""

from __future__ import annotations

import json
import math
import os
import signal
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fuzzer.corpus.frames import Frame, TestCase, canonicalize_raw, parse_testcase
from fuzzer.pipeline.metrics import Metrics
from fuzzer.pipeline.probe import Observation, classify, is_finding_candidate, probe
from fuzzer.pipeline.properties import evaluate_properties, load_properties
from fuzzer.pipeline.model import TargetModel
from fuzzer.pipeline import minimize as minimize_module
from fuzzer.llm.plan import _field_map


def _normalize_ablations(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize aliases without silently disabling search capabilities."""
    ablations = dict(config.get("ablations") or {})
    aliases = {
        "v3_no_symbolic": "no_symbolic",
        "v3_symbolic_initial_only": "symbolic_initial_only",
        "v3_symbolic_start": "symbolic_start",
        "v3_no_goal_prefix": "no_goal_prefix",
        "v3_no_distance": "no_distance",
        "v3_no_time_guidance": "no_time_guidance",
        "v3_no_snapshot": "no_snapshot",
        "v3_no_state_copy": "no_state_copy",
        "v3_llm": "llm",
        "v3_llm_initial_only": "llm_initial_only",
    }
    for alias, canonical in aliases.items():
        if canonical not in ablations and alias in config:
            ablations[canonical] = config[alias]
    config["ablations"] = ablations
    return ablations


@dataclass
class Candidate:
    finding_id: str
    testcase: bytes
    observation: Observation
    candidate: dict[str, Any]
    provenance: dict[str, Any]
    created_s: float


def solver_prefix_skip_reason(record: dict[str, Any], task: dict[str, Any], *,
                              single_cycle: bool = False) -> str | None:
    if single_cycle:
        return "single_cycle_uses_cold_queries"
    if record.get("task_id") != task.get("task_id"):
        return "unrelated_task"
    if (record.get("queue") != "klee_suffix"
            or record.get("trigger") not in {"task_distance_improved", "native_hazard_reach",
                                              "state_frontier", "task_stagnation"}
            or record.get("task_unresolved") is not True
            or not isinstance(record.get("native_execution"), int)):
        return "missing_native_task_progress"
    if not task.get("symbolic_suitable") or task.get("goal_class") != "defect":
        return "task_not_symbolically_suitable"
    return None


def solver_prefix_priority(record: dict[str, Any]) -> tuple[int, int, int]:
    distance = record.get("distance")
    at_goal_frontier = (isinstance(distance, (int, float)) and not isinstance(distance, bool)
                        and math.isfinite(distance) and distance <= 0)
    stagnation = record.get("consecutive_no_progress")
    trigger_rank = {"native_hazard_reach": 3, "state_frontier": 2,
                    "task_distance_improved": 1, "task_stagnation": 0}
    return (trigger_rank.get(record.get("trigger"), 0),
            int(at_goal_frontier), max(0, stagnation) if type(stagnation) is int else 0)


def solver_feedback_cutoff(engine_deadline: float, minimum_margin_s: float,
                           recent_costs_s: list[float]) -> float:
    """Reserve time for cold replay, queue ingestion and at least one mutation."""
    measured = max((cost for cost in recent_costs_s[-5:]
                    if isinstance(cost, (int, float)) and math.isfinite(cost) and cost > 0),
                   default=0.0)
    return engine_deadline - max(minimum_margin_s, measured * 2 + 2.0)


class Campaign:
    def __init__(
        self,
        run_dir: Path,
        bundle: Path,
        target_binary: Path,
        *,
        function: str,
        st_file: Path,
        config: dict[str, Any] | None = None,
        time_budget_s: float = 300.0,
        jobs: int = 12,
        seed: int = 0,
        max_evaluations: int = 40000,
    ):
        self.run_dir = Path(run_dir)
        self.bundle = Path(bundle)
        self.target_binary = Path(target_binary)
        self.function = function
        self.st_file = Path(st_file)
        self.config = config or {}
        _normalize_ablations(self.config)
        self.properties = load_properties(self.config.get("property_file"), function)
        generated_file = self.config.get("generated_oracle_file")
        generated_document = (json.loads(Path(generated_file).read_text(encoding="utf-8"))
                              if generated_file else {"properties": []})
        self.generated_properties = {
            spec["id"]: spec for spec in generated_document.get("properties", [])
        }
        self.time_budget_s = time_budget_s
        self.jobs = jobs
        self.seed = seed
        self.max_evaluations = max_evaluations
        self.started = time.monotonic()
        self.started_epoch_ns = time.time_ns()
        self.deadline = self.started + max(0.0, time_budget_s)
        planning_budget = min(120.0, max(0.0, time_budget_s) * 0.2)
        self.startup_deadline = self.started + planning_budget
        self.symbolic_startup_deadline = self.started + min(
            float(self.config.get("symbolic_startup_budget_s", 30.0)), planning_budget)
        self.engine_deadline = self.deadline - min(5.0, max(1.0, time_budget_s * 0.02))
        self.planned_deadline = self.engine_deadline
        self.solver_deadline = self.engine_deadline - max(
            1.0, float(self.config.get("solver_feedback_margin_s", 15.0)))
        self.phase_times_s: dict[str, float] = {}
        self.metrics = Metrics(self.run_dir)
        self.model = TargetModel.load(self.run_dir, self.bundle)
        contract_path = self.bundle / "execution-contract.json"
        try:
            self.contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.contract = {}
        self.time_dependent = False
        self.cycle_effects: dict[str, Any] = {}
        try:
            goal_tasks = json.loads((self.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
            self.cycle_effects = goal_tasks.get("cycle_effects") or {}
            self.time_dependent = any(task.get("timers") for task in goal_tasks.get("tasks", []))
        except (OSError, json.JSONDecodeError):
            pass
        self.candidates: list[Candidate] = []
        self.confirmed: list[dict[str, Any]] = []
        self.candidate_confirmation_results: list[dict[str, Any]] = []
        self.symbolic_unsupported: list[dict[str, Any]] = []
        self.symbolic_results: list[dict[str, Any]] = []
        self.native_replay_results: list[dict[str, Any]] = []
        # Startup and live prefix queries share one campaign-wide budget.
        self._solver_budget_lock = threading.Lock()
        self._solver_queries_used = 0
        self._solver_task_queries: dict[str, int] = {}
        try:
            task_document = json.loads((self.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
            self._solver_task_limits = {
                str(task["task_id"]): max(0, int((task.get("budget") or {}).get(
                    "solver_queries", self.max_evaluations)))
                for task in task_document.get("tasks", []) if task.get("task_id")
            }
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            self._solver_task_limits = {}
        self._solver_seed_lock = threading.Lock()
        self._solver_seed_best_distance: dict[str, float] = {}
        self._solver_seed_next_id = 1

    def _claim_solver_budget(self, task_id: str | None = None) -> bool:
        """Claim one logical solver query from the campaign budget."""
        lock = getattr(self, "_solver_budget_lock", None)
        if lock is None:
            lock = self._solver_budget_lock = threading.Lock()
        with lock:
            used = int(getattr(self, "_solver_queries_used", 0))
            limit = max(0, int(getattr(self, "max_evaluations", 0)))
            if used >= limit:
                return False
            task_counts = getattr(self, "_solver_task_queries", None)
            if task_counts is None:
                task_counts = self._solver_task_queries = {}
            task_limit = getattr(self, "_solver_task_limits", {}).get(task_id, limit)
            if task_id is not None and task_counts.get(task_id, 0) >= task_limit:
                return False
            self._solver_queries_used = used + 1
            if task_id is not None:
                task_counts[task_id] = task_counts.get(task_id, 0) + 1
            return True

    def _record_symbolic_result(self, query_id: str, task_id: str | None,
                                result: dict[str, Any]) -> None:
        record = {"query_id": query_id, "task_id": task_id,
                  "status": result.get("status", "FAILED"),
                  "reason": result.get("reason"),
                  "solver_scope": result.get("solver_scope"),
                  "model_scope": result.get("model_scope"),
                  "candidate_file": result.get("candidate_file"),
                  "stats": result.get("stats")}
        self.symbolic_results.append(record)
        self.metrics.add("solver_queries", 1)
        if record["status"] == "UNSUPPORTED":
            self.symbolic_unsupported.append(record)

    @staticmethod
    def _scope_is_approximate(scope: dict[str, Any] | None) -> bool:
        """Keep approximate contract/stub provenance in model statistics."""
        scope = scope or {}
        return bool(
            scope.get("imprecise")
            or scope.get("contract_approximate")
            or str(scope.get("semantic_fidelity") or "").lower() == "approximate"
            or scope.get("stub_used")
        )

    def _record_klee_queue_result(self, *, prefix_id: str, query_id: str,
                                  task_id: str | None,
                                  result: dict[str, Any]) -> None:
        """Publish the suffix outcome beside the Rust-produced queue.

        The result is transport/audit metadata only. Native cold replay still
        owns candidate and finding decisions, and no digest is used for queue
        identity or deduplication.
        """
        path = self.run_dir / "prefixes" / "klee-results.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "queue": "klee_suffix_result",
            "prefix_id": prefix_id,
            "query_id": query_id,
            "task_id": task_id,
            "status": result.get("status", "FAILED"),
            "reason": result.get("reason"),
            "model_scope": result.get("model_scope"),
            "solver_scope": result.get("solver_scope"),
            "candidate_file": result.get("candidate_file"),
            "native_replay": "required" if result.get("status") in {"SAT", "SAT_APPROX"} else "not_applicable",
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def _record_klee_native_replay(self, *, prefix_id: str, query_id: str,
                                   task_id: str | None,
                                   observation: Observation,
                                   matched: bool, model_goal_reached: bool = False) -> str:
        """Publish the native cold replay outcome for a solver testcase.

        This is audit transport only. Candidate and finding state still comes
        from the normal native observation and confirmation paths.
        """
        path = self.run_dir / "prefixes" / "klee-results.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if observation.timed_out:
            status = "TIMEOUT"
        elif matched:
            # One cold probe establishes a replayable candidate. The finding
            # layer may promote it to confirmed only after repeat probes.
            status = "candidate"
        elif observation.scan_budget:
            status = "INSTRUMENTATION_ABORT"
        elif model_goal_reached:
            status = "model_goal_reached"
        else:
            status = "REPLAY_MISMATCH"
        record = {
            "queue": "klee_native_replay",
            "prefix_id": prefix_id,
            "query_id": query_id,
            "task_id": task_id,
            "status": status,
            "returncode": observation.returncode,
            "wall_s": round(float(observation.wall_s), 6),
            "timed_out": bool(observation.timed_out),
            "crashed": bool(observation.crashed),
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        return status

    def _mark_native_replay_status(self, query_id: str, status: str) -> None:
        """Attach the cold replay outcome without replacing solver status."""
        for record in reversed(self.symbolic_results):
            if record.get("query_id") == query_id:
                record["native_replay_status"] = status
                return

    def _query_result_depth(self, result: dict[str, Any], testcase: bytes,
                            fallback: int) -> int:
        """Use the solver's actual query scope before estimating frame depth."""
        for scope_name in ("model_scope", "solver_scope"):
            scope = result.get(scope_name)
            if isinstance(scope, dict):
                try:
                    depth = int(scope.get("query_depth") or 0)
                except (TypeError, ValueError):
                    depth = 0
                if depth > 0:
                    return depth
        try:
            return len(parse_testcase(testcase, self._abi_like()).frames)
        except Exception:  # noqa: BLE001 - depth is audit metadata only
            return max(1, int(fallback))

    def _solver_candidate(self, result: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
        """Load a solver testcase; only SAT may become a finding."""
        status = result.get("status")
        if status not in {"SAT", "SAT_APPROX", "REPLAY_MISMATCH", "UNKNOWN", "UNSAT"}:
            return result, None
        candidate_file = result.get("candidate_file")
        if not candidate_file:
            if status not in {"SAT", "SAT_APPROX"}:
                return result, None
            return {**result, "status": "REPLAY_MISMATCH",
                    "reason": "solver reported SAT without candidate_file"}, None
        try:
            return result, Path(candidate_file).read_bytes()
        except (OSError, TypeError, ValueError) as error:
            if status not in {"SAT", "SAT_APPROX"}:
                return result, None
            return {**result, "status": "REPLAY_MISMATCH",
                    "reason": f"solver candidate unavailable: {error}"}, None

    def _enqueue_solver_seed(self, testcase: bytes, *, query_id: str,
                             task_id: str | None) -> bool:
        """Queue a canonical KLEE input; the native engine decides its value."""
        stop_event = getattr(self, "_engine_stop_event", None)
        config = getattr(self, "config", {})
        if stop_event is not None and config.get("ablations", {}).get("no_online_reinjection"):
            self.metrics.event("solver_seed_rejected", query_id=query_id,
                               reason="experiment_no_online_reinjection")
            return False
        if stop_event is not None and stop_event.is_set():
            self.metrics.event("solver_seed_rejected", query_id=query_id,
                               reason="engine_finished_no_feedback_this_round")
            return False
        if not testcase.strip():
            return False
        try:
            from fuzzer.corpus.frames import validate

            abi = self._abi_like()
            case = parse_testcase(testcase, abi)
            validate(case, abi)
            if not case.frames or len(case.frames) > 300 or case.init_profile != "cold":
                return False
            testcase = canonicalize_raw(case, abi, include_dt=True)
        except (OSError, TypeError, ValueError, UnicodeError):
            return False
        lock = getattr(self, "_solver_seed_lock", None)
        if lock is None:
            lock = self._solver_seed_lock = threading.Lock()
        with lock:
            seed_id = int(getattr(self, "_solver_seed_next_id", 1))
            limit = max(0, int(config.get("solver_seed_queue_limit", 64)))
            if seed_id > limit:
                self.metrics.event("solver_seed_rejected", query_id=query_id,
                                   reason="queue_limit")
                return False
            directory = self.run_dir / "solver" / "reinject"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"solver-seed-{seed_id:08}.seed"
            temporary = directory / f"solver-seed-{seed_id:08}.tmp"
            metadata_path = directory / f"solver-seed-{seed_id:08}.json"
            metadata_temporary = directory / f"solver-seed-{seed_id:08}.json.tmp"
            try:
                temporary.write_bytes(testcase)
                metadata_temporary.write_text(json.dumps({
                    "query_id": query_id, "task_id": task_id,
                    "native_replay": "pending_engine_execution",
                }, sort_keys=True) + "\n", encoding="utf-8")
                os.replace(metadata_temporary, metadata_path)
                os.replace(temporary, path)
            except OSError as error:
                temporary.unlink(missing_ok=True)
                metadata_temporary.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                self.metrics.event("solver_seed_rejected", query_id=query_id,
                                   reason=f"queue write failed: {error}")
                return False
            self._solver_seed_next_id = seed_id + 1
        self.metrics.add("solver_seeds_enqueued", 1)
        self.metrics.event("solver_seed_enqueued", query_id=query_id, task_id=task_id,
                           seed_id=seed_id, frames=len(case.frames),
                           native_replay="pending_engine_execution")
        return True

    def _solver_state_key(self, task_id: str | None,
                          observation: Observation) -> str | None:
        if not task_id or not observation.states:
            return None
        try:
            tasks = json.loads((self.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
        except (AttributeError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        task = next((item for item in tasks.get("tasks", [])
                     if item.get("task_id") == task_id), None)
        names = {str(item.get("name") or "").upper() for item in (task or {}).get("state_slice", [])
                 if item.get("block_kind") in {"VAR", "VAR_OUTPUT", "VAR_IN_OUT"}}
        projection = {name.upper(): value for name, value in observation.states[-1].items()
                      if name.upper() in names}
        return json.dumps(projection, sort_keys=True, default=str) if projection else None

    def _solver_task_distance(self, task_id: str | None, case: TestCase,
                              observation: Observation) -> float | None:
        """Score supported scalar path conditions as guidance, never as an oracle."""
        if not task_id:
            return None
        try:
            tasks = json.loads((self.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
        except (AttributeError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        task = next((item for item in tasks.get("tasks", [])
                     if item.get("task_id") == task_id), None)
        if task is None:
            return None
        boundary = task.get("array_boundary") or {}
        if boundary and observation.states:
            runtime_id = (task.get("goal") or {}).get("runtime_id")
            if runtime_id in observation.hits:
                return 0.0
            state = {str(name).upper(): value for name, value in observation.states[-1].items()}
            try:
                index = int(state[str(boundary["field"]).upper()])
                lower, upper = int(boundary["lower"]), int(boundary["upper"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                upper_distance = max(1, upper + 1 - index)
                if boundary.get("lower_guarded"):
                    return float(upper_distance)
                return float(min(upper_distance, max(1, index - (lower - 1))))
        values = {name.upper(): value for name, value in case.frames[-1].values.items()}
        if observation.states:
            values.update({name.upper(): value for name, value in observation.states[-1].items()})
        scores = []
        for path in task.get("candidate_paths") or []:
            conditions = path.get("conditions") or []
            if not conditions:
                continue
            penalties = []
            for condition in conditions:
                field = str((condition.get("lhs") or {}).get("field") or "").upper()
                try:
                    lhs = float(values[field])
                    rhs = float((condition.get("rhs") or {})["const"])
                except (KeyError, TypeError, ValueError):
                    break
                if not (math.isfinite(lhs) and math.isfinite(rhs)):
                    break
                op = condition.get("op")
                if op in ("=", "=="):
                    penalty = abs(lhs - rhs)
                elif op in ("<>", "!="):
                    penalty = 0.0 if lhs != rhs else 1.0
                elif op == "<":
                    penalty = max(0.0, lhs - rhs + 1.0)
                elif op == "<=":
                    penalty = max(0.0, lhs - rhs)
                elif op == ">":
                    penalty = max(0.0, rhs - lhs + 1.0)
                elif op == ">=":
                    penalty = max(0.0, rhs - lhs)
                else:
                    break
                penalties.append(penalty)
            else:
                scores.append(sum(penalties))
        return min(scores) if scores else None

    def _native_task_reached(self, task_id: str) -> bool:
        reached = getattr(self, "_native_reached_task_ids", None)
        if reached is None:
            reached = self._native_reached_task_ids = set()
        path = self.run_dir / "metrics" / "events.jsonl"
        offset = getattr(self, "_native_events_offset", 0)
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read()
        except OSError:
            return task_id in reached
        last_line = chunk.rfind(b"\n")
        if last_line < 0:
            return task_id in reached
        self._native_events_offset = offset + last_line + 1
        for line in chunk[:last_line].splitlines():
            try:
                event = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                continue
            if event.get("kind") == "goal_reached" and event.get("task_id"):
                reached.add(str(event["task_id"]))
        return task_id in reached

    def _offer_additional_solver_seeds(self, result: dict[str, Any], *,
                                       deadline: float,
                                       query_id: str, task_id: str | None) -> None:
        if not result.get("seed_only"):
            return
        for reference in result.get("seed_files", [])[:15]:
            if time.monotonic() >= deadline:
                break
            try:
                testcase = Path(reference).read_bytes()
            except (OSError, TypeError, ValueError):
                continue
            self._enqueue_solver_seed(testcase, query_id=query_id, task_id=task_id)

    # -- inputs ----------------------------------------------------------
    def _abi_like(self):
        from fuzzer.pipeline.model import _AbiLike

        return _AbiLike(self.model)

    def _candidate_key(self, candidate: dict[str, Any]) -> tuple[Any, ...]:
        return (
            candidate.get("oracle"),
            candidate.get("runtime_id"),
            json.dumps(candidate.get("source"), sort_keys=True) if candidate.get("source") else None,
            candidate.get("property_id"),
        )

    def _observe_again(self, testcase: bytes, *, semantic: bool = False) -> Observation:
        return self._probe_with_deadline(testcase, timeout_s=5.0, trace=semantic)

    def _replay_engine_candidate(self, testcase: bytes, *, expected_runtime_id: int | None = None
                                 ) -> tuple[Observation, dict[str, Any] | None]:
        observation = self._observe_again(testcase)
        info = self._classify_testcase(
            observation, testcase, expected_runtime_id=expected_runtime_id)
        if not is_finding_candidate(info) and observation.returncode == 0 and not observation.timed_out:
            traced = self._observe_again(testcase, semantic=True)
            traced_info = self._classify_testcase(
                traced, testcase, expected_runtime_id=expected_runtime_id)
            if is_finding_candidate(traced_info) or traced.timed_out:
                return traced, traced_info
        return observation, info

    def _verify_planned_prefixes(self) -> None:
        try:
            tasks = json.loads((self.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        checked = 0
        for task in tasks.get("tasks", []):
            goal = task.get("goal") or {}
            if goal.get("hazard") != "array_access" or not isinstance(goal.get("runtime_id"), int):
                continue
            for prefix in (task.get("seed_prefixes") or [])[:2]:
                if checked >= 4 or time.monotonic() >= self.engine_deadline:
                    return
                checked += 1
                try:
                    testcase = self.model.build(prefix)
                    observation = self._probe_with_deadline(testcase, trace=True)
                    info = self._classify_testcase(
                        observation, testcase, expected_runtime_id=goal["runtime_id"])
                except (OSError, ValueError, TypeError):
                    continue
                self.metrics.event("planned_prefix_probe", task_id=task.get("task_id"),
                                   runtime_id=goal["runtime_id"], frames=len(prefix),
                                   goal_hit=is_finding_candidate(info))
                if is_finding_candidate(info):
                    self._register_candidate(testcase, observation, info,
                                             provenance_source="planned_prefix", depth=len(prefix),
                                             task_id=task.get("task_id"))
                    self._run_confirmation()
                    break

    def _verify_property_witnesses(self) -> None:
        if not getattr(self, "properties", []):
            return
        abi = json.loads((self.bundle / "abi.json").read_text(encoding="utf-8"))
        names = {entry["name"].upper(): entry["name"] for entry in abi.get("frame_fields", [])}
        base = self._default_action_frame(abi)
        for spec in self.properties[:4]:
            value, window, reset = (spec[key].upper() for key in ("input", "window", "reset"))
            if not {value, window, reset}.issubset(names) or time.monotonic() >= self.engine_deadline:
                continue
            first = {**base, names[value]: "1", names[window]: "2", names[reset]: "1"}
            second = {**first, names[value]: "0", names[reset]: "0"}
            try:
                testcase = self.model.build([first, second])
                observation = self._probe_with_deadline(testcase, trace=True)
                info = self._classify_testcase(
                    observation, testcase, expected_property_id=spec["id"])
            except (OSError, ValueError, TypeError):
                continue
            self.metrics.event("property_witness_probe", property_id=spec["id"],
                               goal_hit=is_finding_candidate(info))
            if is_finding_candidate(info):
                self._register_candidate(testcase, observation, info,
                                         provenance_source="property_witness", depth=2,
                                         task_id=f"property:{spec['id']}")
                self._run_confirmation()

    def _classify_testcase(self, observation: Observation, testcase: bytes, *,
                           expected_runtime_id: int | None = None,
                           expected_property_id: str | None = None) -> dict[str, Any] | None:
        def generated(info: dict[str, Any] | None) -> dict[str, Any] | None:
            if info is None or info.get("oracle") != "generated_property":
                return info
            spec = self.generated_properties.get(info.get("property_id"))
            if spec is None:
                return None
            return {**info, "property_spec": spec,
                    "source": {"file": spec["evidence_refs"][0]},
                    "modeling": "llm_generated_checker"}

        if expected_property_id is not None:
            info = generated(classify(observation, self.model.runtime_index))
            if info and info.get("property_id") == expected_property_id:
                return info
            return evaluate_properties(observation, testcase, self._abi_like(),
                                       getattr(self, "properties", []), expected_id=expected_property_id)
        info = generated(classify(observation, self.model.runtime_index,
                                  expected_runtime_id=expected_runtime_id))
        if is_finding_candidate(info) or expected_runtime_id is not None:
            return info
        return evaluate_properties(observation, testcase, self._abi_like(),
                                   getattr(self, "properties", [])) or info

    def _classify_candidate_observation(self, candidate: Candidate,
                                        observation: Observation) -> dict[str, Any] | None:
        property_id = candidate.candidate.get("property_id")
        if property_id:
            return self._classify_testcase(
                observation, candidate.testcase, expected_property_id=property_id)
        runtime_id = (candidate.candidate.get("runtime_id")
                      if candidate.candidate.get("kind") == "hazard_violation" else None)
        return self._classify_testcase(
            observation, candidate.testcase, expected_runtime_id=runtime_id)

    def _probe_with_deadline(self, testcase: bytes, *, timeout_s: float = 5.0,
                             deadline: float | None = None,
                             trace: bool = True) -> Observation:
        """Run a native probe without exceeding the campaign phase deadline."""
        limit = self.deadline if deadline is None else min(self.deadline, deadline)
        remaining = limit - time.monotonic()
        if remaining <= 0:
            return Observation(None, True, "campaign deadline exhausted")
        return probe(
            self.target_binary,
            testcase,
            work_dir=self.run_dir / "probe",
            timeout_s=min(float(timeout_s), max(0.001, remaining)),
            trace=trace,
        )

    def _source_excerpt(self, task: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded, line-addressed source context for LLM v2."""
        goal = task.get("goal") or {}
        source = goal.get("source") or {}
        source_path = source.get("file") if isinstance(source, dict) else None
        path = Path(source_path) if source_path else self.st_file
        if not path.is_absolute() and not path.is_file():
            path = self.st_file.parent / path
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {"path": str(path), "start_line": None, "end_line": None,
                    "text": "", "truncated": False}
        line = (source.get("start_line") or source.get("line")) if isinstance(source, dict) else None
        line = int(line) if isinstance(line, int) and line > 0 else 1
        if source_path:
            start, end = max(1, line - 12), min(len(lines), line + 15)
        else:
            start = next((index + 2 for index, text in enumerate(lines)
                          if "@END_DECLARATION" in text), 1)
            end = min(len(lines), start + 159)
        truncated = start > 1 or end < len(lines)
        return {"path": str(path), "start_line": start, "end_line": end,
                "text": "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1)),
                "truncated": truncated}

    def _llm_controllable_fields(self, abi: dict[str, Any], allowed: set[str] | None = None) -> list[dict[str, Any]]:
        result = []
        for name, entry in _field_map(abi).items():
            if allowed is not None and name not in allowed and entry.get("_parent") not in allowed:
                continue
            spec = entry.get("spec") or {}
            domain: Any = None
            if spec.get("kind") == "scalar":
                type_name = str(spec.get("name"))
                if type_name == "BOOL":
                    domain = [0, 1]
                elif type_name not in ("REAL", "LREAL"):
                    bits = int(spec.get("size") or 1) * 8
                    unsigned = str(spec.get("c_type") or "").startswith("u") or type_name in {
                        "USINT", "BYTE", "UINT", "WORD", "UDINT", "DWORD", "ULINT", "LWORD",
                    }
                    domain = {"min": 0 if unsigned else -(2 ** (bits - 1)),
                              "max": (2 ** bits - 1) if unsigned else 2 ** (bits - 1) - 1}
            elif spec.get("kind") == "string":
                domain = {"max_length": spec.get("length") or spec.get("capacity")}
            result.append({"name": name, "type": spec.get("name") or spec.get("kind"),
                           "domain": domain})
        return result

    @staticmethod
    def _default_action_frame(abi: dict[str, Any]) -> dict[str, str]:
        """Build a cold frame from every controllable ABI action field.

        VAR_IN_OUT fields are represented in ``shared_fields`` but are still
        per-frame actions.  Keeping this rule in one place prevents LLM and
        symbolic cold starts from silently dropping scan-cycle inputs.
        """
        entries = list(abi.get("frame_fields", []))
        seen = {entry.get("name") for entry in entries}
        entries.extend(
            entry for entry in abi.get("shared_fields", [])
            if entry.get("block_kind") == "VAR_IN_OUT" and entry.get("name") not in seen
        )
        from fuzzer.corpus.frames import default_for_spec, structured_default_for_spec
        values: dict[str, str] = {}
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            spec = entry.get("spec") or {}
            # XML_READER's buffer is a byte array, but an all-zero cold value
            # leaves its state machine in an unbounded search loop. A minimal
            # empty element is a valid structural seed; later byte operators
            # still own arbitrary payload exploration.
            if name.upper() == "BUF" and spec.get("kind") in {"array", "opaque"}:
                values[name] = "0x3c612f3e"
            elif spec.get("kind") in {"opaque", "array"} and spec.get("members"):
                values[name] = structured_default_for_spec(name, spec)
            else:
                values[name] = default_for_spec(spec)
        return values

    @staticmethod
    def _shared_records_for_frames(abi: dict[str, Any], frames: list[dict[str, str]]) -> str:
        """Encode the initial VAR_IN_OUT backing values used by TargetModel.

        ``TargetModel.build`` promotes the first frame's VAR_IN_OUT values to
        shared records.  KLEE planned-prefix drivers must receive the same
        initial storage or their cold replay can diverge before the first
        symbolic suffix frame.
        """
        if not frames:
            return ""
        first = frames[0]
        lines: list[str] = []
        seen: set[str] = set()
        for entry in abi.get("shared_fields", []):
            if entry.get("block_kind") != "VAR_IN_OUT":
                continue
            name = entry.get("name")
            if not name or name in seen or name not in first:
                continue
            seen.add(name)
            spec = entry.get("spec") or {}
            token = (spec.get("name") if spec.get("kind") in ("scalar", "string")
                     else "POINTER" if spec.get("kind") == "pointer" else "ARRAY")
            lines.append(f"{name},{token},{first[name]}\n")
        return "".join(lines)

    # -- persistence -----------------------------------------------------
    def _register_candidate(
        self,
        testcase: bytes,
        observation: Observation,
        candidate_info: dict[str, Any],
        *,
        provenance_source: str = "fuzz",
        depth: int = 1,
        query_id: str | None = None,
        task_id: str | None = None,
        solver_scope: dict[str, Any] | None = None,
        created_s: float | None = None,
    ) -> Candidate:
        signature = (
            candidate_info.get("oracle"),
            candidate_info.get("runtime_id"),
            (candidate_info.get("source") or {}).get("file"),
            (candidate_info.get("source") or {}).get("start_offset"),
            candidate_info.get("diagnostic") if candidate_info.get("kind") == "crash" else None,
            candidate_info.get("property_id"),
        )
        for existing in self.candidates:
            existing_signature = (
                existing.candidate.get("oracle"),
                existing.candidate.get("runtime_id"),
                (existing.candidate.get("source") or {}).get("file"),
                (existing.candidate.get("source") or {}).get("start_offset"),
                existing.candidate.get("diagnostic") if existing.candidate.get("kind") == "crash" else None,
                existing.candidate.get("property_id"),
            )
            if existing_signature == signature:
                return existing
        finding_id = f"F{len(self.candidates) + 1:04d}"
        directory = self.run_dir / "findings" / "candidates"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{finding_id}.txt").write_bytes(testcase)
        record = {
            "finding_id": finding_id,
            "contract_id": self.config.get("contract_id"),
            "oracle": candidate_info.get("oracle"),
            "hazard": candidate_info.get("hazard"),
            "runtime_id": candidate_info.get("runtime_id"),
            "trigger_cycle": candidate_info.get("cycle"),
            "source": candidate_info.get("source"),
            "diagnostic": candidate_info.get("diagnostic"),
            "observed_predicate": candidate_info.get("observed_predicate"),
            "property_id": candidate_info.get("property_id"),
            "property_spec": candidate_info.get("property_spec"),
            "risk_modeling": candidate_info.get("modeling"),
            "testcase_file": f"findings/candidates/{finding_id}.txt",
            "depth": depth,
            "support_status": self.contract.get("support_status", "unknown"),
            "execution_status": {"build": "completed", "native": "candidate"},
            "semantic_fidelity": self.contract.get("semantic_fidelity", "unknown"),
            "stub_used": self.contract.get("stub_used", []),
            "model_scope": solver_scope or self.contract.get("model_scope", {}),
            "provenance": {"source": provenance_source, "attempt_id": finding_id, "query_id": query_id,
                           "task_id": task_id,
                           "solver_scope": solver_scope,
                           "solver_model": "approximate" if solver_scope and solver_scope.get("imprecise")
                           else "faithful" if solver_scope else None},
            "created_s": round(created_s if created_s is not None
                               else time.monotonic() - self.started, 3),
        }
        (directory / f"{finding_id}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        candidate = Candidate(
            finding_id,
            testcase,
            observation,
            candidate_info,
            record["provenance"],
            record["created_s"],
        )
        self.candidates.append(candidate)
        self.metrics.event(
            "candidate",
            finding_id=finding_id,
            oracle=candidate_info.get("oracle"),
            runtime_id=candidate_info.get("runtime_id"),
            depth=depth,
            source=provenance_source,
        )
        self.metrics.add("candidates", 1)
        return candidate

    # -- confirmation ----------------------------------------------------
    def _same_candidate(self, first: dict[str, Any], second: dict[str, Any] | None) -> bool:
        if second is None:
            return False
        return self._candidate_key(first) == self._candidate_key(second)

    def confirm(self, candidate: Candidate,
                observations: list[Observation] | None = None) -> dict[str, Any] | None:
        # The first independent cold replay is sufficient when it matches.
        # A failed first replay needs two matching retries to establish an
        # intermittent candidate.
        if observations is None:
            semantic = candidate.candidate.get("kind") in ("hazard_violation", "property_violation")
            observations = [self._observe_again(candidate.testcase, semantic=semantic)]
            if not self._same_candidate(candidate.candidate,
                                        self._classify_candidate_observation(candidate, observations[0])):
                observations.extend(self._observe_again(candidate.testcase, semantic=semantic)
                                    for _ in range(2))
        matches = [self._same_candidate(candidate.candidate,
                                        self._classify_candidate_observation(candidate, observation))
                   for observation in observations]
        if not matches or not (matches[0] or (len(matches) >= 3 and all(matches[1:3]))):
            return None
        try:
            parsed = parse_testcase(candidate.testcase, self._abi_like())
            from fuzzer.corpus.frames import validate

            validate(parsed, self._abi_like())
            input_validity = "satisfies_confirmed"
        except Exception:
            parsed = None
            input_validity = "violates_contract"
        cause = "source" if candidate.candidate.get("source") else "undetermined"
        if candidate.candidate.get("oracle") == "generated_property":
            cause = "specification"
        if candidate.candidate.get("kind") == "crash" and not candidate.candidate.get("source"):
            cause = "harness" if "semantist" in (candidate.candidate.get("diagnostic") or "").lower() else "runtime"
        nature = {
            "array_access": "memory_or_bounds",
            "null_dereference": "memory_or_bounds",
            "conversion_out_of_range": "arithmetic_or_conversion",
            "division_by_zero": "arithmetic_or_conversion",
            "scan_budget": "scan_budget",
            "crash": "crash",
            "output_invariant": "functional_output",
            "generated_property": "functional_output",
        }.get(candidate.candidate.get("oracle"), "crash")
        from fuzzer.pipeline.probe_audit import audit_probe_completeness

        platform_assumptions = audit_probe_completeness(self.run_dir).get("platform_assumptions", [])
        fidelity = ("approximate" if platform_assumptions else
                    self.contract.get("semantic_fidelity", "unknown"))
        confirmed = {
            "finding_id": candidate.finding_id,
            "contract_id": self.config.get("contract_id"),
            "function": self.function,
            "target_binary": str(self.target_binary),
            "oracle": candidate.candidate.get("oracle"),
            "property_id": candidate.candidate.get("property_id"),
            "property_spec": candidate.candidate.get("property_spec"),
            "goal": {
                "runtime_id": candidate.candidate.get("runtime_id"),
                "stable_id": None,
                "source": candidate.candidate.get("source"),
            },
            "input_validity": input_validity,
            "cause_location": cause,
            "cause_evidence": candidate.candidate.get("diagnostic") or (
                f"native typed predicate: {candidate.candidate['observed_predicate']}"
                if candidate.candidate.get("observed_predicate") else "runtime semantic probe"),
            "platform_evidence": ("platform_assumption_model_incomplete" if platform_assumptions
                                  else "declared_profile"),
            "platform_assumptions": platform_assumptions,
            "vulnerability_status": (
                "functional_requirement_violated_under_model"
                if cause == "specification" and not platform_assumptions
                and (candidate.candidate.get("property_spec") or {}).get("precision") == "exact"
                else "hypothesis_violation_under_model" if cause == "specification"
                else "not_established" if platform_assumptions or cause != "source"
                else "source_risk_confirmed_under_model"),
            "nature": nature,
            "history_evidence": ("history_dependent" if len(parsed.frames) > 1 else "single_cycle")
                                if parsed else "undetermined",
            "confirmation": "repeatable",
            "support_status": self.contract.get("support_status", "unknown"),
            "execution_status": {"build": "completed", "native": "confirmed", "replay": "repeatable"},
            "semantic_fidelity": fidelity,
            "stub_used": self.contract.get("stub_used", []),
            "model_scope": candidate.provenance.get("solver_scope") or self.contract.get("model_scope", {}),
            "first_candidate_s": candidate.created_s,
            "confirmed_s": round(time.monotonic() - self.started, 3),
            "provenance": candidate.provenance,
        }
        return confirmed

    def _write_repro(self, candidate: Candidate, finding: dict[str, Any]) -> None:
        directory = self.run_dir / "findings" / "confirmed" / candidate.finding_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "finding.json").write_text(
            json.dumps(finding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (directory / "testcase.txt").write_bytes(candidate.testcase)
        signature = (
            candidate.candidate.get("oracle"),
            candidate.candidate.get("runtime_id"),
            str(candidate.candidate.get("source")),
        )
        if candidate.candidate.get("property_id"):
            signature += (candidate.candidate["property_id"],)
        minimized_data = candidate.testcase
        try:
            observed_depth = len(parse_testcase(candidate.testcase, self._abi_like()).frames)
        except Exception:
            observed_depth = None
        history: dict[str, Any] = {
            "observed_depth": observed_depth,
            "shortest_found_depth": observed_depth,
            "required_frames": [],
            "ablation": [],
            "time_dependency": False,
            "initialization_scope": "cold",
        }
        if (self.config.get("minimize", True)
                and not self.config.get("ablations", {}).get("no_minimization")):
            minimization_deadline = min(self.deadline - 0.5, time.monotonic() + 5.0)

            def bounded_probe(_target_binary, testcase, *, work_dir, timeout_s):
                return self._probe_with_deadline(
                    testcase, timeout_s=timeout_s, deadline=minimization_deadline
                )

            try:
                minimized = minimize_module.minimize(
                    candidate.testcase,
                    abi_like=self._abi_like(),
                    runtime_index=self.model.runtime_index,
                    target_binary=self.target_binary,
                    work_dir=self.run_dir / "probe",
                    probe_fn=bounded_probe,
                    signature=signature,
                    classify_fn=lambda observation, data: self._classify_testcase(
                        observation, data,
                        expected_runtime_id=(candidate.candidate.get("runtime_id")
                                             if candidate.candidate.get("kind") == "hazard_violation"
                                             else None),
                        expected_property_id=candidate.candidate.get("property_id")),
                    deadline=minimization_deadline,
                )
                minimized_data = minimized.testcase
                history["minimization"] = {
                    "frames": minimized.frames,
                    "removed_frames": minimized.removed,
                    "simplified_values": minimized.simplified,
                    "depth": minimized.depth,
                }
                history["minimization_status"] = "completed"
                if minimized.depth is not None:
                    history["shortest_found_depth"] = minimized.depth
                try:
                    ablation = minimize_module.history_ablation(
                        minimized_data,
                        abi_like=self._abi_like(),
                        runtime_index=self.model.runtime_index,
                        target_binary=self.target_binary,
                        work_dir=self.run_dir / "probe",
                        probe_fn=bounded_probe,
                        signature=signature,
                        classify_fn=lambda observation, data: self._classify_testcase(
                            observation, data,
                            expected_runtime_id=(candidate.candidate.get("runtime_id")
                                                 if candidate.candidate.get("kind") == "hazard_violation"
                                                 else None),
                            expected_property_id=candidate.candidate.get("property_id")),
                        deadline=minimization_deadline,
                    )
                    history.update({key: value for key, value in ablation.items()
                                    if key not in {"observed_depth", "shortest_found_depth"}})
                    history["minimized_observed_depth"] = ablation.get("observed_depth")
                    history["ablation_status"] = "completed"
                    history["time_dependency"] = bool(
                        self.time_dependent and history.get("required_frames")
                    )
                except Exception as error:  # noqa: BLE001 - retain a confirmed minimized testcase
                    history["ablation_status"] = "TIMEOUT" if isinstance(error, TimeoutError) else "FAILED"
                    history["ablation_error"] = str(error)
            except Exception as error:  # noqa: BLE001 - minimization must not lose the finding
                history["minimization_status"] = "TIMEOUT" if isinstance(error, TimeoutError) else "FAILED"
                history["minimization_error"] = str(error)
        (directory / "minimized.txt").write_bytes(minimized_data)
        try:
            minimized_case = parse_testcase(minimized_data, self._abi_like())
            finding["depth"] = len(minimized_case.frames)
            finding["minimized_depth"] = len(minimized_case.frames)
        except Exception:
            finding["depth"] = history.get("shortest_found_depth")
        (directory / "finding.json").write_text(
            json.dumps(finding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (directory / "history.json").write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        trace_lines = []
        for index, state in enumerate(candidate.observation.states):
            trace_lines.append(json.dumps({"frame": index, "state": state}, sort_keys=True))
        (directory / "trace.txt").write_text("\n".join(trace_lines) + ("\n" if trace_lines else ""), encoding="utf-8")
        contract = self.bundle / "execution-contract.json"
        if contract.exists():
            shutil.copy2(contract, directory / "execution-contract.json")
        if self.target_binary.is_file():
            binary_dir = directory / "binary"
            binary_dir.mkdir(exist_ok=True)
            shutil.copy2(self.target_binary, binary_dir / "fuzz_target")
        source_dir = directory / "source"
        source_dir.mkdir(exist_ok=True)
        shutil.copy2(self.st_file, source_dir / "target.st")
        abi_file = self.bundle / "abi.json"
        if abi_file.is_file():
            shutil.copy2(abi_file, source_dir / "abi.json")
        resources: list[dict[str, Any]] = [{"kind": "source", "path": "source/target.st"}]
        for key, label in (("compatibility_manifest", "compatibility-manifest"),
                           ("library", "library"), ("stubs", "stubs")):
            configured = self.config.get(key)
            if not configured:
                continue
            original = Path(str(configured))
            if not original.exists():
                continue
            destination_root = directory / "source" / label
            # A compatibility manifest contains relative paths to normalized
            # sources, native providers and sometimes the standard library.
            # Copy its whole parent tree so the replay package remains
            # self-contained when moved to another directory.
            if key == "compatibility_manifest" and original.is_file():
                destination = destination_root / original.name
            else:
                destination = destination_root / original.name if original.is_dir() else source_dir / f"{label}.json"
            try:
                if key == "compatibility_manifest" and original.is_file():
                    destination_root.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(original.parent, destination_root, dirs_exist_ok=True)
                elif original.is_dir():
                    shutil.copytree(original, destination)
                else:
                    shutil.copy2(original, destination)
            except OSError:
                continue
            resources.append({"kind": key, "path": str(destination.relative_to(directory))})
        for name in ("stg-model.json", "stg-runtime-ids.json"):
            source = self.run_dir / "stg" / name
            if source.exists():
                shutil.copy2(source, source_dir / name)
        (directory / "repro-manifest.json").write_text(
            json.dumps({
                "schema_version": "semantist.repro/1",
                "function": self.function,
                "resources": resources,
                "profile": self.config.get("profile"),
                "contract_id": self.config.get("contract_id"),
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        replay = directory / "replay.sh"
        replay.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"\n'
            f'exec python3 -m fuzzer.pipeline.replay "$DIR"\n',
            encoding="utf-8",
        )
        replay.chmod(0o755)

    def _run_confirmation(self) -> None:
        confirmed_ids = {item.get("finding_id") for item in self.confirmed if isinstance(item, dict)}
        confirmation_results = getattr(self, "candidate_confirmation_results", None)
        if confirmation_results is None:
            confirmation_results = self.candidate_confirmation_results = []
        reviewed_ids = {item.get("finding_id") for item in confirmation_results if isinstance(item, dict)}
        candidates = [item for item in self.candidates
                      if item.finding_id not in confirmed_ids and item.finding_id not in reviewed_ids]
        if not candidates:
            return
        workers = max(1, int(getattr(self, "jobs", 1)))

        # Retry only candidates whose first independent cold replay missed.
        observations: list[list[Observation]] = [[] for _ in candidates]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="semantist-confirm") as pool:
            futures = [
                    (candidate_index, pool.submit(
                        self._observe_again, candidate.testcase,
                        semantic=candidate.candidate.get("kind") in
                        ("hazard_violation", "property_violation")))
                for candidate_index, candidate in enumerate(candidates)
            ]
            for candidate_index, future in futures:
                observations[candidate_index].append(future.result())
            retries = [
                (candidate_index, pool.submit(
                    self._observe_again, candidate.testcase,
                        semantic=candidate.candidate.get("kind") in
                        ("hazard_violation", "property_violation")))
                for candidate_index, candidate in enumerate(candidates)
                if not self._same_candidate(candidate.candidate,
                                            self._classify_candidate_observation(
                                                candidate, observations[candidate_index][0]))
                for _attempt in range(2)
            ]
            for candidate_index, future in retries:
                observations[candidate_index].append(future.result())
        self.metrics.event("confirmation_probes", candidates=len(candidates),
                           probes=sum(map(len, observations)), workers=workers)
        for candidate, candidate_observations in zip(candidates, observations):
            initial_fast_match = any(
                self._same_candidate(candidate.candidate,
                                     self._classify_candidate_observation(candidate, observation))
                for observation in candidate_observations
            )
            shortening: dict[str, Any] = {
                "status": "skipped",
                "reason": "native_mismatch" if not initial_fast_match
                else "deferred_to_report",
            }
            finding = self.confirm(candidate, observations=candidate_observations)
            attempts = []
            for observation in candidate_observations:
                actual = self._classify_candidate_observation(candidate, observation)
                attempts.append({
                    "round": 1,
                    "status": ("TIMEOUT" if observation.timed_out else
                                "INSTRUMENTATION_ABORT" if actual is not None
                                and actual.get("kind") == "scan_budget" else
                                "MATCH" if self._same_candidate(candidate.candidate, actual)
                                else "REPLAY_MISMATCH"),
                    "returncode": observation.returncode,
                    "wall_s": observation.wall_s,
                    "risk_probe_hits": [int(runtime_id) for runtime_id in observation.hits],
                    "observed_oracle": actual.get("oracle") if actual else None,
                    "observed_runtime_id": actual.get("runtime_id") if actual else None,
                })
            status = "confirmed" if finding is not None else (
                "TIMEOUT" if any(item["status"] == "TIMEOUT" for item in attempts)
                else "INSTRUMENTATION_ABORT" if any(
                    item["status"] == "INSTRUMENTATION_ABORT" for item in attempts)
                else "REPLAY_MISMATCH")
            evidence = {
                "finding_id": candidate.finding_id,
                "status": status,
                "initial_risk_probe_hits": [int(runtime_id) for runtime_id in candidate.observation.hits],
                "initial_oracle": candidate.candidate.get("oracle"),
                "shortening": shortening,
                "attempts": attempts,
            }
            confirmation_results.append(evidence)
            (self.run_dir / "findings" / "candidates" /
             f"{candidate.finding_id}.confirmation.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if finding is None:
                continue
            self.confirmed.append(finding)
            self._write_repro(candidate, finding)
            self.metrics.event("confirmed", finding_id=candidate.finding_id, oracle=finding["oracle"])
            self.metrics.add("confirmed", 1)

    # -- report ----------------------------------------------------------
    def _write_report(self) -> None:
        from fuzzer.pipeline.probe_audit import audit_probe_completeness

        probe_audit = audit_probe_completeness(self.run_dir)
        (self.run_dir / "probe-completeness.json").write_text(
            json.dumps(probe_audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        query_statuses = [str(item.get("status", "FAILED")) for item in self.symbolic_results]
        native_replay_statuses = [str(item.get("native_replay_status", ""))
                                  for item in self.symbolic_results]
        status_counts = {
            "candidate": len(self.candidates),
            "confirmed": len(self.confirmed),
            **{
                status: sum(1 for value in query_statuses if value == status)
                for status in ("FAILED", "ERROR", "UNSUPPORTED", "TIMEOUT", "REPLAY_MISMATCH")
            },
        }
        status_counts["REPLAY_MISMATCH"] += sum(
            status == "REPLAY_MISMATCH" for status in native_replay_statuses
        )
        for status in ("TIMEOUT", "REPLAY_MISMATCH"):
            status_counts[status] += sum(item.get("status") == status for item in
                                         getattr(self, "candidate_confirmation_results", []))
        for status in ("FAILED", "TIMEOUT", "REPLAY_MISMATCH"):
            status_counts[status] += sum(item.get("status") == status
                                         for item in getattr(self, "native_replay_results", []))
        symbolic_status = "completed" if query_statuses else "NOT_RUN"
        symbolic_reason = None
        ablations = self.config.get("ablations", {})
        bundle = getattr(self, "bundle", None)
        unsupported_path = Path(bundle) / "klee" / "UNSUPPORTED.json" if bundle else None
        if unsupported_path is not None and unsupported_path.is_file() and not query_statuses:
            try:
                unsupported = json.loads(unsupported_path.read_text(encoding="utf-8"))
                symbolic_reason = str(unsupported.get("reason") or "symbolic_model_unavailable")
                detail = unsupported.get("detail")
                if detail:
                    symbolic_reason += f": {detail}"
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                symbolic_reason = "symbolic_model_unavailable"
            symbolic_status = "UNSUPPORTED"
        if ablations.get("no_symbolic"):
            # No query was requested by the caller.  Keep this distinct from
            # a completed solver phase so batch reports cannot turn an
            # intentional native-only run into a clean symbolic result.
            symbolic_status = "UNSUPPORTED"
            symbolic_reason = "disabled_by_no_symbolic_ablation"
        if any(status == "TIMEOUT" for status in query_statuses) or any(
                status == "TIMEOUT" for status in native_replay_statuses):
            symbolic_status = "TIMEOUT"
            symbolic_reason = None
        elif any(status == "REPLAY_MISMATCH" for status in query_statuses) or any(
                status == "REPLAY_MISMATCH" for status in native_replay_statuses):
            symbolic_status = "REPLAY_MISMATCH"
            symbolic_reason = None
        elif any(status in {"FAILED", "ERROR"} for status in query_statuses):
            symbolic_status = "FAILED"
            symbolic_reason = None
        elif any(status == "UNSUPPORTED" for status in query_statuses):
            symbolic_status = "UNSUPPORTED"
            symbolic_reason = None
        elif any(status == "UNKNOWN" for status in query_statuses):
            symbolic_status = "INCOMPLETE"
            symbolic_reason = "symbolic_query_unresolved"
        model_result_counts = {"faithful": 0, "approximate": 0, "unknown": 0}
        for item in self.symbolic_results:
            status = str(item.get("status", ""))
            if status not in {"SAT", "SAT_APPROX", "UNSAT", "UNKNOWN"}:
                continue
            scope = item.get("model_scope") or {}
            if status == "UNKNOWN":
                model_result_counts["unknown"] += 1
                continue
            approximate = status == "SAT_APPROX" or self._scope_is_approximate(scope)
            model_result_counts["approximate" if approximate else "faithful"] += 1
        confirmed_model_counts = {"faithful": 0, "approximate": 0, "unknown": 0}
        for finding in self.confirmed:
            scope = finding.get("model_scope") or {}
            fidelity = str(finding.get("semantic_fidelity") or
                           self.contract.get("semantic_fidelity") or "").lower()
            if self._scope_is_approximate(scope) or fidelity == "approximate" or finding.get("stub_used"):
                key = "approximate"
            elif fidelity == "semantic":
                key = "faithful"
            else:
                key = "unknown"
            confirmed_model_counts[key] += 1
        execution_status = {"build": "completed", "native": getattr(self, "engine_status", "completed"),
                            "symbolic": symbolic_status}
        if symbolic_reason:
            execution_status["symbolic_reason"] = symbolic_reason
        engine_progress = []
        engine_summary_path = self.run_dir / "metrics" / "engine-summary.json"
        try:
            engine_progress = json.loads(engine_summary_path.read_text(encoding="utf-8")).get("task_progress", [])
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            pass
        progress_by_task = {item.get("task_id"): dict(item) for item in engine_progress
                            if isinstance(item, dict) and item.get("task_id")}
        for task_id, progress in progress_by_task.items():
            progress["klee_queries_used"] = sum(item.get("task_id") == task_id
                                                for item in self.symbolic_results)
            progress["klee_queries_remaining"] = max(
                0, getattr(self, "_solver_task_limits", {}).get(task_id, self.max_evaluations)
                - progress["klee_queries_used"])
            progress["klee_query_statuses"] = [item.get("status") for item in self.symbolic_results
                                               if item.get("task_id") == task_id]
            progress["remaining_time_s"] = max(0.0, self.deadline - time.monotonic())
        metric_snapshot = self.metrics.summary()
        event_counts = metric_snapshot.get("events", {})
        solver_feedback = {
            "enqueued": metric_snapshot.get("solver_seeds_enqueued", 0),
            "executed_by_engine": metric_snapshot.get("solver_seeds_executed", 0),
            "retained_by_engine": metric_snapshot.get("solver_seeds_retained", 0),
            "engine_finished_no_feedback_this_round": event_counts.get("solver_seed_not_fed", 0),
        }
        summary = {
            "function": self.function,
            "contract_id": self.config.get("contract_id"),
            "candidates": len(self.candidates),
            "confirmed": len(self.confirmed),
            "source_risk_confirmed_under_model": sum(
                item.get("vulnerability_status") == "source_risk_confirmed_under_model"
                for item in self.confirmed),
            # Keep finding and solver outcomes independently consumable by
            # report/batch clients; the raw query list remains authoritative.
            "status_counts": status_counts,
            "probe_audit": {key: value for key, value in probe_audit.items() if key != "risks"},
            "verification_status": "incomplete" if probe_audit["status"] != "COMPLETE"
            or symbolic_status in {"FAILED", "UNSUPPORTED", "TIMEOUT", "REPLAY_MISMATCH", "NOT_RUN", "INCOMPLETE"}
            or any(item.get("status") in {"FAILED", "TIMEOUT", "REPLAY_MISMATCH", "INSTRUMENTATION_ABORT"}
                   for item in getattr(self, "native_replay_results", []))
            or any(item.get("status") in {"FAILED", "TIMEOUT", "REPLAY_MISMATCH", "INSTRUMENTATION_ABORT"}
                   for item in getattr(self, "candidate_confirmation_results", [])) else "executed",
            "candidate_confirmation_results": getattr(self, "candidate_confirmation_results", []),
            "findings": self.confirmed,
            "support_status": self.contract.get("support_status", "unknown"),
            "execution_status": execution_status,
            "semantic_fidelity": ("approximate" if probe_audit["platform_assumptions"] else
                                  self.contract.get("semantic_fidelity", "unknown")),
            "stub_used": self.contract.get("stub_used", []),
            "model_scope": self.contract.get("model_scope", {}),
            "model_result_counts": model_result_counts,
            "confirmed_model_counts": confirmed_model_counts,
            "symbolic_queries": self.symbolic_results,
            "solver_feedback": solver_feedback,
            "cycle_effects": getattr(self, "cycle_effects", {}),
            "llm_planning": getattr(self, "llm_planning_summary", {"status": "not_run"}),
            "phase_times_s": getattr(self, "phase_times_s", {}),
            "online_start_offset_s": getattr(self, "online_start_offset_s", None),
            "total_budget_s": getattr(self, "time_budget_s", None),
            "task_progress": list(progress_by_task.values()),
            "native_replay_results": getattr(self, "native_replay_results", []),
            "metrics": metric_snapshot,
        }
        (self.run_dir / "report.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        lines = [
            f"# SemantiST report: {self.function}",
            "",
            f"- candidates: {len(self.candidates)}",
            f"- confirmed: {len(self.confirmed)}",
            f"- contract: `{self.config.get('contract_id')}`",
            "",
            "## Findings",
            "",
        ]
        if self.confirmed:
            lines += [
                "| id | oracle | runtime | risk status | cause | validity | history |",
                "| --- | --- | ---: | --- | --- | --- | --- |",
            ]
            for finding in self.confirmed:
                lines.append(
                    f"| {finding['finding_id']} | {finding['oracle']} | "
                    f"{finding['goal'].get('runtime_id')} | {finding.get('vulnerability_status', 'not_established')} | "
                    f"{finding['cause_location']} | "
                    f"{finding['input_validity']} | {finding['history_evidence']} |"
                )
        else:
            lines.append("No confirmed finding.")
        (self.run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- main ------------------------------------------------------------
    @staticmethod
    def _startup_query_timeout(remaining_s: float, later_tasks: int,
                               native_guided: bool) -> float:
        reserve_s = 5.0 + min(5.0, max(0, later_tasks) * 2.0)
        return min(10.0 if native_guided else 5.0,
                   max(0.001, remaining_s - reserve_s))

    @staticmethod
    def _float_conversion_anchor(task: dict[str, Any], abi: dict[str, Any],
                                 runtime_index: dict[int, dict[str, Any]]) -> tuple[str, str] | None:
        goal_id = (task.get("goal") or {}).get("runtime_id")
        predicate = (runtime_index.get(goal_id, {}).get("violation_predicate") or {})
        if predicate.get("kind") != "float_to_integer":
            return None
        width = predicate.get("target_bit_width")
        if width not in (8, 16, 32, 64) or predicate.get("signed") is None:
            return None
        boundary = float(1 << (width - int(bool(predicate["signed"]))))
        controllable = set(task.get("controllable_fields") or [])
        for entry in abi.get("frame_fields", []):
            if entry.get("name") in controllable and (entry.get("spec") or {}).get("name") in {"REAL", "LREAL"}:
                return entry["name"], f"{boundary:.1f}"
        return None

    def run_symbolic_startup(self) -> None:
        ablations = self.config.get("ablations", {})
        # `symbolic_initial_only` keeps this startup pass and disables only
        # live prefix queries.
        if ablations.get("no_symbolic"):
            return
        klee_dir = self.bundle / "klee"
        if not (klee_dir / "manifest.json").exists():
            return
        from compiler.symbolic.solver import run_query

        abi = json.loads((self.bundle / "abi.json").read_text(encoding="utf-8"))
        goal_tasks = json.loads((self.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
        base_frame = self._default_action_frame(abi)
        shared_records = self._shared_records_for_frames(abi, [base_frame])
        attempted = 0
        # A caller-provided zero is a real budget: do not silently turn it
        # into one symbolic query.
        query_budget = min(
            max(0, int(self.max_evaluations)),
            max(0, int(self.config.get("symbolic_startup_max_queries", 8))),
        )
        suggestions = getattr(self, "llm_symbolic_suggestions", {})
        eligible_tasks = [task for task in goal_tasks.get("tasks", [])
                          if (task.get("goal") or {}).get("hazard") in (
                              "array_access", "pointer_access", "division", "modulo",
                              "dangerous_conversion")
                          and task.get("symbolic_suitable")
                          and (task.get("goal") or {}).get("runtime_id") is not None]
        ordered_tasks = sorted(eligible_tasks,
                               key=lambda item: not bool(suggestions.get(str(item.get("task_id")))))
        for task_index, task in enumerate(ordered_tasks):
            if time.monotonic() >= self.symbolic_startup_deadline or attempted >= query_budget:
                break
            goal = task.get("goal") or {}
            runtime_id = goal.get("runtime_id")
            anchor = self._float_conversion_anchor(task, abi, self.model.runtime_index)
            previous_status = None
            for variant in ("symbolic", "typed_float_anchor"):
                if variant == "typed_float_anchor" and (anchor is None or previous_status not in {"UNKNOWN", "UNSAT"}):
                    break
                if time.monotonic() >= self.symbolic_startup_deadline or attempted >= query_budget:
                    break
                if not self._claim_solver_budget(str(task.get("task_id"))):
                    break
                attempted += 1
                query_id = f"q-start-{attempted}-{runtime_id}"
                query_frame = dict(base_frame)
                suggested = suggestions.get(str(task.get("task_id"))) or []
                symbolic_fields = [entry["name"] for entry in abi.get("frame_fields", [])
                                   if entry["name"] in suggested] or None
                if variant == "typed_float_anchor" and anchor is not None:
                    query_frame[anchor[0]] = anchor[1]
                    symbolic_fields = [entry["name"] for entry in abi.get("frame_fields", [])
                                       if entry["name"] != anchor[0]
                                       and (not suggested or entry["name"] in suggested)]
                    self.metrics.event("solver_trigger", query_id=query_id, task_id=task.get("task_id"),
                                       reason="typed_float_conversion_boundary",
                                       fixed_field=anchor[0], fixed_value=anchor[1])
                remaining_s = self.symbolic_startup_deadline - time.monotonic()
                query_timeout_s = self._startup_query_timeout(
                    remaining_s, len(ordered_tasks) - task_index - 1,
                    bool(suggestions.get(str(task.get("task_id")))))
                try:
                    result = run_query(
                        bundle=self.bundle, run_dir=self.run_dir,
                        target_binary=self.target_binary, abi=abi,
                        runtime_ids=self.run_dir / "stg" / "stg-runtime-ids.json",
                        goal_id=int(runtime_id), query_id=query_id,
                        task_id=task.get("task_id"), base_frame=query_frame,
                        symbolic_fields=symbolic_fields, shared_records=shared_records,
                        dt_ns=int(self.config.get("scan_period_ns") or 10_000_000),
                        time_budget_s=query_timeout_s,
                    )
                except Exception as error:  # noqa: BLE001 - native fuzz remains available
                    result = {"status": "FAILED", "reason": str(error)}
                previous_status = result.get("status")
                result, solver_testcase = self._solver_candidate(result)
                self._record_symbolic_result(query_id, task.get("task_id"), result)
                self.metrics.event("solver_result", query_id=query_id, status=result.get("status"),
                                   runtime_id=runtime_id)
                if solver_testcase is None:
                    continue
                self._enqueue_solver_seed(solver_testcase,
                                          query_id=query_id, task_id=task.get("task_id"))
                self._offer_additional_solver_seeds(
                    result, deadline=self.symbolic_startup_deadline,
                    query_id=query_id, task_id=task.get("task_id"))
                if result.get("status") not in {"SAT", "SAT_APPROX"}:
                    continue
                observation = self._probe_with_deadline(
                    solver_testcase, timeout_s=5.0, deadline=self.symbolic_startup_deadline)
                candidate_info = classify(observation, self.model.runtime_index)
                replay_status = self._record_klee_native_replay(
                    prefix_id="cold", query_id=query_id, task_id=task.get("task_id"),
                    observation=observation,
                    matched=is_finding_candidate(candidate_info) and int(runtime_id) in observation.hits,
                    model_goal_reached=bool(result.get("native_goal_reached"))
                    and int(runtime_id) in observation.hits,
                )
                self._mark_native_replay_status(query_id, replay_status)
                if is_finding_candidate(candidate_info) and int(runtime_id) in observation.hits:
                    self._register_candidate(
                        solver_testcase, observation, candidate_info,
                        provenance_source="solver",
                        depth=self._query_result_depth(result, solver_testcase, 1),
                        query_id=query_id, task_id=task.get("task_id"),
                        solver_scope=result.get("model_scope"))
                    self.metrics.event("goal_reached", task_id=task.get("task_id"), depth=1,
                                       oracle=candidate_info.get("oracle"), source="solver")
        if attempted:
            self.metrics.event("symbolic_startup", attempted=attempted,
                               sat=sum(1 for candidate in self.candidates if candidate.provenance.get("source") == "solver"))
        if attempted >= query_budget:
            self.metrics.event("solver_budget_exhausted", phase="startup",
                               queries=attempted, max_evaluations=query_budget)

    def _run_live_prefix_solver(self, stop_event: threading.Event) -> None:
        """Consume newly reachable prefixes while the native engine is live.

        Queries use cold prefix replay. KLEE can suggest a testcase, but only
        the native probe can register a candidate or finding.
        """
        ablations = self.config.get("ablations", {})
        if ablations.get("no_symbolic") or ablations.get("symbolic_initial_only"):
            return
        from compiler.symbolic.solver import run_query

        queue_index = self.run_dir / "prefixes" / "klee-queue.jsonl"
        try:
            goals = json.loads((self.bundle / "goal-tasks.json").read_text(encoding="utf-8"))
            abi = json.loads((self.bundle / "abi.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
            self.metrics.event("solver_monitor", status="FAILED", reason=str(error)[:400])
            return
        tasks = [task for task in goals.get("tasks", [])
                 if task.get("symbolic_suitable") and task.get("goal_class") == "defect"
                 and (task.get("goal") or {}).get("runtime_id") is not None]
        single_cycle = bool(goals.get("single_cycle"))
        if not tasks:
            self.metrics.event("solver_monitor", status="UNSUPPORTED", reason="no_symbolic_tasks")
            return
        frame_fields = [entry["name"] for entry in abi.get("frame_fields", [])]
        frame_fields.extend(entry["name"] for entry in abi.get("shared_fields", [])
                            if entry.get("block_kind") == "VAR_IN_OUT")
        suggestions = getattr(self, "llm_symbolic_suggestions", {})
        hints = getattr(self, "llm_solver_hints", {})
        task_queries: dict[str, int] = {}
        task_failures: dict[str, int] = {}
        next_execution: dict[str, int] = {}
        solver_deadline = getattr(self, "solver_deadline", self.engine_deadline)
        recent_costs_s: list[float] = []
        minimum_margin_s = max(1.0, float(self.config.get("solver_feedback_margin_s", 15.0)))
        seen: set[tuple[str, str | None]] = set()
        processed = 0
        max_online_queries = max(0, int(self.config.get("online_symbolic_max_queries", 4)))
        budget_exhausted = False
        if max_online_queries == 0:
            self.metrics.event("solver_monitor", status="DISABLED",
                               reason="online_symbolic_query_budget_zero")
            return
        self.metrics.event(
            "solver_scheduler", phase="engine_live", workers=1,
            mode="rust_klee_queue",
        )
        while (not stop_event.is_set() and not budget_exhausted
               and processed < max_online_queries
               and time.monotonic() < solver_deadline):
            if not queue_index.exists():
                stop_event.wait(0.05)
                continue
            try:
                lines = queue_index.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                stop_event.wait(0.05)
                continue
            found = False
            records = []
            for line in lines:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # The Rust append may be observed between writes. Retry
                    # the line on the next poll instead of dropping it.
                    continue
            for record in sorted(records, key=lambda item: (
                    int((hints.get(item.get("task_id")) or {}).get("priority", 50) <= 25),
                    solver_prefix_priority(item)), reverse=True):
                if stop_event.is_set() or time.monotonic() >= solver_deadline:
                    break
                prefix_id = record.get("prefix_id")
                task_key = record.get("task_id") if isinstance(record.get("task_id"), str) else None
                frames_ref = record.get("frames_file")
                if not isinstance(prefix_id, str) or not prefix_id:
                    self.metrics.event("prefix_record_invalid", phase="engine_live", status="FAILED",
                                       reason="missing_prefix_id")
                    continue
                if (prefix_id, task_key) in seen:
                    continue
                if not isinstance(frames_ref, str) or not frames_ref:
                    self.metrics.event("prefix_record_invalid", phase="engine_live", status="FAILED",
                                       prefix_id=prefix_id, reason="missing_frames_file")
                    continue
                frames_file = self.run_dir / frames_ref
                if not frames_file.is_file():
                    self.metrics.event("prefix_record_invalid", phase="engine_live", status="FAILED",
                                       prefix_id=prefix_id, frames_file=frames_ref,
                                       reason="missing_frames_file")
                    continue
                try:
                    case = parse_testcase(frames_file.read_bytes(), self._abi_like())
                except Exception as error:  # noqa: BLE001 - native fuzz continues
                    self.metrics.event("prefix_record_invalid", phase="engine_live", status="FAILED",
                                       frames_file=str(frames_file), reason=str(error)[:400])
                    continue
                if not case.frames:
                    seen.add((prefix_id, task_key))
                    continue
                seen.add((prefix_id, task_key))
                found = True
                frames = [dict(frame.values) for frame in case.frames]
                shared_records = "".join(
                    f"{name},{case.types.get(name, '')},{value}\n"
                    for name, value in case.shared.items()
                )
                prefix_dt_ns = [frame.dt_ns for frame in case.frames]
                preferred_task_id = record.get("task_id")
                if not isinstance(preferred_task_id, str):
                    self.metrics.event("solver_trigger", prefix_id=prefix_id, status="skipped",
                                       reason="missing_native_task_progress")
                    continue
                ordered_tasks = [task for task in tasks if str(task.get("task_id")) == preferred_task_id]
                if not ordered_tasks:
                    self.metrics.event("solver_trigger", prefix_id=prefix_id, status="skipped",
                                       reason="task_not_symbolically_suitable")
                    continue
                for task in ordered_tasks:
                    reason = solver_prefix_skip_reason(record, task)
                    task_id = str(task.get("task_id"))
                    allowed_triggers = (hints.get(task_id) or {}).get("triggers") or []
                    if not reason and allowed_triggers and record.get("trigger") not in allowed_triggers:
                        reason = "outside_plan_solver_triggers"
                    if reason:
                        self.metrics.event("solver_trigger", prefix_id=prefix_id,
                                           task_id=task.get("task_id"), status="skipped", reason=reason)
                        continue
                    if stop_event.is_set() or time.monotonic() >= solver_deadline:
                        break
                    native_execution = record.get("native_execution", 0)
                    if native_execution < next_execution.get(task_id, 0):
                        self.metrics.event("solver_trigger", prefix_id=prefix_id, task_id=task_id,
                                           status="skipped", reason="task_failure_backoff")
                        continue
                    if self._native_task_reached(task_id):
                        self.metrics.event("solver_trigger", prefix_id=prefix_id, task_id=task_id,
                                           status="skipped", reason="task_already_reached")
                        continue
                    task_limit = max(0, int((task.get("budget") or {}).get(
                        "solver_queries", self.max_evaluations)))
                    if task_queries.get(task_id, 0) >= task_limit:
                        self.metrics.event("solver_trigger", prefix_id=prefix_id, task_id=task_id,
                                           status="skipped", reason="task_query_budget_exhausted")
                        continue
                    remaining_s = solver_deadline - time.monotonic()
                    minimum_query_s = max(4.0, max(recent_costs_s[-5:], default=0.0))
                    if remaining_s < minimum_query_s:
                        self.metrics.event("solver_trigger", prefix_id=prefix_id, task_id=task_id,
                                           status="skipped", reason="insufficient_compile_link_solve_time",
                                           remaining_s=round(remaining_s, 3))
                        continue
                    requested = suggestions.get(task_id) or frame_fields
                    symbolic_fields = [name for name in requested if name in frame_fields]
                    if not symbolic_fields:
                        symbolic_fields = frame_fields
                    self.metrics.event("solver_trigger", prefix_id=prefix_id, task_id=task_id,
                                       status="triggered", reason=record["trigger"],
                                       native_execution=record["native_execution"],
                                       distance=record.get("distance"))
                    if not self._claim_solver_budget(task_id):
                        self.metrics.event("solver_budget_exhausted", phase="engine_live",
                                           queries=int(getattr(self, "_solver_queries_used", 0)),
                                           max_evaluations=int(self.max_evaluations))
                        budget_exhausted = True
                        break
                    task_queries[task_id] = task_queries.get(task_id, 0) + 1
                    processed += 1
                    runtime_id = int((task.get("goal") or {}).get("runtime_id"))
                    query_id = f"live-prefix-{prefix_id}-{runtime_id}"
                    query_started = time.monotonic()
                    try:
                        result = run_query(
                            bundle=self.bundle,
                            run_dir=self.run_dir,
                            target_binary=self.target_binary,
                            abi=abi,
                            runtime_ids=self.run_dir / "stg" / "stg-runtime-ids.json",
                            goal_id=runtime_id,
                            query_id=query_id,
                            task_id=task_id,
                            base_frame=frames[-1],
                            symbolic_fields=symbolic_fields,
                            shared_records=shared_records,
                            prefix_frames=None if single_cycle else frames,
                            prefix_dt_ns=None if single_cycle else prefix_dt_ns,
                            window=1 if single_cycle else 110 if task.get("timers") else 2,
                            dt_ns=int(self.config.get("scan_period_ns") or 10_000_000),
                            time_budget_s=min(5.0, max(0.001, solver_deadline - time.monotonic())),
                        )
                    except Exception as error:  # noqa: BLE001 - native fuzz remains available
                        result = {"status": "FAILED", "reason": str(error)}
                    result, solver_testcase = self._solver_candidate(result)
                    recent_costs_s.append(time.monotonic() - query_started)
                    recent_costs_s = recent_costs_s[-5:]
                    solver_deadline = min(solver_deadline, solver_feedback_cutoff(
                        self.engine_deadline, minimum_margin_s, recent_costs_s))
                    self._record_symbolic_result(query_id, task_id, result)
                    self._record_klee_queue_result(
                        prefix_id=prefix_id,
                        query_id=query_id,
                        task_id=task_id,
                        result=result,
                    )
                    self.metrics.event("solver_result", query_id=query_id,
                                       status=result.get("status"), runtime_id=runtime_id,
                                       online=True, phase="engine_live",
                                       prefix_id=prefix_id)
                    if solver_testcase is None:
                        if result.get("status") in {"TIMEOUT", "UNSUPPORTED", "FAILED", "REPLAY_MISMATCH"}:
                            task_failures[task_id] = task_failures.get(task_id, 0) + 1
                            next_execution[task_id] = native_execution + min(
                                10000, 100 * (2 ** min(task_failures[task_id], 6)))
                        continue
                    queued = self._enqueue_solver_seed(solver_testcase,
                                                       query_id=query_id, task_id=task_id)
                    if queued:
                        task_failures[task_id] = 0
                    else:
                        task_failures[task_id] = task_failures.get(task_id, 0) + 1
                        next_execution[task_id] = native_execution + min(
                            10000, 100 * (2 ** min(task_failures[task_id], 6)))
                    self._offer_additional_solver_seeds(
                        result, deadline=self.engine_deadline,
                        query_id=query_id, task_id=task_id)
                    recent_costs_s[-1] = time.monotonic() - query_started
                    solver_deadline = min(solver_deadline, solver_feedback_cutoff(
                        self.engine_deadline, minimum_margin_s, recent_costs_s))
                    self.metrics.event("solver_feedback_cutoff", query_id=query_id,
                                       query_and_replay_s=round(recent_costs_s[-1], 3),
                                       remaining_for_feedback_s=round(
                                           self.engine_deadline - solver_deadline, 3))
                    if result.get("status") not in {"SAT", "SAT_APPROX"}:
                        continue
                    observation = self._probe_with_deadline(
                        solver_testcase, timeout_s=1.0, deadline=self.engine_deadline)
                    info = classify(observation, self.model.runtime_index)
                    native_replay_status = self._record_klee_native_replay(
                        prefix_id=prefix_id,
                        query_id=query_id,
                        task_id=task_id,
                        observation=observation,
                        matched=is_finding_candidate(info) and runtime_id in observation.hits,
                        model_goal_reached=bool(result.get("native_goal_reached"))
                        and runtime_id in observation.hits,
                    )
                    self._mark_native_replay_status(query_id, native_replay_status)
                    if is_finding_candidate(info) and runtime_id in observation.hits:
                        self._register_candidate(
                            solver_testcase, observation, info,
                            provenance_source="solver",
                            depth=self._query_result_depth(result, solver_testcase, len(frames) + 1),
                            query_id=query_id,
                            task_id=task_id,
                            solver_scope=result.get("model_scope"),
                        )
                        self.metrics.event("goal_reached", task_id=task_id,
                                           depth=self._query_result_depth(result, solver_testcase, len(frames) + 1),
                                           oracle=info.get("oracle"), source="solver",
                                           phase="engine_live")
            if not found:
                stop_event.wait(0.05)
        self.metrics.event("solver_monitor", status="stopped", phase="engine_live",
                           processed=processed,
                           reason=("online_query_budget_exhausted" if processed >= max_online_queries else
                                   "solver_budget_exhausted" if budget_exhausted else
                                   "engine_finished" if stop_event.is_set() else "deadline"))

    def run_rust_engine(self) -> dict[str, Any]:
        """Run the Rust/LibAFL in-process fuzz loop and confirm its candidates."""
        import subprocess

        symbolic_start = self.config.get("ablations", {}).get("symbolic_start", "import")
        from fuzzer.pipeline.plan_batches import run_plan_batches

        self.phase_times_s = getattr(self, "phase_times_s", {})
        phase_started = time.monotonic()
        prepared = self.config.get("prepared_llm")
        if prepared is None:
            run_plan_batches(self)
        else:
            # Planning happened once, outside this campaign's online clock.
            # Only seeds/suggestions are imported; prep candidates and metrics
            # remain in their own directory and cannot become time-zero hits.
            self.llm_planning_summary = dict(prepared.get("summary") or {})
            self.llm_symbolic_suggestions = dict(prepared.get("symbolic_suggestions") or {})
            self.llm_solver_hints = dict(prepared.get("solver_hints") or {})
            self.metrics.event("frozen_planning_import", plan_id=prepared.get("plan_id"))
        self.phase_times_s["llm_planning"] = round(time.monotonic() - phase_started, 3)
        online_started = time.monotonic()
        self.online_start_offset_s = (0.0 if self.config.get("online_budget_from_start")
                                      else online_started - self.started)
        if self.config.get("online_budget_from_start"):
            self.engine_deadline = self.deadline
            self.solver_deadline = self.engine_deadline - max(
                1.0, float(self.config.get("solver_feedback_margin_s", 15.0)))
        self.metrics.event("online_search_started")
        if not (self.config.get("ablations", {}).get("no_goal_prefix")
                or self.config.get("ablations", {}).get("no_stg_guidance")):
            self._verify_planned_prefixes()
        self._verify_property_witnesses()
        self.symbolic_startup_deadline = min(
            self.engine_deadline, time.monotonic() + max(0.0, float(
                self.config.get("symbolic_startup_budget_s", 30.0))))
        if symbolic_start == "replay":
            # Replay mode skips cold startup queries; online KLEE still uses
            # native, task-related prefixes while the engine is running.
            self.metrics.event("symbolic_startup", attempted=0, skipped=True, mode="replay")
        else:
            phase_started = time.monotonic()
            self.run_symbolic_startup()
            self.phase_times_s["symbolic_startup"] = round(time.monotonic() - phase_started, 3)
        engine_budget_s = self.engine_deadline - time.monotonic()
        if engine_budget_s <= 0:
            self.metrics.event("engine_finished", returncode=None, timed_out=True,
                               reason="campaign budget exhausted before engine")
            raise RuntimeError("campaign budget exhausted before the native engine")
        full_fuzz_duration = bool(self.config.get("full_fuzz_duration"))
        full_hybrid_duration = bool(self.config.get("full_hybrid_duration"))
        engine_runtime_s = (self.time_budget_s - 180.0 if full_fuzz_duration else
                            engine_budget_s if full_hybrid_duration else
                            max(0.001, engine_budget_s - min(3.0, max(0.25, engine_budget_s * 0.1))))
        if full_fuzz_duration and engine_budget_s < engine_runtime_s + 5.0:
            raise RuntimeError("insufficient campaign time for the requested native fuzz duration")
        engine = self.target_binary.parent / "semantist-inprocess"
        if not engine.exists():
            raise RuntimeError(
                f"in-process engine missing: {engine}; rebuild with SEMANTIST_INPROCESS_LIB"
            )
        command = [
            str(engine),
            "--target", str(self.target_binary),
            "--function", self.function,
            "--run-dir", str(self.run_dir),
            "--v3-bundle", str(self.bundle),
            "--v3-time-budget-ms", str(max(1, int(engine_runtime_s * 1000))),
            "--seed", str(self.seed),
        ]
        if full_fuzz_duration or full_hybrid_duration:
            command.append("--v3-full-duration")
        else:
            command.extend(["--fuzz-iterations", str(self.max_evaluations)])
        ablations = self.config.get("ablations", {})
        for key, flag in (
            ("no_goal_prefix", "--no-goal-prefix"),
            ("no_distance", "--no-distance"),
            ("no_time_guidance", "--no-time-guidance"),
            ("no_snapshot", "--no-snapshot"),
            ("no_state_copy", "--no-state-copy"),
        ):
            if ablations.get(key):
                command.append(flag)
        environment = os.environ.copy()
        environment["SEMANTIST_V3_RUN_DIR"] = str(self.run_dir)
        environment.pop("SEMANTIST_ONLINE_DEADLINE_NS", None)
        if self.config.get("online_budget_from_start"):
            environment["SEMANTIST_ONLINE_DEADLINE_NS"] = str(int(self.deadline * 1_000_000_000))
        environment["SEMANTIST_MUTATION_MODE"] = str(
            self.config.get("mutation_mode", environment.get("SEMANTIST_MUTATION_MODE", "combined")))
        semantic_ratio = self.config.get("semantic_ratio", environment.get("SEMANTIST_SEMANTIC_RATIO", 0.30))
        try:
            semantic_ratio = float(semantic_ratio)
        except (TypeError, ValueError):
            semantic_ratio = 0.30
        if semantic_ratio > 1.0:
            semantic_ratio /= 100.0
        environment["SEMANTIST_SEMANTIC_RATIO"] = str(semantic_ratio)
        environment.pop("SEMANTIST_EXPERIMENT_NO_STG", None)
        environment.pop("SEMANTIST_EXPERIMENT_SINGLE_FRAME", None)
        if ablations.get("no_stg_guidance"):
            environment["SEMANTIST_EXPERIMENT_NO_STG"] = "1"
        if ablations.get("single_frame"):
            environment["SEMANTIST_EXPERIMENT_SINGLE_FRAME"] = "1"
        if self.config.get("fb_max_cycles") is not None:
            environment["SEMANTIST_FB_MAX_CYCLES"] = str(self.config["fb_max_cycles"])
        if self.config.get("fb_stale_threshold") is not None:
            environment["SEMANTIST_FB_STALE_THRESHOLD"] = str(self.config["fb_stale_threshold"])
        if self.config.get("fb_state_trace"):
            environment["SEMANTIST_FB_STATE_TRACE"] = "1"
            if self.config.get("fb_state_trace_file"):
                environment["SEMANTIST_FB_STATE_TRACE_FILE"] = str(
                    self.config["fb_state_trace_file"]
                )
        for key, env_key in (
            ("no_online_reinjection", "SEMANTIST_EXPERIMENT_NO_ONLINE_REINJECTION"),
            ("no_adaptive_reset", "SEMANTIST_EXPERIMENT_NO_ADAPTIVE_RESET"),
            ("no_tail_guidance", "SEMANTIST_EXPERIMENT_NO_TAIL_GUIDANCE"),
            ("no_transition_guidance", "SEMANTIST_EXPERIMENT_NO_TRANSITIONS"),
            ("no_goal_tasks", "SEMANTIST_EXPERIMENT_NO_GOAL_TASKS"),
        ):
            if ablations.get(key):
                environment[env_key] = "1"
        asan_options = environment.get("ASAN_OPTIONS", "")
        environment["ASAN_OPTIONS"] = ":".join(filter(None, (
            asan_options, "halt_on_error=0", "abort_on_error=0",
        )))
        # These paths belong to one native probe and must not leak into the
        # long-lived engine. User contract settings such as FB cycle limits
        # remain available to the harness and engine.
        for key in (
            "SEMANTIST_SEMANTIC_TRACE_RUNTIME",
            "SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE",
            "SEMANTIST_STATE_DUMP_FILE",
        ):
            environment.pop(key, None)
        engine_summary_path = self.run_dir / "metrics" / "summary.json"
        engine_archive_path = self.run_dir / "metrics" / "engine-summary.json"
        pending_path = self.run_dir / "metrics" / "engine-pending.txt"
        try:
            engine_summary_path.unlink(missing_ok=True)
            engine_archive_path.unlink(missing_ok=True)
            pending_path.unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeError(f"could not clear prior engine summary: {error}") from error
        try:
            process = subprocess.Popen(
                command,
                env=environment,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            # A missing or non-executable engine is a campaign failure.
            self.metrics.event("engine_finished", returncode=127,
                               timed_out=False, engine=str(engine),
                               launch_error=str(error)[:400])
            raise RuntimeError(f"native engine could not start: {error}") from error
        online_stop = threading.Event()
        self._engine_stop_event = online_stop
        solver_thread = None
        solver_ablations = self.config.get("ablations", {})
        if not solver_ablations.get("no_symbolic") and not solver_ablations.get("symbolic_initial_only"):
            def solver_worker() -> None:
                try:
                    self._run_live_prefix_solver(online_stop)
                except Exception as error:  # noqa: BLE001 - native engine remains authoritative
                    self.metrics.event("solver_monitor", status="FAILED", phase="engine_live",
                                       reason=str(error)[:400])

            solver_thread = threading.Thread(
                target=solver_worker,
                name="semantist-solver-live",
                daemon=True,
            )
            solver_thread.start()
        phase_started = time.monotonic()
        try:
            launcher_guard_s = max(0.001, self.engine_deadline - time.monotonic())
            if self.config.get("online_budget_from_start"):
                launcher_guard_s += 60.0
            elif full_hybrid_duration:
                launcher_guard_s = max(0.001, engine_runtime_s) + 5.0
            stdout, stderr = process.communicate(
                timeout=launcher_guard_s
            )
            completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
            self.engine_status = "TIMEOUT"
            self.metrics.event("engine_finished", returncode=None, timed_out=True, engine=str(engine))
            self.metrics.write_summary()
            self._write_report()
            raise RuntimeError("native engine exceeded its wall-clock guard") from error
        finally:
            online_finished = time.monotonic()
            self.phase_times_s["engine_and_online_solver"] = round(
                time.monotonic() - phase_started, 3)
            online_stop.set()
            if solver_thread is not None:
                solver_thread.join(timeout=2.0)
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        self.metrics.event(
            "engine_finished",
            returncode=completed.returncode,
            engine=str(engine),
        )
        self.phase_times_s["online_search"] = round(
            min(online_finished, self.engine_deadline) - self.started
            if self.config.get("online_budget_from_start")
            else online_finished - online_started, 3)
        if self.config.get("online_budget_from_start"):
            self.phase_times_s["engine_postdeadline"] = round(
                max(0.0, online_finished - self.engine_deadline), 3)
        self.metrics.event("online_search_finished")
        review_budget_s = self.config.get("review_budget_s")
        if review_budget_s is not None:
            self.deadline = time.monotonic() + max(0.0, float(review_budget_s))
        review_started = time.monotonic()
        if completed.returncode != 0:
            self.engine_status = "FAILED"
            if pending_path.is_file():
                try:
                    testcase = pending_path.read_bytes()
                    observation = self._probe_with_deadline(testcase, timeout_s=5.0)
                    info = classify(observation, self.model.runtime_index)
                    if is_finding_candidate(info):
                        depth = len(parse_testcase(testcase, self._abi_like()).frames)
                        self._register_candidate(testcase, observation, info,
                                                 provenance_source="engine_crash_recovery", depth=depth)
                        self._run_confirmation()
                        self.metrics.event("engine_crash_replay", status="confirmed" if self.confirmed else "REPLAY_MISMATCH",
                                           returncode=completed.returncode)
                    elif info is not None and info.get("kind") == "scan_budget":
                        self.metrics.event("engine_crash_replay", status="INSTRUMENTATION_ABORT",
                                           reason="scan_budget", returncode=completed.returncode)
                    else:
                        self.metrics.event("engine_crash_replay", status="REPLAY_MISMATCH",
                                           returncode=completed.returncode)
                except Exception as error:  # noqa: BLE001 - preserve the original engine failure
                    self.metrics.event("engine_crash_replay", status="FAILED", reason=str(error)[:400],
                                       returncode=completed.returncode)
            self.metrics.write_summary()
            self._write_report()
            raise RuntimeError(f"native engine exited with status {completed.returncode}")
        try:
            engine_summary = json.loads(engine_summary_path.read_text(encoding="utf-8"))
            if not isinstance(engine_summary, dict):
                raise ValueError("engine summary must be a JSON object")
            for name in ("executions", "scans", "candidates"):
                value = engine_summary.get(name)
                if type(value) is not int or value < 0:
                    raise ValueError(f"engine summary missing nonnegative {name}")
            engine_archive_path.write_text(
                json.dumps(engine_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"invalid engine summary: {error}") from error
        self.metrics.add("executions", engine_summary["executions"])
        self.metrics.add("scans", engine_summary["scans"])
        self.metrics.add("engine_executions", engine_summary["executions"])
        self.metrics.add("engine_scans", engine_summary["scans"])
        for name in ("solver_seeds_loaded", "solver_seeds_executed", "solver_seeds_retained"):
            self.metrics.add(name, engine_summary.get(name, 0))
        self.metrics.add("snapshot_hits", engine_summary.get("cache_hits", 0))
        self.metrics.add("snapshot_saved_scans", engine_summary.get("saved_scans", 0))
        snapshot_attempts = engine_summary.get("snapshot_attempts")
        snapshot_misses = engine_summary.get("snapshot_misses")
        if snapshot_attempts is not None:
            self.metrics.add("snapshot_attempts", snapshot_attempts)
            self.metrics.add("snapshot_misses", snapshot_misses if snapshot_misses is not None
                             else max(0, snapshot_attempts - engine_summary.get("cache_hits", 0)))
        else:
            self.metrics.event("snapshot_stats_unavailable", engine="rust")
        candidate_dir = self.run_dir / "findings" / "candidates"
        if candidate_dir.is_dir():
            for path in sorted(candidate_dir.glob("candidate-*.txt")):
                testcase = path.read_bytes()
                metadata_file = path.with_suffix(".json")
                try:
                    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    metadata = {}
                runtime_id = metadata.get("runtime_id")
                expected_runtime_id = (runtime_id if metadata.get("kind") == "semantic_goal"
                                       and type(runtime_id) is int else None)
                # Candidate confirmation must use the target's native path.
                # Semantic tracing is an attribution aid and can turn parser
                # stress into a timeout (notably XML_READER's scan budget).
                observation, info = self._replay_engine_candidate(
                    testcase, expected_runtime_id=expected_runtime_id)
                replay_status = ("candidate" if is_finding_candidate(info) else
                                 "INSTRUMENTATION_ABORT" if info is not None
                                 and info.get("kind") == "scan_budget" else
                                 "TIMEOUT" if observation.timed_out
                                 else "FAILED" if observation.returncode not in (0, None)
                                 else "REPLAY_MISMATCH")
                result = {"testcase_file": str(path.relative_to(self.run_dir)), "status": replay_status}
                self.native_replay_results.append(result)
                self.metrics.event("engine_candidate_replay", **result)
                if is_finding_candidate(info):
                    previous_count = len(self.candidates)
                    try:
                        created_epoch_ns = metadata.get("created_epoch_ns")
                        created_s = ((int(created_epoch_ns) - self.started_epoch_ns) / 1e9
                                     if created_epoch_ns is not None else None)
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        created_s = None
                    self._register_candidate(
                        testcase,
                        observation,
                        info,
                        provenance_source="fuzz",
                        depth=len(parse_testcase(testcase, self._abi_like()).frames),
                        task_id=metadata.get("task_id") if isinstance(metadata.get("task_id"), str) else None,
                        created_s=max(0.0, created_s) if created_s is not None else None,
                    )
                    if len(self.candidates) > previous_count:
                        self._run_confirmation()
        self._run_confirmation()
        self.metrics.event("post_fuzz_symbolic_verification", status="skipped",
                           reason="native_confirmation_only")
        self.phase_times_s["result_review"] = round(time.monotonic() - review_started, 3)
        self.metrics.write_summary()
        self._write_report()
        return {
            "candidates": len(self.candidates),
            "confirmed": len(self.confirmed),
            "evaluations": engine_summary["executions"],
            "engine_returncode": completed.returncode,
        }

    def run(self) -> dict[str, Any]:
        self.metrics.event(
            "campaign_start",
            function=self.function,
            contract_id=self.config.get("contract_id"),
            config=self.config,
        )
        if self.config.get("engine") != "rust":
            raise RuntimeError("hybrid campaigns must use the Rust engine")
        return self.run_rust_engine()
