"""Deterministic structured seed minimization and field attribution."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
from hashlib import sha256
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from fuzzer.corpus.store import normalize_seed
from fuzzer.runtime.paths import ROOT


FINDING_STATUSES = {"crash", "timeout", "nonzero", "oom"}
INTEGER_BOUNDS = {
    "SINT": (-(2**7), 2**7 - 1),
    "USINT": (0, 2**8 - 1),
    "BYTE": (0, 2**8 - 1),
    "INT": (-(2**15), 2**15 - 1),
    "UINT": (0, 2**16 - 1),
    "WORD": (0, 2**16 - 1),
    "DINT": (-(2**31), 2**31 - 1),
    "UDINT": (0, 2**32 - 1),
    "DWORD": (0, 2**32 - 1),
    "LINT": (-(2**63), 2**63 - 1),
    "ULINT": (0, 2**64 - 1),
    "LWORD": (0, 2**64 - 1),
    "CHAR": (0, 255),
}
FLOAT_TYPES = {"REAL", "LREAL"}
STRING_TYPES = {"STRING", "WSTRING"}
BYTE_TYPES = {"POINTER", "ARRAY"}
OOM_MARKERS = (
    "out of memory",
    "allocation failed",
    "allocator is out of memory",
    "cannot allocate memory",
    "memory allocation failure",
)
SANITIZER_ERROR_RE = re.compile(
    r"ERROR:\s+([A-Za-z]+Sanitizer):\s+([A-Za-z0-9_.:/+-]+)"
)
UBSAN_ERROR_RE = re.compile(r"runtime error:\s+(.+)")
STACK_FRAME_RE = re.compile(r"^\s*#\d+\s+(?:0x[0-9a-fA-F]+\s+)?(?:in\s+)?(.+)$")


@dataclass
class FailureSignature:
    status: str
    returncode: int | None
    signal: str | None = None
    sanitizer: str | None = None
    sanitizer_kind: str | None = None
    stack_hash: str | None = None
    stack_top: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SeedRecord:
    name: str
    ty: str
    value: str
    original_index: int

    def line(self) -> str:
        return f"{self.name},{self.ty},{self.value}"

    @property
    def field(self) -> str:
        return self.name


@dataclass
class ReplayOutcome:
    status: str
    returncode: int | None
    stderr_preview: str = ""
    signature: FailureSignature | None = None


@dataclass
class MinimizationResult:
    original_seed: str
    minimized_seed: str
    original_status: str
    minimized_status: str
    removed_records: list[dict[str, object]] = field(default_factory=list)
    changed_values: list[dict[str, object]] = field(default_factory=list)
    critical_fields: list[str] = field(default_factory=list)
    maybe_fields: list[str] = field(default_factory=list)
    irrelevant_fields: list[str] = field(default_factory=list)
    replay_stderr_preview: str = ""
    baseline_status: str | None = None
    replay_count: int = 0
    original_signature: dict[str, object] | None = None
    minimized_signature: dict[str, object] | None = None
    signature_match: bool = False

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def parse_seed(seed: bytes) -> list[SeedRecord]:
    records: list[SeedRecord] = []
    for index, raw_line in enumerate(normalize_seed(seed).decode("utf-8").splitlines()):
        name, ty, value = raw_line.split(",", 2)
        records.append(SeedRecord(name=name, ty=ty, value=value, original_index=index))
    return records


def serialize_seed(records: Iterable[SeedRecord]) -> bytes:
    return ("\n".join(record.line() for record in records) + "\n").encode("utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def record_json(record: SeedRecord) -> dict[str, object]:
    return {
        "index": record.original_index,
        "name": record.name,
        "type": record.ty,
        "value": record.value,
    }


def field_sort_key(name: str) -> tuple[int, str, str]:
    prefix, dot, rest = name.partition(".")
    if dot and prefix.isdigit():
        return (int(prefix), rest.upper(), name)
    return (-1, name.upper(), name)


def classify_process_result(returncode: int, stderr: str) -> str:
    lowered = stderr.lower()
    if any(marker in lowered for marker in OOM_MARKERS):
        return "oom"
    if returncode == 0:
        return "ok"
    if returncode < 0:
        return "crash"
    return "nonzero"


def signal_name(returncode: int | None) -> str | None:
    if returncode is None or returncode >= 0:
        return None
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"signal_{-returncode}"


def normalize_stack_frame(frame: str) -> str:
    frame = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", frame)
    frame = re.sub(r":\d+(?::\d+)?", ":LINE", frame)
    frame = re.sub(r"\s+", " ", frame).strip()
    return frame[:240]


def extract_stack_frames(stderr: str, limit: int = 6) -> list[str]:
    frames: list[str] = []
    for line in stderr.splitlines():
        match = STACK_FRAME_RE.match(line)
        if not match:
            continue
        frame = normalize_stack_frame(match.group(1))
        if frame:
            frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


def extract_failure_signature(status: str, returncode: int | None, stderr: str) -> FailureSignature:
    sanitizer = None
    sanitizer_kind = None
    match = SANITIZER_ERROR_RE.search(stderr)
    if match:
        sanitizer = match.group(1)
        sanitizer_kind = match.group(2)
    else:
        ubsan = UBSAN_ERROR_RE.search(stderr)
        if ubsan:
            sanitizer = "UndefinedBehaviorSanitizer"
            sanitizer_kind = normalize_stack_frame(ubsan.group(1))

    stack_top = extract_stack_frames(stderr)
    stack_hash = (
        sha256("\n".join(stack_top).encode("utf-8")).hexdigest()[:16]
        if stack_top
        else None
    )
    return FailureSignature(
        status=status,
        returncode=returncode,
        signal=signal_name(returncode),
        sanitizer=sanitizer,
        sanitizer_kind=sanitizer_kind,
        stack_hash=stack_hash,
        stack_top=stack_top,
    )


def signatures_match(expected: ReplayOutcome, candidate: ReplayOutcome) -> bool:
    if candidate.status != expected.status:
        return False
    expected_sig = expected.signature
    candidate_sig = candidate.signature
    if expected_sig is None or candidate_sig is None:
        return True
    if expected_sig.sanitizer or expected_sig.sanitizer_kind:
        sanitizer_matches = (
            candidate_sig.sanitizer == expected_sig.sanitizer
            and candidate_sig.sanitizer_kind == expected_sig.sanitizer_kind
        )
        if not sanitizer_matches:
            return False
        if expected_sig.stack_hash:
            return candidate_sig.stack_hash == expected_sig.stack_hash
        return True
    if expected_sig.stack_hash:
        return candidate_sig.stack_hash == expected_sig.stack_hash
    if expected.status == "crash" and expected.returncode is not None:
        return candidate.returncode == expected.returncode
    return True


class SeedReducer:
    def __init__(
        self,
        target: Path,
        *,
        timeout: float,
        baseline_status: str | None = None,
        cwd: Path = ROOT,
    ) -> None:
        if baseline_status is not None and baseline_status not in FINDING_STATUSES:
            raise ValueError(f"unsupported baseline status: {baseline_status}")
        self.target = target
        self.timeout = timeout
        self.baseline_status = baseline_status
        self.cwd = cwd
        self._cache: dict[bytes, ReplayOutcome] = {}
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.replay_count = 0

    def __enter__(self) -> "SeedReducer":
        self._tempdir = tempfile.TemporaryDirectory(prefix="semantist-minimize-")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def replay_seed(self, seed: bytes) -> ReplayOutcome:
        seed = normalize_seed(seed)
        cached = self._cache.get(seed)
        if cached is not None:
            return cached
        if self._tempdir is None:
            self._tempdir = tempfile.TemporaryDirectory(prefix="semantist-minimize-")
        path = Path(self._tempdir.name) / f"candidate-{len(self._cache):06d}.seed"
        path.write_bytes(seed)
        env = os.environ.copy()
        env["ASAN_OPTIONS"] = "abort_on_error=1:symbolize=1:detect_leaks=0:print_stacktrace=1"
        env["UBSAN_OPTIONS"] = "abort_on_error=1:symbolize=1:print_stacktrace=1"
        try:
            completed = subprocess.run(
                [str(self.target), str(path)],
                cwd=self.cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                timeout=self.timeout,
                check=False,
            )
            stderr = completed.stderr.decode("utf-8", errors="replace")
            status = classify_process_result(completed.returncode, stderr)
            outcome = ReplayOutcome(
                status=status,
                returncode=completed.returncode,
                stderr_preview=stderr[:1200],
                signature=extract_failure_signature(status, completed.returncode, stderr),
            )
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr.decode("utf-8", errors="replace") if error.stderr else ""
            outcome = ReplayOutcome(
                status="timeout",
                returncode=None,
                stderr_preview=stderr[:1200],
                signature=extract_failure_signature("timeout", None, stderr),
            )
        except OSError as error:
            stderr = str(error)
            outcome = ReplayOutcome(
                status="replay_error",
                returncode=None,
                stderr_preview=stderr[:1200],
                signature=extract_failure_signature("replay_error", None, stderr),
            )
        self._cache[seed] = outcome
        self.replay_count += 1
        return outcome

    def reproduces(self, records: list[SeedRecord], expected: ReplayOutcome) -> tuple[bool, ReplayOutcome]:
        outcome = self.replay_seed(serialize_seed(records))
        return signatures_match(expected, outcome), outcome

    def minimize(
        self,
        seed_path: Path,
        output_path: Path,
        *,
        metadata_path: Path | None = None,
    ) -> MinimizationResult:
        original_records = parse_seed(seed_path.read_bytes())
        original_outcome = self.replay_seed(serialize_seed(original_records))
        expected_status = self.baseline_status or original_outcome.status
        if expected_status not in FINDING_STATUSES:
            raise RuntimeError(f"baseline did not produce a finding status: {original_outcome.status}")
        if original_outcome.status != expected_status:
            raise RuntimeError(
                f"baseline status mismatch: expected {expected_status}, got {original_outcome.status}"
            )
        expected_outcome = original_outcome

        records = list(original_records)
        records = self._delete_cycles(records, expected_outcome)
        records = self._delete_records(records, expected_outcome, exact_duplicates_only=True)
        records = self._delete_records(records, expected_outcome, exact_duplicates_only=False)
        records = self._shrink_values(records, expected_outcome)

        minimized_seed = serialize_seed(records)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(minimized_seed)
        minimized_outcome = self.replay_seed(minimized_seed)
        attribution = self._attribute_fields(records, expected_outcome)
        result = MinimizationResult(
            original_seed=display_path(seed_path),
            minimized_seed=display_path(output_path),
            original_status=original_outcome.status,
            minimized_status=minimized_outcome.status,
            removed_records=self._removed_records(original_records, records),
            changed_values=self._changed_values(original_records, records),
            critical_fields=attribution["critical_fields"],
            maybe_fields=attribution["maybe_fields"],
            irrelevant_fields=attribution["irrelevant_fields"],
            replay_stderr_preview=minimized_outcome.stderr_preview,
            baseline_status=self.baseline_status,
            replay_count=self.replay_count,
            original_signature=(
                original_outcome.signature.to_json() if original_outcome.signature else None
            ),
            minimized_signature=(
                minimized_outcome.signature.to_json() if minimized_outcome.signature else None
            ),
            signature_match=signatures_match(expected_outcome, minimized_outcome),
        )
        if metadata_path is not None:
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(
                json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return result

    def _delete_cycles(self, records: list[SeedRecord], expected: ReplayOutcome) -> list[SeedRecord]:
        current = list(records)
        while True:
            cycles = sorted(
                {
                    int(prefix)
                    for prefix, dot, rest in (record.name.partition(".") for record in current)
                    if dot and prefix.isdigit() and rest
                },
                reverse=True,
            )
            changed = False
            for cycle in cycles:
                candidate = [
                    record
                    for record in current
                    if not (record.name.startswith(f"{cycle}.") and record.name.split(".", 1)[0].isdigit())
                ]
                if not candidate:
                    continue
                ok, _ = self.reproduces(candidate, expected)
                if ok:
                    current = candidate
                    changed = True
                    break
            if not changed:
                return current

    def _delete_records(
        self,
        records: list[SeedRecord],
        expected: ReplayOutcome,
        *,
        exact_duplicates_only: bool,
    ) -> list[SeedRecord]:
        current = list(records)
        while True:
            changed = False
            duplicate_indices = self._exact_duplicate_indices(current)
            for index in range(len(current) - 1, -1, -1):
                if exact_duplicates_only and index not in duplicate_indices:
                    continue
                candidate = current[:index] + current[index + 1 :]
                if not candidate:
                    continue
                ok, _ = self.reproduces(candidate, expected)
                if ok:
                    current = candidate
                    changed = True
                    break
            if not changed:
                return current

    def _shrink_values(self, records: list[SeedRecord], expected: ReplayOutcome) -> list[SeedRecord]:
        current = list(records)
        index = 0
        while index < len(current):
            record_changed = False
            for value in value_candidates(current[index]):
                candidate_record = SeedRecord(
                    name=current[index].name,
                    ty=current[index].ty,
                    value=value,
                    original_index=current[index].original_index,
                )
                candidate = current[:index] + [candidate_record] + current[index + 1 :]
                ok, _ = self.reproduces(candidate, expected)
                if ok:
                    current = candidate
                    record_changed = True
                    break
            if not record_changed:
                index += 1
        return current

    def _attribute_fields(
        self,
        records: list[SeedRecord],
        expected: ReplayOutcome,
    ) -> dict[str, list[str]]:
        critical: list[str] = []
        maybe: list[str] = []
        irrelevant: list[str] = []
        for index, record in enumerate(records):
            default = default_value(record)
            field = record.field
            if default == record.value:
                maybe.append(field)
                continue
            candidate_record = SeedRecord(record.name, record.ty, default, record.original_index)
            candidate = records[:index] + [candidate_record] + records[index + 1 :]
            outcome = self.replay_seed(serialize_seed(candidate))
            if signatures_match(expected, outcome):
                irrelevant.append(field)
            elif outcome.status == "ok":
                critical.append(field)
            else:
                maybe.append(field)
        return {
            "critical_fields": sorted(critical, key=field_sort_key),
            "maybe_fields": sorted(maybe, key=field_sort_key),
            "irrelevant_fields": sorted(irrelevant, key=field_sort_key),
        }

    @staticmethod
    def _exact_duplicate_indices(records: list[SeedRecord]) -> set[int]:
        seen: set[tuple[str, str, str]] = set()
        duplicates: set[int] = set()
        for index, record in enumerate(records):
            key = (record.name.upper(), record.ty, record.value)
            if key in seen:
                duplicates.add(index)
            else:
                seen.add(key)
        return duplicates

    @staticmethod
    def _removed_records(
        original_records: list[SeedRecord],
        minimized_records: list[SeedRecord],
    ) -> list[dict[str, object]]:
        kept = {record.original_index for record in minimized_records}
        return [record_json(record) for record in original_records if record.original_index not in kept]

    @staticmethod
    def _changed_values(
        original_records: list[SeedRecord],
        minimized_records: list[SeedRecord],
    ) -> list[dict[str, object]]:
        originals = {record.original_index: record for record in original_records}
        changes = []
        for record in minimized_records:
            original = originals[record.original_index]
            if original.value != record.value:
                changes.append(
                    {
                        "index": record.original_index,
                        "name": record.name,
                        "type": record.ty,
                        "from": original.value,
                        "to": record.value,
                    }
                )
        return changes


def ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def value_candidates(record: SeedRecord) -> list[str]:
    ty = record.ty.upper()
    if ty == "BOOL":
        return [value for value in ("0", "1") if value != record.value]
    if ty in INTEGER_BOUNDS:
        return integer_candidates(record.value, INTEGER_BOUNDS[ty])
    if ty in FLOAT_TYPES:
        return float_candidates(record.value)
    if ty in STRING_TYPES:
        return string_candidates(record.value)
    if ty in BYTE_TYPES and record.value.startswith(("0x", "0X")):
        return hex_byte_candidates(record.value)
    if ty in BYTE_TYPES:
        return string_candidates(record.value)
    return []


def integer_candidates(value: str, bounds: tuple[int, int]) -> list[str]:
    try:
        original = int(value, 0)
    except ValueError:
        return []
    lo, hi = bounds
    raw: list[int] = [0, 1, -1]
    current = original
    while current not in (0, 1, -1):
        current = int(current / 2)
        raw.append(current)
    magnitude = 1
    while magnitude < abs(original):
        raw.extend((magnitude, -magnitude))
        magnitude *= 2
    raw.extend(
        boundary
        for boundary in (lo, hi, lo + 1, hi - 1)
        if abs(boundary) <= abs(original)
    )
    return [
        str(candidate)
        for candidate in ordered_unique(str(item) for item in raw if lo <= item <= hi and item != original)
    ]


def float_candidates(value: str) -> list[str]:
    try:
        original = float(value)
    except ValueError:
        return []
    raw: list[float] = [0.0, 1.0, -1.0]
    current = original
    while abs(current) > 1.0:
        current = current / 2.0
        raw.append(current)
    return ordered_unique(format_float(item) for item in raw if item != original)


def format_float(value: float) -> str:
    text = f"{value:.12g}"
    if "e" not in text.lower() and "." not in text:
        text += ".0"
    return text


def string_candidates(value: str) -> list[str]:
    raw = ["", value[:1]]
    length = len(value)
    step = length // 2
    while step > 0:
        raw.append(value[:step])
        step //= 2
    for length_candidate in (2, 4, 8, 16, 32, 64, 128):
        if length_candidate < length:
            raw.append(value[:length_candidate])
    return [candidate for candidate in ordered_unique(raw) if candidate != value]


def hex_byte_candidates(value: str) -> list[str]:
    raw_hex = value[2:]
    if len(raw_hex) % 2 != 0:
        return []
    try:
        data = bytes.fromhex(raw_hex)
    except ValueError:
        return []
    raw: list[bytes] = [b"", b"\x00", b"\x00" * len(data)]
    step = len(data) // 2
    while step > 0:
        raw.append(data[:step])
        step //= 2
    chunk = max(1, len(data) // 2)
    while chunk > 0:
        for start in range(0, len(data), chunk):
            candidate = data[:start] + data[start + chunk :]
            raw.append(candidate)
            cleared = bytearray(data)
            cleared[start : min(len(data), start + chunk)] = b"\x00" * min(chunk, len(data) - start)
            raw.append(bytes(cleared))
        chunk //= 2
    return ordered_unique("0x" + candidate.hex() for candidate in raw if candidate != data)


def default_value(record: SeedRecord) -> str:
    ty = record.ty.upper()
    if ty in FLOAT_TYPES:
        return "0.0"
    if ty in STRING_TYPES:
        return ""
    if ty in BYTE_TYPES:
        return "0x00"
    return "0"


def minimize_seed(
    *,
    target: Path,
    seed: Path,
    output: Path,
    timeout: float,
    baseline_status: str | None = None,
    metadata: Path | None = None,
) -> MinimizationResult:
    metadata_path = metadata if metadata is not None else output.with_suffix(".json")
    with SeedReducer(target, timeout=timeout, baseline_status=baseline_status) as reducer:
        return reducer.minimize(seed, output, metadata_path=metadata_path)
