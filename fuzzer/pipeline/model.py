"""STG/ABI target model used by the engine campaign and result processing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fuzzer.corpus.frames import build_testcase, default_for_spec
from fuzzer.pipeline import analysis
from fuzzer.pipeline.probe import load_runtime_index


def _require_fields(document: Any, label: str, fields: tuple[str, ...]) -> None:
    """Reject incomplete exchange documents instead of treating them as empty models."""
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    missing = [field for field in fields if field not in document]
    if missing:
        raise ValueError(f"{label} missing required field(s): {', '.join(missing)}")

def type_bounds(spec: dict[str, Any]) -> tuple[int, int] | None:
    if spec.get("kind") != "scalar":
        return None
    name = str(spec.get("name", ""))
    if name == "BOOL":
        return 0, 1
    if name in ("REAL", "LREAL"):
        return None
    bits = int(spec.get("size") or 0) * 8
    if bits <= 0:
        return None
    c_type = str(spec.get("c_type") or "")
    unsigned = c_type.startswith("u") or name in {
        "USINT", "BYTE", "CHAR", "UINT", "WORD", "WCHAR",
        "UDINT", "DWORD", "ULINT", "LWORD",
    }
    if unsigned:
        return 0, 2**bits - 1
    return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1


@dataclass
class InputField:
    name: str
    spec: dict[str, Any]

    @property
    def upper(self) -> str:
        return self.name.upper()

    def default(self) -> str:
        return default_for_spec(self.spec)

    def values(self, literals: set[int | float]) -> list[str]:
        spec = self.spec
        if spec.get("kind") == "scalar":
            name = str(spec.get("name"))
            if name in ("REAL", "LREAL"):
                values = set(FLOAT_BASE)
                values.update(float(v) for v in literals if isinstance(v, (int, float)))
                return [format_float(value) for value in sorted(values)]
            if name == "BOOL":
                return list(BOOL_VALUES)
            candidates = set(INT_BASE)
            for value in literals:
                if isinstance(value, int):
                    for delta in (-8, -2, -1, 0, 1, 2, 8):
                        candidates.add(value + delta)
            bounds = type_bounds(spec)
            if bounds is not None:
                candidates.update((bounds[0], bounds[1], bounds[0] + 1, bounds[1] - 1))
            return [str(value) for value in clamp(candidates, bounds)]
        if spec.get("kind") == "pointer":
            return ["0x00", "0x" + "00" * 64, "0x" + "ff" * 64]
        if spec.get("kind") == "string":
            capacity = int(spec.get("length") or 8)
            return ["A", "0", "", "Z" * min(8, max(1, capacity))]
        return ["0x00", "0x" + "00" * 64]


class _SpecView:
    def __init__(self, data: dict[str, Any]):
        self.kind = str(data.get("kind", "opaque"))
        self.name = str(data.get("name", ""))
        self.c_type = data.get("c_type")
        self.length = data.get("length")
        self.size = int(data.get("size") or 0)
        self.count = data.get("count")
        self._byte_size = int(data.get("byte_size") or 0)
        self.element = _SpecView(data["element"]) if isinstance(data.get("element"), dict) else None

    def byte_size(self) -> int:
        if self._byte_size:
            return self._byte_size
        if self.kind == "array" and self.element is not None:
            return int(self.count or 0) * self.element.byte_size()
        if self.kind == "string":
            return (int(self.length or 0) + 1) * self.size
        if self.kind == "pointer":
            return 8
        return self.size


@dataclass
class _ParamLike:
    name: str
    spec_data: dict[str, Any]
    block_kind: str = "VAR_INPUT"
    constant: bool = False
    index: int | None = None

    @property
    def spec(self) -> _SpecView:
        return _SpecView(self.spec_data)

    @property
    def kind(self) -> str:
        return "FUNCTION"

    def byte_size(self) -> int:
        return 0


def format_float(value: float) -> str:
    if value == int(value):
        return f"{value:.1f}"
    return f"{value:.17g}"


@dataclass
class TargetModel:
    run_dir: Path
    bundle: Path
    function: str
    abi: dict[str, Any]
    layout: dict[str, Any]
    tasks: dict[str, Any]
    runtime_index: dict[int, dict[str, Any]]
    stg_model: dict[str, Any]
    stg_types: dict[str, dict[str, Any]]
    literals: set[int | float]
    fields: list[InputField]

    @classmethod
    def load(cls, run_dir: Path, bundle: Path) -> "TargetModel":
        abi = json.loads((bundle / "abi.json").read_text(encoding="utf-8"))
        layout = json.loads((bundle / "state-layout.json").read_text(encoding="utf-8"))
        tasks = json.loads((run_dir / "semantic-task-plan.json").read_text(encoding="utf-8"))
        runtime_index = load_runtime_index(run_dir / "stg" / "stg-runtime-ids.json")
        stg_model = json.loads((run_dir / "stg" / "stg-model.json").read_text(encoding="utf-8"))
        _require_fields(abi, "bundle/abi.json",
                        ("schema_version", "target", "frame", "frame_fields", "shared_fields", "state_fields"))
        _require_fields(abi["target"], "bundle/abi.json.target", ("name", "kind"))
        _require_fields(abi["frame"], "bundle/abi.json.frame", ("size", "align", "fields"))
        _require_fields(layout, "bundle/state-layout.json",
                        ("schema_version", "snapshot_schema_version", "total_size", "harness_region_index", "regions"))
        _require_fields(tasks, "semantic-task-plan.json", ("schema_version", "tasks"))
        _require_fields(stg_model, "stg/stg-model.json", ("schema_version",))
        frame_entries = abi.get("frame_fields", [])
        shared_entries = abi.get("shared_fields", [])
        fields = [InputField(entry["name"], entry["spec"]) for entry in frame_entries]
        fields += [
            InputField(entry["name"], entry["spec"])
            for entry in shared_entries
            if entry["name"] not in {item["name"] for item in frame_entries}
        ]
        return cls(
            run_dir=run_dir,
            bundle=bundle,
            function=abi["target"]["name"],
            abi=abi,
            layout=layout,
            tasks=tasks,
            runtime_index=runtime_index,
            stg_model=stg_model,
            stg_types=analysis.load_stg_types(run_dir / "stg" / "stg-model.json"),
            literals=analysis.collect_literals(stg_model),
            fields=fields,
        )

    @property
    def kind(self) -> str:
        return self.abi["target"]["kind"]

    @property
    def shared_names(self) -> set[str]:
        return {entry["name"] for entry in self.abi.get("shared_fields", [])}

    @property
    def frame_names(self) -> set[str]:
        explicit = {entry["name"] for entry in self.abi.get("frame_fields", [])}
        # VAR_IN_OUT is both shared backing storage and a per-frame action
        # field. Keep it in the frame schema even when ordinary inputs exist;
        # this matches the Rust engine's action_fields() contract.
        explicit.update(
            entry["name"]
            for entry in self.abi.get("shared_fields", [])
            if entry.get("block_kind") == "VAR_IN_OUT"
        )
        return explicit

    def default_frame(self) -> dict[str, str]:
        return {field.name: field.default() for field in self.fields}

    def build(
        self,
        frames: list[dict[str, str]],
        *,
        shared: dict[str, str] | None = None,
        frame_dt_ns: list[int | None] | None = None,
    ) -> bytes:
        # Shared configuration records (constant inputs, IN_OUT buffers) are
        # taken from the first frame; the remaining fields form real frames.
        shared_names = self.shared_names
        shared_values: dict[str, str] = dict(shared or {})
        frame_only: list[dict[str, str]] = []
        for index, frame in enumerate(frames):
            split = {key: value for key, value in frame.items() if key in shared_names}
            if index == 0:
                # A recorded prefix carries the authoritative shared backing
                # state.  Only infer an initial shared value from the first
                # frame when the caller did not provide that field.
                for key, value in split.items():
                    shared_values.setdefault(key, value)
            remainder = {
                key: value
                for key, value in frame.items()
                if key not in shared_names or key in self.frame_names
            }
            if remainder or self.frame_names:
                frame_only.append(remainder)
        if not frame_only and not self.frame_names:
            frame_only = []
        include_dt = bool(frame_dt_ns and any(value is not None for value in frame_dt_ns))
        return build_testcase(
            _AbiLike(self),
            frame_only,
            shared=shared_values,
            frame_dt_ns=frame_dt_ns,
            include_dt=include_dt,
        )

    def goals(self) -> list[dict[str, Any]]:
        objective = {"array_access", "pointer_access", "division", "modulo"}
        return [
            task
            for task in self.tasks.get("tasks", [])
            if task.get("terminal_kind") == "hazard_violation"
            and str(task.get("terminal_hazard")) in objective
        ]

    def state_lookup(self, name: str) -> str:
        for entry in self.abi.get("state_fields", []):
            if entry["name"].upper() == name.upper():
                return entry["name"]
        return name


@dataclass
class _AbiLike:
    model: TargetModel

    @property
    def params(self) -> list[_ParamLike]:
        names = self.model.frame_names
        return [
            _ParamLike(field.name, field.spec, "VAR_INPUT", False, index)
            for index, field in enumerate(self.model.fields)
            if field.name in names
        ]

    @property
    def state_fields(self) -> list[_ParamLike]:
        shared = {
            entry["name"]
            for entry in self.model.abi.get("shared_fields", [])
        }
        return [
            _ParamLike(field.name, field.spec, "VAR_IN_OUT" if field.name in shared else "VAR_INPUT", False, index)
            for index, field in enumerate(self.model.fields)
            if field.name in shared
        ]


