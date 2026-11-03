"""Canonical SemantiST testcase frames (spec section 3).

A testcase is one optional ``__INIT`` record, shared records, and consecutive
frames.  Frame records are written as ``<frame>.<field>,<type>,<value>`` and
every frame carries every frame field in canonical form, so deleting or
changing one frame never depends on an earlier frame's omitted fields.

The parser still accepts handwritten testcases that omit fields: omitted fields
inherit the previous frame's value (frame 0 starts from the type default) and
the result is expanded to the canonical form.  The same FNV-1a 64 frame hash is
implemented by the Rust engine (``fuzzer/engine/src/frames.rs``); the two are
cross-checked by a unit/regression test.
"""

from __future__ import annotations

import re
import math
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from compiler.harness.abi import Param, TargetAbi, frame_action_params, shared_params

FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
FRAME_SEPARATOR = b"\x1e"
RESERVED_INIT = "__INIT"
RESERVED_DT = "__DT"


class FrameFormatError(ValueError):
    pass


def default_for_spec(spec: Mapping[str, Any]) -> str:
    kind = spec.get("kind")
    if kind == "scalar":
        if spec.get("name") in ("REAL", "LREAL"):
            return "0.0"
        return "0"
    if kind == "string":
        return "A"
    return "0x00"


def default_value(param: Param) -> str:
    return default_for_spec({"kind": param.spec.kind, "name": param.spec.name})


def type_token(param: Param) -> str:
    spec = param.spec
    if spec.kind == "scalar" or spec.kind == "string":
        return spec.name
    if spec.kind == "pointer":
        return "POINTER"
    return "ARRAY"


