"""Frame deletion and value simplification for a confirmed candidate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from fuzzer.corpus.frames import Frame, TestCase, build_testcase, default_value, parse_testcase
from fuzzer.pipeline.probe import classify


@dataclass
class Minimized:
    testcase: bytes
    frames: int
    depth: int | None
    removed: int
    simplified: int


def _signature(candidate: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if candidate is None:
        return None
    result = (candidate.get("oracle"), candidate.get("runtime_id"),
              str(candidate.get("source")))
    return result + (candidate["property_id"],) if candidate.get("property_id") else result


def _encode(abi_like, original: TestCase, frames: list[Frame]) -> bytes:
    return build_testcase(
        abi_like, [frame.values for frame in frames], shared=original.shared,
        init_profile=original.init_profile, frame_dt_ns=[frame.dt_ns for frame in frames],
        include_dt=any(frame.dt_ns is not None for frame in frames),
    )


def _probe_timeout(deadline: float | None) -> float:
    if deadline is None:
        return 5.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("campaign deadline exhausted during minimization")
    return min(5.0, remaining)


def minimize(
    testcase: bytes,
    *,
    abi_like,
    runtime_index: dict[int, dict[str, Any]],
    target_binary: Path,
    work_dir: Path,
    probe_fn,
    signature: tuple[Any, ...],
    classify_fn: Callable[[Any, bytes], dict[str, Any] | None] | None = None,
    max_rounds: int = 6,
    deadline: float | None = None,
) -> Minimized:
    parsed = parse_testcase(testcase, abi_like)
    frames = [frame.copy() for frame in parsed.frames]
    removed = 0
    simplified = 0

    def triggers(candidate_frames: list[Frame]) -> tuple[bool, Any]:
        try:
            data = _encode(abi_like, parsed, candidate_frames)
        except Exception:
            return False, None
        observation = probe_fn(target_binary, data, work_dir=work_dir,
                               timeout_s=_probe_timeout(deadline))
        candidate = (classify_fn(observation, data) if classify_fn
                     else classify(observation, runtime_index))
        return _signature(candidate) == signature, candidate

    for _ in range(max_rounds):
        changed = False
        index = 0
        while index < len(frames):
            for block in (32, 16, 8, 4, 2, 1):
                if index + block > len(frames):
                    continue
                trial = frames[:index] + frames[index + block :]
                if not trial:
                    continue
                ok, _candidate = triggers(trial)
                if ok:
                    frames = trial
                    removed += block
                    changed = True
                    break
            else:
                index += 1
                continue
            if not changed:
                break
        if not changed:
            break
    # Simplify values, keeping every frame.
    defaults = {param.name: default_value(param) for param in abi_like.params}
    for frame_index, frame in enumerate(frames):
        for name in list(frame.values):
            current = frame.values[name]
            candidates = [defaults.get(name, current)]
            if current not in ("0", "0.0"):
                candidates += ["0", "1"]
            for value in candidates:
                if value == current:
                    continue
                trial = [item.copy() for item in frames]
                trial[frame_index].values[name] = value
                ok, _candidate = triggers(trial)
                if ok:
                    frame.values[name] = value
                    simplified += 1
                    break
    data = _encode(abi_like, parsed, frames)
    observation = probe_fn(target_binary, data, work_dir=work_dir,
                           timeout_s=_probe_timeout(deadline))
    candidate = (classify_fn(observation, data) if classify_fn
                 else classify(observation, runtime_index))
    if _signature(candidate) != signature:
        return Minimized(testcase, len(parsed.frames), None, 0, 0)
    cycle = candidate.get("cycle") if candidate else None
    return Minimized(data, len(frames), (cycle + 1) if isinstance(cycle, int) else None, removed, simplified)


def history_ablation(
    testcase: bytes,
    *,
    abi_like,
    runtime_index: dict[int, dict[str, Any]],
    target_binary: Path,
    work_dir: Path,
    probe_fn,
    signature: tuple[Any, ...],
    classify_fn: Callable[[Any, bytes], dict[str, Any] | None] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    parsed = parse_testcase(testcase, abi_like)
    frames = [frame.copy() for frame in parsed.frames]
    baseline = {param.name: default_value(param) for param in abi_like.params}
    ablation = []
    required = []
    for index, frame in enumerate(frames):
        trial = [item.copy() for item in frames]
        trial[index].values = dict(baseline)
        data = _encode(abi_like, parsed, trial)
        observation = probe_fn(target_binary, data, work_dir=work_dir,
                               timeout_s=_probe_timeout(deadline))
        candidate = (classify_fn(observation, data) if classify_fn
                     else classify(observation, runtime_index))
        still = _signature(candidate) == signature
        ablation.append({"frame": index, "replaced_with": "baseline", "still_triggers": still})
        if not still:
            required.append(index)
    return {
        "observed_depth": len(frames),
        "shortest_found_depth": len(frames),
        "required_frames": required,
        "ablation": ablation,
        "time_dependency": len(required) > 0 and len(required) < len(frames),
        "initialization_scope": "cold",
    }
