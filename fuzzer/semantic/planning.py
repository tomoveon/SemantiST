"""STG-native semantic task planning and runtime ranking.

This module deliberately consumes only compiler-semantic STG artifacts.  It
does not inspect Structured Text source and it never creates an alternate
target identity: every waypoint is an existing STG Stable Target ID.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


PLAN_SCHEMA = "semantist.semantic-task-plan/1.0.0"
SEED_PROFILE_SCHEMA = "semantist.seed-profile/1.0.0"
TERMINAL_KINDS = {"hazard_violation", "property_violation"}
GUIDE_KINDS = {
    "branch_outcome",
    "case_outcome",
    "loop_outcome",
    "control_transfer",
    "cycle",
    "hazard_reach",
}
MODELING_ORDER = {"exact": 0, "conservative": 1, "opaque": 2}
TASK_STATES = {"blocked", "ready", "active", "covered", "deferred", "unmapped"}


def _stable_digest(namespace: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(payload).hexdigest()[:32]}"


def _modeling_max(values: Iterable[str]) -> str:
    return max(values, key=lambda value: MODELING_ORDER.get(value, 2), default="exact")


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return raw


def _target_sort_key(target: dict[str, Any]) -> tuple[int, str]:
    kind = target.get("kind")
    order = {
        "cycle": 0,
        "branch_outcome": 1,
        "case_outcome": 1,
        "loop_outcome": 1,
        "control_transfer": 2,
        "hazard_reach": 3,
        "hazard_violation": 4,
        "property_violation": 4,
    }
    return order.get(str(kind), 9), str(target.get("id", ""))


def _expression_tree(
    expression_id: str | None,
    expressions: dict[str, dict[str, Any]],
    seen: set[str] | None = None,
) -> dict[str, Any] | None:
    if not expression_id or expression_id not in expressions:
        return None
    seen = set() if seen is None else seen
    if expression_id in seen:
        return {"recursive": True}
    seen.add(expression_id)
    expression = expressions[expression_id]
    return {
        "kind": expression.get("kind"),
        "type_name": expression.get("type_name"),
        "reads": list(expression.get("reads", [])),
        "operands": [
            tree
            for operand in expression.get("operands", [])
            if (tree := _expression_tree(operand, expressions, seen.copy())) is not None
        ],
    }


def _transfer_operation_tree(
    operation: dict[str, Any],
    expressions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expanded: dict[str, Any] = {}
    for field_name in ("kind", "target_symbol", "reads"):
        if field_name in operation:
            expanded[field_name] = operation[field_name]
    for field_name in ("target", "value"):
        expression_id = operation.get(field_name)
        if isinstance(expression_id, str):
            expanded[f"{field_name}_expression"] = _expression_tree(
                expression_id, expressions
            )
    arguments = operation.get("arguments")
    if isinstance(arguments, list):
        expanded["argument_expressions"] = [
            tree
            for expression_id in arguments
            if isinstance(expression_id, str)
            if (tree := _expression_tree(expression_id, expressions)) is not None
        ]
    return expanded


def strongly_connected_components(
    node_ids: Iterable[str], edges: Iterable[dict[str, Any]]
) -> list[list[str]]:
    """Return deterministic Tarjan SCCs for the semantic graph."""

    adjacency: dict[str, list[str]] = defaultdict(list)
    nodes = sorted(set(node_ids))
    for edge in edges:
        source = edge.get("from")
        destination = edge.get("to")
        if isinstance(source, str) and isinstance(destination, str):
            adjacency[source].append(destination)
    for destinations in adjacency.values():
        destinations.sort()

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in adjacency.get(node, []):
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] != indices[node]:
            return
        component = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        components.append(sorted(component))

    for node in nodes:
        if node not in indices:
            visit(node)
    components.sort(key=lambda component: component[0] if component else "")
    return components


def bounded_paths(
    graph: dict[str, Any],
    start: str,
    destination: str,
    *,
    max_paths: int = 8,
    max_nodes: int | None = None,
) -> list[dict[str, list[str]]]:
    """Enumerate paths without unfolding an SCC more than once.

    Nodes inside one SCC may be traversed once each, but a path cannot leave an
    SCC and later re-enter it.  This preserves a finite representation of loop
    obligations without pretending that a loop has a fixed iteration count.
    """

    nodes = [node["id"] for node in graph.get("nodes", []) if isinstance(node.get("id"), str)]
    edges = [
        edge
        for edge in graph.get("edges", [])
        if isinstance(edge.get("from"), str) and isinstance(edge.get("to"), str)
    ]
    if start not in nodes or destination not in nodes:
        return []
    components = strongly_connected_components(nodes, edges)
    component_of = {
        node: component_index
        for component_index, component in enumerate(components)
        for node in component
    }
    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["from"]].append(edge)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda edge: (edge.get("to", ""), edge.get("id", "")))

    limit = max_nodes or max(2, len(nodes) + len(components))
    queue = deque([(start, [start], [], {component_of[start]}, {start})])
    results: list[dict[str, list[str]]] = []
    while queue and len(results) < max_paths:
        node, path_nodes, path_edges, entered_components, visited_nodes = queue.popleft()
        if node == destination:
            results.append({"node_ids": path_nodes, "edge_ids": path_edges})
            continue
        if len(path_nodes) >= limit:
            continue
        current_component = component_of[node]
        for edge in adjacency.get(node, []):
            successor = edge["to"]
            successor_component = component_of[successor]
            if successor in visited_nodes:
                continue
            if (
                successor_component != current_component
                and successor_component in entered_components
            ):
                continue
            queue.append(
                (
                    successor,
                    [*path_nodes, successor],
                    [*path_edges, edge["id"]],
                    entered_components | {successor_component},
                    visited_nodes | {successor},
                )
            )
    return results


@dataclass
class SeedProfile:
    content_hash: str
    covered_target_ids: list[str] = field(default_factory=list)
    covered_targets_by_cycle: dict[str, list[str]] = field(default_factory=dict)
    cycle_ids: list[int] = field(default_factory=list)
    state_signatures: list[str] = field(default_factory=list)
    target_affinity: dict[str, float] = field(default_factory=dict)
    parent_id: str | None = None
    schema_version: str = SEED_PROFILE_SCHEMA


def build_seed_profile(
    seed: bytes,
    semantic_events: Iterable[dict[str, Any]],
    *,
    parent_id: str | None = None,
    tasks: Iterable[dict[str, Any]] = (),
) -> SeedProfile:
    covered: list[str] = []
    by_cycle: dict[str, list[str]] = defaultdict(list)
    cycles: set[int] = set()
    signatures: list[str] = []
    for event in semantic_events:
        target_id = event.get("target_id")
        if not isinstance(target_id, str) or not target_id:
            continue
        covered.append(target_id)
        cycle = event.get("cycle")
        if isinstance(cycle, int):
            cycles.add(cycle)
            by_cycle[str(cycle)].append(target_id)
        signature = event.get("state_signature") or event.get("state_hash_after")
        if isinstance(signature, str) and signature:
            signatures.append(signature)
    covered_ids = sorted(set(covered))
    affinity = {}
    covered_set = set(covered_ids)
    for task in tasks:
        task_id = task.get("task_id")
        waypoints = set(task.get("ordered_waypoints", []))
        if isinstance(task_id, str) and waypoints:
            affinity[task_id] = len(covered_set & waypoints) / len(waypoints)
    return SeedProfile(
        content_hash=hashlib.sha256(seed).hexdigest(),
        covered_target_ids=covered_ids,
        covered_targets_by_cycle={
            cycle: sorted(set(target_ids))
            for cycle, target_ids in sorted(by_cycle.items(), key=lambda item: int(item[0]))
        },
        cycle_ids=sorted(cycles),
        state_signatures=sorted(set(signatures)),
        target_affinity=affinity,
        parent_id=parent_id,
    )


def guide_target(path: Iterable[str], covered_target_ids: Iterable[str]) -> str | None:
    covered = set(covered_target_ids)
    return next((target_id for target_id in path if target_id not in covered), None)


def _remaining_path(path: list[str], covered: set[str]) -> list[str]:
    first = guide_target(path, covered)
    if first is None:
        return []
    return path[path.index(first) :]


def effort_components(
    seed_profile: dict[str, Any] | SeedProfile,
    task: dict[str, Any],
    normalization: dict[str, float] | None = None,
) -> dict[str, float]:
    profile = asdict(seed_profile) if isinstance(seed_profile, SeedProfile) else seed_profile
    covered = set(profile.get("covered_target_ids", []))
    active_path = choose_active_path(task, covered)
    remaining = set(_remaining_path(active_path, covered))
    control = {
        obligation.get("target_id")
        for obligation in task.get("control_obligations", [])
        if obligation.get("target_id")
    }
    data = {
        dependency.get("symbol")
        for dependency in task.get("data_dependencies", [])
        if dependency.get("symbol")
    }
    transfers = {
        transfer.get("edge_id")
        for transfer in task.get("state_transfer_chains", [])
        if transfer.get("edge_id")
    }
    temporal = task.get("temporal_requirements", [])
    prerequisites = set(task.get("hazard_prerequisites", []))
    covered_dependencies = set(profile.get("satisfied_dependencies", []))
    covered_transfers = set(profile.get("satisfied_transfers", []))
    completed_temporal = int(profile.get("completed_temporal_steps", 0))
    affinity = max(
        0.0,
        min(
            1.0,
            float(profile.get("target_affinity", {}).get(task.get("task_id"), 0.0)),
        ),
    )
    dependency_work = float(len(data - covered_dependencies))
    transfer_work = float(len(transfers - covered_transfers))
    if "satisfied_dependencies" not in profile:
        dependency_work *= 1.0 - affinity
    if "satisfied_transfers" not in profile and profile.get("state_signatures"):
        transfer_work *= 1.0 - affinity
    raw = {
        "ec": float(len(control & remaining)),
        "ed": dependency_work,
        "es": transfer_work,
        "et": float(max(0, len(temporal) - completed_temporal)),
        "eh": float(
            sum(target_id not in covered for target_id in prerequisites)
            + int(task.get("terminal_target_id") not in covered)
        ),
    }
    if not normalization:
        return raw
    return {
        name: value / max(1.0, float(normalization.get(name, 1.0)))
        for name, value in raw.items()
    }


def semantic_effort(
    seed_profile: dict[str, Any] | SeedProfile,
    task: dict[str, Any],
    normalization: dict[str, float] | None = None,
) -> float:
    return sum(effort_components(seed_profile, task, normalization).values())


def choose_active_path(task: dict[str, Any], covered_target_ids: Iterable[str]) -> list[str]:
    covered = set(covered_target_ids)
    paths = [
        list(candidate.get("target_ids", []))
        for candidate in task.get("candidate_paths", [])
        if candidate.get("target_ids")
    ]
    if not paths:
        paths = [list(task.get("ordered_waypoints", []))]
    return min(
        paths,
        key=lambda path: (
            len(_remaining_path(path, covered)),
            len(path),
            tuple(path),
        ),
        default=[],
    )


def task_gain(task: dict[str, Any], covered_target_ids: Iterable[str]) -> float:
    covered = set(covered_target_ids)
    exact_violations = int(
        task.get("task_kind") == "violation"
        and task.get("modeling_status") == "exact"
        and task.get("terminal_target_id") not in covered
    )
    unlocked_hazards = sum(
        target_id not in covered for target_id in task.get("unlocked_targets", [])
    )
    kinds = {
        obligation.get("kind")
        for obligation in task.get("control_obligations", [])
        if obligation.get("kind")
    }
    novelty = min(1.0, len(kinds) / max(1, len(GUIDE_KINDS)))
    return float(exact_violations + unlocked_hazards) + novelty


def rank_tasks(
    tasks: Iterable[dict[str, Any]],
    seed_profiles: Iterable[dict[str, Any] | SeedProfile],
    *,
    failed_attempts: dict[str, int] | None = None,
    rho: float = 0.25,
    normalization_by_pou: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    profiles = [asdict(profile) if isinstance(profile, SeedProfile) else profile for profile in seed_profiles]
    failed_attempts = failed_attempts or {}
    normalization_by_pou = normalization_by_pou or {}
    ranked = []
    globally_covered = {
        target_id
        for profile in profiles
        for target_id in profile.get("covered_target_ids", [])
    }
    for task in tasks:
        normalization = normalization_by_pou.get(task.get("pou", ""), {})
        candidates = [
            (
                semantic_effort(profile, task, normalization),
                str(profile.get("content_hash", "")),
                profile,
            )
            for profile in profiles
        ]
        candidates.sort(key=lambda item: (item[0], item[1]))
        minimum_effort, best_seed_id, best_profile = (
            candidates[0] if candidates else (sum(normalization.values()), None, {})
        )
        gain = task_gain(task, globally_covered)
        base_priority = gain / (1.0 + minimum_effort)
        failures = failed_attempts.get(str(task.get("task_id")), 0)
        dynamic = base_priority * math.exp(-rho * failures)
        active_path = choose_active_path(task, best_profile.get("covered_target_ids", []))
        guide = guide_target(active_path, best_profile.get("covered_target_ids", []))
        ranked.append(
            {
                **task,
                "priority": dynamic,
                "base_priority": base_priority,
                "gain": gain,
                "best_seed_id": best_seed_id,
                "active_path": active_path,
                "guide_target_id": guide,
                "remaining_obligations": _remaining_path(
                    active_path, set(best_profile.get("covered_target_ids", []))
                ),
                "prerequisites": list(task.get("hazard_prerequisites", [])),
                "unlocked_targets": list(task.get("unlocked_targets", [])),
                "failed_attempts": failures,
            }
        )
    ranked.sort(key=lambda task: (-task["priority"], task["task_id"]))
    return ranked


@dataclass
class TaskRuntime:
    task_id: str
    status: str
    failed_attempts: int = 0
    last_coverage_epoch: int = 0
    deferred_at_epoch: int | None = None

    def __post_init__(self) -> None:
        if self.status not in TASK_STATES:
            raise ValueError(f"invalid semantic task state {self.status!r}")


def refresh_task_runtime(
    runtime: TaskRuntime,
    task: dict[str, Any],
    covered_target_ids: Iterable[str],
    *,
    coverage_epoch: int,
    mapped_target_ids: Iterable[str],
) -> TaskRuntime:
    covered = set(covered_target_ids)
    mapped = set(mapped_target_ids)
    terminal = str(task.get("terminal_target_id", ""))
    if terminal and terminal not in mapped:
        runtime.status = "unmapped"
    elif terminal in covered:
        runtime.status = "covered"
    elif not task.get("ordered_waypoints") and not any(
        candidate.get("target_ids") for candidate in task.get("candidate_paths", [])
    ):
        runtime.status = "blocked"
    elif runtime.status == "deferred":
        if coverage_epoch > (runtime.deferred_at_epoch or 0):
            runtime.status = "ready"
            runtime.deferred_at_epoch = None
    elif runtime.status in {"blocked", "active"}:
        runtime.status = "ready"
    runtime.last_coverage_epoch = max(runtime.last_coverage_epoch, coverage_epoch)
    return runtime


def defer_task(runtime: TaskRuntime, *, coverage_epoch: int) -> TaskRuntime:
    runtime.failed_attempts += 1
    runtime.status = "deferred"
    runtime.deferred_at_epoch = coverage_epoch
    return runtime


class SemanticTaskPlanner:
    def __init__(
        self,
        model: dict[str, Any],
        runtime_ids: dict[str, Any],
        mapped_target_ids: Iterable[str] | None = None,
    ):
        self.model = model
        self.runtime_ids = runtime_ids
        if not str(model.get("schema_version", "")).startswith("semantist.stg/"):
            raise ValueError("unsupported or missing STG schema")
        if runtime_ids.get("schema_version") != "semantist.runtime-ids/1.0.0":
            raise ValueError("unsupported semantic runtime ID schema")
        self.runtime_by_stable = {
            entry["stable_id"]: entry
            for entry in runtime_ids.get("entries", [])
            if isinstance(entry.get("stable_id"), str)
        }
        self.mapped_target_ids = (
            set(mapped_target_ids)
            if mapped_target_ids is not None
            else set(self.runtime_by_stable)
        )

    @classmethod
    def from_files(cls, model_path: Path, runtime_ids_path: Path) -> "SemanticTaskPlanner":
        mapping_path = runtime_ids_path.with_name("stg-ir-mapping.json")
        mapped_target_ids = None
        if mapping_path.exists():
            mapping = _read_json(mapping_path)
            mapped_target_ids = [
                entry["stable_id"]
                for entry in mapping.get("mappings", [])
                if isinstance(entry.get("stable_id"), str)
            ]
        return cls(
            _read_json(model_path),
            _read_json(runtime_ids_path),
            mapped_target_ids,
        )

    def build(self) -> dict[str, Any]:
        tasks = []
        sccs: dict[str, list[list[str]]] = {}
        for pou in self.model.get("pous", []):
            pou_tasks, components = self._build_pou(pou)
            tasks.extend(pou_tasks)
            sccs[pou["pou"]["qualified_name"]] = components
        tasks.sort(key=lambda task: task["task_id"])
        normalization = self._normalization(tasks)
        return {
            "schema_version": PLAN_SCHEMA,
            "stg_schema_version": self.model.get("schema_version"),
            "runtime_id_schema_version": self.runtime_ids.get("schema_version"),
            "model_sha256": hashlib.sha256(
                json.dumps(self.model, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "target_identity": "stg_stable_target_id",
            "tasks": tasks,
            "normalization_by_pou": normalization,
            "sccs": sccs,
        }

    def write(self, path: Path) -> dict[str, Any]:
        plan = self.build()
        path.parent.mkdir(parents=True, exist_ok=True)
        pretty = os.environ.get("SEMANTIST_SEMANTIC_PLAN_PRETTY", "").lower() in {
            "1",
            "true",
            "yes",
            "debug",
        }
        serialized = (
            json.dumps(plan, indent=2, sort_keys=True)
            if pretty
            else json.dumps(plan, sort_keys=True, separators=(",", ":"))
        )
        contents = serialized + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == contents:
            return plan
        path.write_text(contents, encoding="utf-8")
        return plan

    def _build_pou(
        self, pou: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[list[str]]]:
        graph = pou.get("graph", {})
        environment = pou.get("environment", {})
        targets = list(pou.get("targets", []))
        target_by_id = {target["id"]: target for target in targets}
        targets_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for target in targets:
            targets_by_node[target["node_id"]].append(target)
        for node_targets in targets_by_node.values():
            node_targets.sort(key=_target_sort_key)
        components = strongly_connected_components(
            [node["id"] for node in graph.get("nodes", [])],
            graph.get("edges", []),
        )
        start = graph.get("entry_id")
        terminal_targets = [
            target for target in targets if target.get("kind") in TERMINAL_KINDS
        ]
        tasks = []
        claimed_waypoints: set[str] = set()
        for terminal in terminal_targets:
            paths = bounded_paths(graph, start, terminal["node_id"])
            candidate_paths = []
            for index, path in enumerate(paths):
                target_ids = self._path_targets(
                    path["node_ids"],
                    path["edge_ids"],
                    targets_by_node,
                    terminal["id"],
                )
                if terminal.get("kind") == "hazard_violation":
                    reach = self._hazard_reach(terminal, targets_by_node)
                    if reach and reach not in target_ids:
                        target_ids.insert(max(0, len(target_ids) - 1), reach)
                claimed_waypoints.update(target_ids)
                candidate_paths.append(
                    {
                        "path_id": _stable_digest(
                            "semantic-path",
                            pou["model_id"],
                            terminal["id"],
                            index,
                            *path["edge_ids"],
                        ),
                        "target_ids": target_ids,
                        "_edge_ids": path["edge_ids"],
                    }
                )
            tasks.append(
                self._task(
                    pou,
                    terminal,
                    candidate_paths,
                    task_kind="violation",
                    target_by_id=target_by_id,
                )
            )

        for target in targets:
            if target.get("kind") not in GUIDE_KINDS:
                continue
            paired_unmapped_violation = (
                target.get("kind") == "hazard_reach"
                and any(
                    candidate.get("node_id") == target.get("node_id")
                    and candidate.get("kind") == "hazard_violation"
                    and candidate["id"] not in self.mapped_target_ids
                    for candidate in targets
                )
            )
            if target["id"] in claimed_waypoints and not paired_unmapped_violation:
                continue
            paths = bounded_paths(graph, start, target["node_id"], max_paths=4)
            candidate_paths = [
                {
                    "path_id": _stable_digest(
                        "semantic-path",
                        pou["model_id"],
                        target["id"],
                        index,
                        *path["edge_ids"],
                    ),
                    "target_ids": self._path_targets(
                        path["node_ids"],
                        path["edge_ids"],
                        targets_by_node,
                        target["id"],
                    ),
                    "_edge_ids": path["edge_ids"],
                }
                for index, path in enumerate(paths)
            ]
            tasks.append(
                self._task(
                    pou,
                    target,
                    candidate_paths,
                    task_kind="exploration",
                    target_by_id=target_by_id,
                )
            )
        return tasks, components

    @staticmethod
    def _path_targets(
        node_ids: list[str],
        edge_ids: list[str],
        targets_by_node: dict[str, list[dict[str, Any]]],
        terminal_id: str,
    ) -> list[str]:
        path_edges = set(edge_ids)
        target_ids = [
            target["id"]
            for node_id in node_ids
            for target in targets_by_node.get(node_id, [])
            if target.get("kind") in GUIDE_KINDS or target["id"] == terminal_id
            if target["id"] == terminal_id
            or not target.get("edge_ids")
            or bool(path_edges & set(target.get("edge_ids", [])))
        ]
        if terminal_id not in target_ids:
            target_ids.append(terminal_id)
        elif target_ids[-1] != terminal_id:
            target_ids = [target for target in target_ids if target != terminal_id]
            target_ids.append(terminal_id)
        return _dedupe(target_ids)

    @staticmethod
    def _hazard_reach(
        terminal: dict[str, Any],
        targets_by_node: dict[str, list[dict[str, Any]]],
    ) -> str | None:
        return next(
            (
                target["id"]
                for target in targets_by_node.get(terminal["node_id"], [])
                if target.get("kind") == "hazard_reach"
            ),
            None,
        )

    def _task(
        self,
        pou: dict[str, Any],
        terminal: dict[str, Any],
        candidate_paths: list[dict[str, Any]],
        *,
        task_kind: str,
        target_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        environment = pou.get("environment", {})
        graph = pou.get("graph", {})
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        expressions = {
            expression["id"]: expression
            for expression in environment.get("expressions", [])
        }
        edges = {edge["id"]: edge for edge in graph.get("edges", [])}
        symbols = {
            symbol["qualified_name"]: symbol
            for symbol in environment.get("symbols", [])
        }
        active = min(
            candidate_paths,
            key=lambda path: (len(path.get("target_ids", [])), path.get("path_id", "")),
            default={"target_ids": [], "_edge_ids": []},
        )
        ordered_waypoints = list(active.get("target_ids", []))
        path_edge_ids = {
            edge_id
            for candidate in candidate_paths
            for edge_id in candidate.get("_edge_ids", candidate.get("edge_ids", []))
        }
        runtime_candidate_paths = [
            {
                "path_id": candidate["path_id"],
                "target_ids": candidate.get("target_ids", []),
            }
            for candidate in candidate_paths
        ]
        control_obligations = []
        for edge_id in sorted(path_edge_ids):
            edge = edges.get(edge_id, {})
            if edge.get("kind") != "evaluation":
                continue
            target = next(
                (
                    candidate
                    for candidate in pou.get("targets", [])
                    if edge_id in candidate.get("edge_ids", [])
                ),
                None,
            )
            if not target:
                target = next(
                    (
                        candidate
                        for candidate in pou.get("targets", [])
                        if candidate.get("node_id") == edge.get("to")
                    ),
                    None,
                )
            if not target:
                continue
            control_obligations.append(
                {
                    "target_id": target["id"],
                    "kind": target.get("kind"),
                    "role": target.get("role"),
                    "evaluation_edge_id": edge_id,
                    "goal_expression_id": edge.get("guard"),
                    "goal_expression": _expression_tree(edge.get("guard"), expressions),
                    "modeling_status": (
                        expressions.get(edge.get("guard"), {}).get("modeling", "opaque")
                    ),
                }
            )
        dependencies = list(
            environment.get("target_dependencies", {}).get(
                terminal["id"], terminal.get("backward_dependencies", [])
            )
        )
        data_dependencies = []
        for symbol_name in dependencies:
            symbol = symbols.get(symbol_name, {})
            data_dependencies.append(
                {
                    "symbol": symbol_name,
                    "role": symbol.get("role"),
                    "type_name": symbol.get("type_name"),
                    "configuration_source": symbol.get("configuration_source"),
                }
            )
        persistent = set(
            (environment.get("persistent_state") or {}).get("symbols", [])
        )
        state_transfer_chains = []
        transfer_modeling = []
        for edge_id in sorted(path_edge_ids):
            edge = edges.get(edge_id, {})
            transfer = edge.get("transfer")
            if not isinstance(transfer, dict):
                continue
            writes = set(transfer.get("writes", []))
            if not (writes & (set(dependencies) | persistent)):
                continue
            transfer_modeling.append(transfer.get("modeling", "opaque"))
            state_transfer_chains.append(
                {
                    "edge_id": edge_id,
                    "reads": sorted(transfer.get("reads", [])),
                    "writes": sorted(transfer.get("writes", [])),
                    "operations": [
                        _transfer_operation_tree(operation, expressions)
                        for operation in transfer.get("operations", [])
                        if isinstance(operation, dict)
                    ],
                    "modeling_status": transfer.get("modeling", "opaque"),
                }
            )
        temporal_requirements = []
        if persistent & set(dependencies):
            for edge in graph.get("edges", []):
                if edge.get("kind") != "temporal":
                    continue
                temporal = edge.get("temporal", {})
                temporal_requirements.append(
                    {
                        "edge_id": edge["id"],
                        "from": edge.get("from"),
                        "to": edge.get("to"),
                        "persistent_symbols": list(
                            temporal.get("persistent_symbols", [])
                        ),
                        "rule": temporal.get("rule"),
                        "minimum_cycles": 2,
                        "modeling_status": temporal.get("modeling", "opaque"),
                    }
                )
        hazard_prerequisites = []
        if terminal.get("kind") == "hazard_violation":
            reach = next(
                (
                    target["id"]
                    for target in pou.get("targets", [])
                    if target.get("node_id") == terminal.get("node_id")
                    and target.get("kind") == "hazard_reach"
                ),
                None,
            )
            if reach:
                hazard_prerequisites.append(reach)
        controllable_seed_fields = [
            {
                "symbol": name,
                "seed_field": symbols[name].get("name"),
                "role": symbols[name].get("role"),
                "type_name": symbols[name].get("type_name"),
                "configuration_source": symbols[name].get("configuration_source"),
                "type": next(
                    (
                        iec_type
                        for iec_type in environment.get("types", [])
                        if str(iec_type.get("name", "")).lower()
                        == str(symbols[name].get("type_name", "")).lower()
                    ),
                    None,
                ),
            }
            for name in dependencies
            if name in symbols and symbols[name].get("role") in {"input", "in_out"}
        ]
        modeling = _modeling_max(
            [
                terminal.get("modeling", "opaque"),
                *(item["modeling_status"] for item in control_obligations),
                *transfer_modeling,
                *(
                    item["modeling_status"]
                    for item in temporal_requirements
                ),
            ]
        )
        unlocked_targets = [
            target_id
            for candidate in candidate_paths
            for target_id in candidate.get("target_ids", [])
            if target_id != terminal["id"]
        ]
        terminal_node = nodes.get(terminal.get("node_id"), {})
        terminal_hazard = terminal_node.get("hazard") or {}
        terminal_goal_expression_id = terminal_hazard.get("expression_id")
        return {
            "task_id": _stable_digest(
                "semantic-task", pou["model_id"], task_kind, terminal["id"]
            ),
            "task_kind": task_kind,
            "pou": pou["pou"]["qualified_name"],
            "terminal_target_id": terminal["id"],
            "candidate_paths": runtime_candidate_paths,
            "ordered_waypoints": ordered_waypoints,
            "control_obligations": control_obligations,
            "data_dependencies": data_dependencies,
            "state_transfer_chains": state_transfer_chains,
            "temporal_requirements": temporal_requirements,
            "hazard_prerequisites": hazard_prerequisites,
            "controllable_seed_fields": controllable_seed_fields,
            "modeling_status": modeling,
            "initial_state": (
                "unmapped"
                if terminal["id"] not in self.mapped_target_ids
                else "blocked"
                if not candidate_paths
                else "ready"
            ),
            "unlocked_targets": sorted(set(unlocked_targets)),
            "terminal_runtime_id": (
                self.runtime_by_stable.get(terminal["id"], {}).get("runtime_id")
            ),
            "terminal_kind": terminal.get("kind"),
            "terminal_hazard": terminal_hazard.get("kind"),
            "terminal_goal_expression_id": terminal_goal_expression_id,
            "terminal_goal_expression": _expression_tree(
                terminal_goal_expression_id, expressions
            ),
        }

    @staticmethod
    def _normalization(tasks: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for task in tasks:
            pou = task["pou"]
            dimensions = {
                "ec": float(len(task.get("control_obligations", []))),
                "ed": float(len(task.get("data_dependencies", []))),
                "es": float(len(task.get("state_transfer_chains", []))),
                "et": float(len(task.get("temporal_requirements", []))),
                "eh": float(
                    len(task.get("hazard_prerequisites", []))
                    + int(task.get("task_kind") == "violation")
                ),
            }
            current = result.setdefault(
                pou, {"ec": 1.0, "ed": 1.0, "es": 1.0, "et": 1.0, "eh": 1.0}
            )
            for name, value in dimensions.items():
                current[name] = max(current[name], value, 1.0)
        return result


def generate_semantic_task_plan(
    model_path: Path, runtime_ids_path: Path, output_path: Path
) -> dict[str, Any]:
    return SemanticTaskPlanner.from_files(model_path, runtime_ids_path).write(output_path)