def fnv1a64(data: bytes, seed: int = FNV_OFFSET) -> int:
    value = seed
    for byte in data:
        value ^= byte
        value = (value * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return value


def _split_record(line: str) -> tuple[str, str, str]:
    parts = line.split(",", 2)
    if len(parts) != 3:
        raise FrameFormatError(f"invalid record: {line!r}")
    name, ty, value = (part.strip() for part in parts)
    if not name:
        raise FrameFormatError(f"invalid record: {line!r}")
    return name, ty.upper(), value


def _check_type_token(token: str, expected: str, name: str) -> None:
    """Validate an explicit type token while accepting Rust's empty token."""
    if token and token != expected.upper():
        raise FrameFormatError(f"field {name!r} has type {token!r}, expected {expected!r}")


def split_cycle_name(name: str) -> tuple[int | None, str]:
    match = re.match(r"^(\d+)\.(.+)$", name)
    if not match:
        return None, name
    return int(match.group(1)), match.group(2)


@dataclass
class Frame:
    index: int
    values: dict[str, str] = field(default_factory=dict)
    dt_ns: int | None = None

    def copy(self) -> "Frame":
        return Frame(self.index, dict(self.values), self.dt_ns)


@dataclass
class TestCase:
    init_profile: str = "cold"
    shared: dict[str, str] = field(default_factory=dict)
    frames: list[Frame] = field(default_factory=list)
    contract_id: str | None = None
    field_order: list[str] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    shared_order: list[str] = field(default_factory=list)

    def canonical(self, *, include_dt: bool = False) -> str:
        lines: list[str] = []
        if self.init_profile != "cold":
            lines.append(f"{RESERVED_INIT},ENUM,{self.init_profile}")
        for name in self.shared_order or list(self.shared):
            if name not in self.shared:
                continue
            lines.append(f"{name},{self.types.get(name, 'INT')},{self.shared[name]}")
        for frame in self.frames:
            for name in self.field_order or list(frame.values):
                if name not in frame.values:
                    continue
                lines.append(f"{frame.index}.{name},{self.types.get(name, 'INT')},{frame.values[name]}")
            if include_dt and frame.dt_ns is not None:
                lines.append(f"{frame.index}.{RESERVED_DT},TIME,{frame.dt_ns}")
        return "\n".join(lines) + ("\n" if lines else "")

    def to_bytes(self, *, include_dt: bool = False) -> bytes:
        return self.canonical(include_dt=include_dt).encode("utf-8")

    def frame_hashes(self) -> list[int]:
        hashes = []
        for frame in self.frames:
            text = "".join(f"{name}={frame.values[name]};" for name in sorted(frame.values))
            if frame.dt_ns is not None:
                text += f"{RESERVED_DT}={frame.dt_ns};"
            hashes.append(fnv1a64(text.encode("utf-8")))
        return hashes

    def prefix_hashes(self) -> list[int]:
        """Rolling hash of the frame sequence; element i covers frames 0..i."""
        rolling = FNV_OFFSET
        result = []
        for index, frame_hash in enumerate(self.frame_hashes()):
            rolling = fnv1a64(frame_hash.to_bytes(8, "little") + FRAME_SEPARATOR, rolling)
            result.append(rolling)
        return result


def _looks_like_bytes(value: str) -> bool:
    return value.startswith(("0x", "0X", "0X"))


def parse_testcase(
    data: str | bytes,
    abi: TargetAbi,
    *,
    scan_period_ns: int = 10_000_000,
    dt_range_ns: tuple[int, int] | None = None,
) -> TestCase:
    """Parse and canonicalize a testcase against ``abi``.

    ``abi.params`` is the frame-input list; ``abi.state_fields`` supplies the
    type information for constant inputs and VAR_IN_OUT backing storage. A field
    that the ABI does not declare is an error: guessed fields never enter the
    corpus.
    """
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    frame_params_map = {param.name.upper(): param for param in frame_action_params(abi)}
    all_params = {param.name.upper(): param for param in list(abi.state_fields) + list(abi.params)}
    shared_params_map = {param.name.upper(): param for param in shared_params(abi)}

    init_profile = "cold"
    shared: dict[str, str] = {}
    raw_frames: dict[int, list[tuple[str, str]]] = {}
    dt_by_frame: dict[int, int] = {}
    order: list[int] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name, ty, value = _split_record(line)
        cycle, field_name = split_cycle_name(name)
        if cycle is None:
            if field_name.upper() == RESERVED_INIT:
                _check_type_token(ty, "ENUM", field_name)
                if value not in ("cold",):
                    raise FrameFormatError(f"unsupported init profile {value!r}")
                init_profile = value
                continue
            key = field_name.upper()
            if key not in shared_params_map:
                if key in frame_params_map:
                    raise FrameFormatError(f"frame field {field_name!r} needs a frame prefix")
                raise FrameFormatError(f"unknown shared field {field_name!r}")
            param = shared_params_map[key]
            _check_type_token(ty, type_token(param), field_name)
            shared[param.name] = value
            continue
        if field_name.upper() == RESERVED_DT:
            _check_type_token(ty, "TIME", field_name)
            dt_by_frame[cycle] = _parse_dt(value)
            continue
        param = frame_params_map.get(field_name.upper())
        if param is None:
            # Constant shared inputs cannot be changed per frame.
            if field_name.upper() in all_params:
                raise FrameFormatError(f"field {field_name!r} is not a frame field")
            raise FrameFormatError(f"unknown frame field {field_name!r}")
        _check_type_token(ty, type_token(param), field_name)
        if cycle not in raw_frames:
            raw_frames[cycle] = []
            order.append(cycle)
        raw_frames[cycle].append((param.name, value))

    indices = sorted(set(raw_frames) | set(dt_by_frame))
    if indices and indices != list(range(0, len(indices))):
        raise FrameFormatError(f"frame indices must be consecutive from 0, got {indices}")
    frames: list[Frame] = []
    schema = frame_action_params(abi)
    previous: dict[str, str] = {param.name: default_value(param) for param in schema}
    previous.update({param.name: shared[param.name] for param in schema if param.name in shared})
    for index in range(len(indices)):
        values = dict(previous)
        for name, value in raw_frames.get(index, []):
            values[name] = value
        for param in schema:
            values.setdefault(param.name, default_value(param))
        dt_ns = dt_by_frame.get(index)
        if dt_ns is None and dt_range_ns is not None:
            dt_ns = scan_period_ns
        if dt_ns is not None:
            if dt_range_ns is not None and not (dt_range_ns[0] <= dt_ns <= dt_range_ns[1]):
                raise FrameFormatError(f"frame {index} delta_t {dt_ns} outside contract range")
        frames.append(Frame(index, values, dt_ns))
        previous = values
    if not frames:
        frames = [Frame(0, dict(previous))]
    testcase = TestCase(
        init_profile,
        shared,
        frames,
        field_order=[param.name for param in frame_action_params(abi)],
        types={param.name: type_token(param) for param in _all_typed_params(abi)},
        shared_order=[param.name for param in shared_params(abi)],
    )
    validate(testcase, abi)
    return testcase


def _all_typed_params(abi: TargetAbi) -> list[Param]:
    return list(abi.state_fields or []) + list(abi.params)


def _parse_dt(value: str) -> int:
    text = value.strip()
    if text.lower().startswith("t#"):
        return parse_time_literal(text)
    return int(text, 0)


def parse_time_literal(text: str) -> int:
    """Parse an ST TIME literal into nanoseconds (``T#500ms``)."""
    match = re.match(r"^t#\s*([0-9]+(?:\.[0-9]+)?)\s*(d|h|m|s|ms|us|ns)$", text.strip(), re.I)
    if not match:
        raise FrameFormatError(f"invalid TIME literal {text!r}")
    magnitude = float(match.group(1))
    unit = match.group(2).lower()
    factor = {
        "d": 86_400_000_000_000,
        "h": 3_600_000_000_000,
        "m": 60_000_000_000,
        "s": 1_000_000_000,
        "ms": 1_000_000,
        "us": 1_000,
        "ns": 1,
    }[unit]
    return int(round(magnitude * factor))


def canonicalize(
    data: str | bytes,
    abi: TargetAbi,
    *,
    scan_period_ns: int = 10_000_000,
    dt_range_ns: tuple[int, int] | None = None,
    include_dt: bool = False,
) -> bytes:
    return parse_testcase(data, abi, scan_period_ns=scan_period_ns, dt_range_ns=dt_range_ns).to_bytes(
        include_dt=include_dt
    )


def build_testcase(
    abi: TargetAbi,
    frames: Sequence[Mapping[str, str]],
    *,
    shared: Mapping[str, str] | None = None,
    init_profile: str = "cold",
    dt_ns: int | None = None,
    frame_dt_ns: Sequence[int | None] | None = None,
    include_dt: bool = False,
) -> bytes:
    """Build a canonical testcase from frame field mappings."""
    frame_objects: list[Frame] = []
    schema = frame_action_params(abi)
    previous = {param.name: default_value(param) for param in schema}
    previous.update({param.name: str((shared or {})[param.name]) for param in schema
                     if param.name in (shared or {})})
    for index, values in enumerate(frames):
        merged = dict(previous)
        for key, value in values.items():
            match = next((param.name for param in schema if param.name.upper() == key.upper()), None)
            if match is None:
                raise FrameFormatError(f"unknown frame field {key!r}")
            if value is None:
                merged.pop(match, None)
            else:
                merged[match] = str(value)
        frame_dt = frame_dt_ns[index] if frame_dt_ns is not None and index < len(frame_dt_ns) else \
            (dt_ns if index == 0 else None)
        frame_objects.append(Frame(index, merged, frame_dt))
        previous = merged
    testcase = TestCase(
        init_profile,
        dict(shared or {}),
        frame_objects,
        field_order=[param.name for param in schema],
        types={param.name: type_token(param) for param in _all_typed_params(abi)},
        shared_order=[param.name for param in shared_params(abi)],
    )
    for param in shared_params(abi):
        if param.name in testcase.shared:
            _validate_value(param, testcase.shared[param.name])
    for frame in testcase.frames:
        for param in schema:
            if param.name in frame.values:
                _validate_value(param, frame.values[param.name])
    return testcase.to_bytes(include_dt=include_dt)


def _validate_value(param: Param, value: str) -> None:
    spec = param.spec
    if spec.kind == "string":
        units = len(value.encode("utf-16-le", errors="surrogatepass")) // 2 \
            if spec.name == "WSTRING" else len(value)
        if spec.length is not None and units > spec.length:
            raise FrameFormatError(
                f"field {param.name!r} string length {units} exceeds {spec.length}"
            )
        return
    if spec.kind == "pointer":
        if value and not value.lower().startswith("0x"):
            raise FrameFormatError(f"field {param.name!r} has invalid pointer value {value!r}")
        return
    if spec.kind == "array":
        if not value.lower().startswith("0x"):
            raise FrameFormatError(f"field {param.name!r} has invalid array value {value!r}")
        digits = value[2:]
        if len(digits) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", digits):
            raise FrameFormatError(f"field {param.name!r} has invalid array bytes {value!r}")
        expected = spec.byte_size()
        if expected and len(digits) // 2 > expected:
            raise FrameFormatError(
                f"field {param.name!r} array bytes {len(digits) // 2} exceeds {expected}"
            )
        return
    if spec.kind != "scalar":
        return
    if spec.name in {"REAL", "LREAL"}:
        try:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("non-finite")
            if spec.name == "REAL":
                struct.pack("<f", number)
        except (ValueError, OverflowError) as error:
            raise FrameFormatError(f"field {param.name!r} has invalid {spec.name} value {value!r}") from error
        return
    text = value.strip()
    try:
        number = int(text, 16) if re.fullmatch(r"[+-]?0[xX][0-9a-fA-F]+", text) else int(text, 10)
    except ValueError as error:
        raise FrameFormatError(f"field {param.name!r} has invalid integer value {value!r}") from error
    bits = int(spec.size or 0) * 8
    if not 1 <= bits <= 64:
        raise FrameFormatError(f"field {param.name!r} has unsupported scalar width {bits}")
    unsigned = str(spec.c_type or "").startswith("uint") or spec.name in {
        "USINT", "BYTE", "CHAR", "UINT", "WORD", "WCHAR",
        "UDINT", "DWORD", "ULINT", "LWORD",
    }
    minimum, maximum = (0, (1 << bits) - 1) if unsigned else (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
    if spec.name == "BOOL":
        maximum = 1
    if not minimum <= number <= maximum:
        raise FrameFormatError(f"field {param.name!r} value {number} outside [{minimum}, {maximum}]")


def validate(testcase: TestCase, abi: TargetAbi) -> None:
    frame_names = {param.name for param in frame_action_params(abi)}
    shared = {param.name: param for param in shared_params(abi)}
    if not set(testcase.shared) <= set(shared):
        raise FrameFormatError(f"unknown shared fields: {sorted(set(testcase.shared) - set(shared))}")
    for name, value in testcase.shared.items():
        _validate_value(shared[name], value)
    action = {param.name: param for param in frame_action_params(abi)}
    for frame in testcase.frames:
        if set(frame.values) != frame_names:
            missing = frame_names - set(frame.values)
            extra = set(frame.values) - frame_names
            raise FrameFormatError(f"frame {frame.index}: missing {sorted(missing)}, extra {sorted(extra)}")
        if frame.dt_ns is not None and not 0 <= frame.dt_ns <= (1 << 64) - 1:
            raise FrameFormatError(f"frame {frame.index}: delta_t outside uint64 range")
        for name, value in frame.values.items():
            _validate_value(action[name], value)
