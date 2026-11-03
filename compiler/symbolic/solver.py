"""Run one KLEE query and turn a goal ktest into a canonical testcase."""

from __future__ import annotations

import json
import math
import os
import signal
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

from compiler.symbolic.driver import generate_driver, symbolic_scope
from fuzzer.pipeline.probe import classify, load_runtime_index, probe


_ORIGINAL_SUBPROCESS_RUN = subprocess.run

_UNSIGNED_TYPE_NAMES = {
    "BOOL", "USINT", "BYTE", "CHAR", "UINT", "WORD", "WCHAR",
    "UDINT", "DWORD", "ULINT", "LWORD",
}


def _run_with_deadline(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one tool in a process group and enforce the query deadline."""
    # Unit tests replace subprocess.run with a deterministic fake. Preserve
    # that seam while production commands use a process group so child tools
    # cannot outlive the shared query deadline.
    if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def decode_ktest(path: Path) -> dict[str, bytes]:
    data = Path(path).read_bytes()
    if len(data) < 9 or data[:5] != b"KTEST":
        raise ValueError("not a KTEST file")
    cursor = 5

    def u32() -> int:
        nonlocal cursor
        value = struct.unpack_from(">I", data, cursor)[0]
        cursor += 4
        return value

    version = u32()
    if version < 2:
        raise ValueError(f"unsupported ktest version {version}")
    num_args = u32()
    for _ in range(num_args):
        length = u32()
        cursor += length
    # KLEE 3.x writes symArgvs/symArgvLen before the object table.
    sym_argvs = u32()
    sym_argv_len = u32()
    num_objects = u32()
    objects: dict[str, bytes] = {}
    for _ in range(num_objects):
        name_length = u32()
        name = data[cursor : cursor + name_length].decode("utf-8", errors="replace")
        cursor += name_length
        size = u32()
        objects[name] = data[cursor : cursor + size]
        cursor += size
    return objects


def _int_value(raw: bytes, signed: bool) -> int:
    return int.from_bytes(raw, "little", signed=signed)


def _field_from_objects(
    abi: dict[str, Any],
    objects: dict[str, bytes],
    base_frame: dict[str, str],
    object_prefix: str = "",
) -> dict[str, str]:
    frame_fields = {entry["name"]: entry for entry in abi.get("frame_fields", [])}
    inout_fields = [entry for entry in abi.get("shared_fields", [])
                    if entry.get("block_kind") == "VAR_IN_OUT"]
    frame_fields.update({entry["name"]: entry for entry in inout_fields})
    values = dict(base_frame)
    frame_entries = list((abi.get("frame") or {}).get("fields", []))
    seen_frame_names = {entry.get("name") for entry in frame_entries if isinstance(entry, dict)}
    frame_entries.extend(entry for entry in abi.get("frame_fields", [])
                         if isinstance(entry, dict) and entry.get("name") not in seen_frame_names)
    for entry in [*frame_entries, *inout_fields]:
        name = entry["name"]
        if name not in frame_fields:
            continue
        spec = entry["spec"]
        raw = objects.get(object_prefix + name)
        if spec.get("kind") == "scalar" and raw is not None:
            type_name = str(spec.get("name") or "").upper()
            c_type = str(spec.get("c_type") or "").lower()
            signed = not (c_type.startswith("u") or type_name in _UNSIGNED_TYPE_NAMES)
            if spec.get("name") in ("REAL", "LREAL"):
                value = struct.unpack("<f" if spec.get("name") == "REAL" else "<d", raw.ljust(4 if spec.get("name") == "REAL" else 8, b"\0")[:4 if spec.get("name") == "REAL" else 8])[0]
                # Native frame parsing rejects NaN and infinities.  Keep the
                # candidate executable if KLEE selected a non-finite bit
                # pattern by falling back to the concrete base value.
                values[name] = repr(value) if math.isfinite(value) else str(base_frame.get(name, "0.0"))
            else:
                values[name] = str(_int_value(raw, signed))
        elif spec.get("kind") == "string" and raw is not None:
            width = int(spec.get("size") or 1)
            if width == 2:
                payload = raw[: len(raw) - (len(raw) % 2)]
                value = payload.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
            else:
                value = raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
            values[name] = value[: int(spec.get("length") or len(value))]
        elif spec.get("kind") == "pointer" and raw is not None:
            values[name] = "0x" + raw.hex()
        elif spec.get("kind") == "array":
            element = spec.get("element") or {}
            element_size = int(element.get("byte_size") or element.get("size") or 1)
            chunks = []
            for index in range(min(int(spec.get("count") or 0), 32)):
                chunk = objects.get(f"{object_prefix}{name}[{index}]")
                if chunk is None:
                    break
                chunks.append(chunk.rjust(element_size, b"\0")[:element_size].hex())
            if chunks:
                values[name] = "0x" + "".join(chunks)
    return values


def run_query(
    *,
    bundle: Path,
    run_dir: Path,
    target_binary: Path,
    abi: dict[str, Any],
    runtime_ids: Path,
    goal_id: int,
    query_id: str,
    task_id: str | None = None,
    base_frame: dict[str, str] | None = None,
    shared_records: str = "",
    dt_ns: int = 10_000_000,
    symbolic_fields: list[str] | None = None,
    prefix_frames: list[dict[str, str]] | None = None,
    prefix_dt_ns: list[int | None] | None = None,
    state_file: Path | None = None,
    relocation_file: Path | None = None,
    window: int = 1,
    time_budget_s: float = 30.0,
    memory_mb: int = 2048,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + max(0.0, time_budget_s)
    actual_symbolic, concretized = symbolic_scope(abi, symbolic_fields)
    prefix_count = len(prefix_frames or [])
    prefix_deltas = [prefix_dt_ns[index] if prefix_dt_ns is not None and index < len(prefix_dt_ns)
                     and prefix_dt_ns[index] is not None else dt_ns for index in range(prefix_count)]
    action_names = [entry["name"] for entry in abi.get("frame_fields", [])]
    action_names.extend(entry["name"] for entry in abi.get("shared_fields", [])
                        if entry.get("block_kind") == "VAR_IN_OUT")
    scope = {
        "start": "snapshot_import" if state_file is not None else ("prefix_replay" if prefix_frames else "cold"),
        "snapshot_fallback": False,
        "state_file": str(state_file) if state_file is not None else None,
        "relocation_file": str(relocation_file) if relocation_file is not None else None,
        "prefix_depth": prefix_count,
        "query_depth": prefix_count + max(1, int(window)),
        "window": max(1, int(window)),
        "suffix_frames": max(1, int(window)),
        "symbolic_fields": actual_symbolic,
        # ``fixed_fields`` is retained for the existing exchange format;
        # ``concrete_fields`` is the field name required by reports.
        "fixed_fields": [name for name in action_names if name not in actual_symbolic and name not in concretized],
        "concretized": concretized,
        "shared_records": shared_records,
        "init": "import" if state_file is not None else "cold",
        "prefix_dt_ns": prefix_deltas,
        "suffix_dt_ns": [dt_ns] * max(1, int(window)),
        "task_id": task_id,
        "task_goal_id": goal_id,
        "models": ["semantic_klee.c", "libc_stubs.c", "stdlib_stubs.c", "memory_stubs.c"],
        "klee_version": "3.3",
    }
    scope["concrete_fields"] = list(scope["fixed_fields"])

    # A compatibility manifest or fallback stub can make the whole execution
    # contract approximate even when the KLEE model itself is classified as
    # faithful.  Carry that provenance into every query: an approximate
    # contract may still produce a useful SAT candidate, but a no-goal result
    # must not be presented as a proof that the target has no defect.
    contract_approximate = False
    contract_path = Path(bundle) / "execution-contract.json"
    if contract_path.exists():
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            contract = {}
        if isinstance(contract, dict):
            fidelity = str(contract.get("semantic_fidelity") or "").lower()
            stub_used = contract.get("stub_used")
            contract_approximate = fidelity == "approximate" or bool(stub_used)
            scope["semantic_fidelity"] = fidelity or "unknown"
            scope["stub_used"] = stub_used if isinstance(stub_used, list) else []
    scope["contract_approximate"] = contract_approximate

    def query_result(status: str, **fields: Any) -> dict[str, Any]:
        return {"status": status, "query_id": query_id, "task_id": task_id,
                "solver_scope": scope,
                "model_scope": scope,
                "stats": {"wall_s": round(time.monotonic() - started, 3)}, **fields}

    def remaining(phase: str) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"query deadline expired before {phase}")
        return left

    klee_dir = bundle / "klee"
    manifest_path = klee_dir / "manifest.json"
    unsupported = klee_dir / "UNSUPPORTED.json"
    if unsupported.exists():
        try:
            document = json.loads(unsupported.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {}
        status = str(document.get("status", "unsupported")).upper()
        return query_result(status if status in {"UNSUPPORTED", "FAILED", "TIMEOUT"} else "UNSUPPORTED",
                      reason=document.get("reason", str(unsupported)), detail=document.get("detail"))
    if not manifest_path.exists():
        return query_result("UNSUPPORTED", reason="KLEE manifest missing")
    state_bytes = None
    relocations: list[dict[str, Any]] = []
    if state_file is not None:
        try:
            state_bytes = Path(state_file).read_bytes()
        except OSError as error:
            return query_result("UNSUPPORTED", reason=f"state snapshot unavailable: {error}")
        if not state_bytes:
            return query_result("UNSUPPORTED", reason="state snapshot is empty")
        try:
            layout = json.loads((Path(bundle) / "state-layout.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return query_result("UNSUPPORTED", reason=f"state layout unavailable: {error}")
        pointer_regions = [region.get("name") for region in layout.get("regions", [])
                           if region.get("pointer_offsets")]
        if pointer_regions:
            relocation_path = relocation_file or state_file.with_suffix(".reloc.json")
            try:
                relocation_document = json.loads(relocation_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                return query_result("UNSUPPORTED", reason=f"snapshot relocation metadata unavailable: {error}",
                                    pointer_regions=pointer_regions)
            if not relocation_document.get("complete"):
                return query_result("UNSUPPORTED",
                                    reason=relocation_document.get("reason", "snapshot relocation is incomplete"),
                                    pointer_regions=pointer_regions)
            if not isinstance(relocation_document.get("relocations", []), list):
                return query_result("UNSUPPORTED", reason="snapshot relocation metadata is malformed",
                                    pointer_regions=pointer_regions)
            relocations = [item for item in relocation_document["relocations"] if isinstance(item, dict)]
            scope["relocation_count"] = len(relocations)
    query_dir = run_dir / "solver" / "queries" / query_id
    query_dir.mkdir(parents=True, exist_ok=True)
    driver = query_dir / "driver.c"
    driver.write_text(
        generate_driver(
            abi,
            goal_id=goal_id,
            dt_ns=dt_ns,
            base_frame=base_frame,
            symbolic_fields=actual_symbolic,
            shared_records=shared_records,
            prefix_frames=[] if state_bytes is not None else prefix_frames,
            prefix_dt_ns=prefix_dt_ns,
            dt_prefix_ns=dt_ns,
            window=max(1, int(window)),
            state_bytes=state_bytes,
            relocations=relocations,
        ),
        encoding="utf-8",
    )
    clang = os.environ.get("SEMANTIST_CLANG18", "/usr/bin/clang-18")
    klee_include = os.environ.get("SEMANTIST_KLEE_INCLUDE", "/opt/klee/include")
    driver_bc = query_dir / "driver.bc"
    driver_tmp = query_dir / "driver.bc.tmp"
    linked = query_dir / "query.bc"
    linked_tmp = query_dir / "query.bc.tmp"
    for stale in (driver_bc, driver_tmp, linked, linked_tmp):
        stale.unlink(missing_ok=True)
    try:
        result = _run_with_deadline(
            [clang, "-I", klee_include, "-O0", "-emit-llvm", "-c", str(driver), "-o", str(driver_tmp)],
            timeout=remaining("driver compile"),
        )
    except FileNotFoundError as error:
        return query_result("UNSUPPORTED", reason=f"driver compiler unavailable: {error.filename}")
    except OSError as error:
        return query_result("FAILED", reason=f"driver compiler could not start: {error}")
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        driver_tmp.unlink(missing_ok=True)
        return query_result("TIMEOUT", reason=f"driver compile: {error}")
    if result.returncode != 0:
        driver_tmp.unlink(missing_ok=True)
        return query_result("FAILED", reason="driver compile failed", detail=result.stderr[-2000:])
    if not driver_tmp.exists():
        return query_result("FAILED", reason="driver compile produced no bitcode")
    os.replace(driver_tmp, driver_bc)
    llvm_link = os.environ.get("SEMANTIST_LLVM_LINK16", "/usr/lib/llvm-18/bin/llvm-link")
    try:
        result = _run_with_deadline(
            [llvm_link, str(klee_dir / "bundle.bc"), str(driver_bc), "-o", str(linked_tmp)],
            timeout=remaining("llvm-link"),
        )
    except FileNotFoundError as error:
        return query_result("UNSUPPORTED", reason=f"llvm-link unavailable: {error.filename}")
    except OSError as error:
        return query_result("FAILED", reason=f"llvm-link could not start: {error}")
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        linked_tmp.unlink(missing_ok=True)
        return query_result("TIMEOUT", reason=f"llvm-link: {error}")
    if result.returncode != 0:
        linked_tmp.unlink(missing_ok=True)
        return query_result("FAILED", reason="llvm-link failed", detail=result.stderr[-2000:])
    if not linked_tmp.exists():
        return query_result("FAILED", reason="llvm-link produced no bitcode")
    os.replace(linked_tmp, linked)
    klee = os.environ.get("SEMANTIST_KLEE", "/opt/klee/bin/klee")
    output_dir = query_dir / "klee-out"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    imprecise = []
    manifest_path = Path(bundle) / "klee" / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            imprecise = manifest.get("imprecise", [])
            scope["model_classes"] = manifest.get("model_classes", {})
            model_classes = scope["model_classes"]
            if isinstance(model_classes, dict):
                # Older or hand-produced manifests may omit ``imprecise``;
                # concretizing/havoc classification still makes UNSAT
                # untrustworthy and must be visible in the query scope.
                classified_imprecise = [
                    name
                    for category in ("concretizing", "havoc", "approximating")
                    for name in (model_classes.get(category) or [])
                    if isinstance(name, str) and name
                ]
                imprecise = sorted(set(imprecise or []) | set(classified_imprecise))
        except json.JSONDecodeError:
            imprecise = []
    if concretized:
        imprecise = sorted(set(imprecise + [f"concretized:{name}" for name in concretized]))
    float_entries = [*abi.get("frame_fields", []), *abi.get("shared_fields", [])]

    def contains_float(spec: dict[str, Any]) -> bool:
        if spec.get("kind") == "scalar":
            return spec.get("name") in {"REAL", "LREAL"}
        element = spec.get("element")
        return isinstance(element, dict) and contains_float(element)

    float_symbolic = [
        name for name in actual_symbolic
        if any(entry.get("name") == name
               and contains_float(entry.get("spec", {}))
               for entry in float_entries)
    ]
    if float_symbolic:
        # Raw IEEE bytes are now exposed to KLEE for coverage, but the current
        # model does not prove floating-point path completeness.  Keep such a
        # query UNKNOWN rather than presenting an incomplete UNSAT as a proof.
        imprecise = sorted(set(imprecise + [f"float_symbolic:{name}" for name in float_symbolic]))
    scope["fixed_float_anchors"] = {
        entry["name"]: base_frame[entry["name"]]
        for entry in float_entries
        if entry.get("name") not in actual_symbolic
        and contains_float(entry.get("spec", {}))
        and entry.get("name") in (base_frame or {})
    }
    scope["imprecise"] = imprecise
    try:
        klee_time = remaining("KLEE")
    except TimeoutError as error:
        return query_result("TIMEOUT", reason=str(error))
    command = [
        klee,
        "--libc=none",
        "--external-calls=none",
        "--search=dfs",
        "--exit-on-error-type=ReportError",
        f"--max-time={max(1, math.ceil(klee_time))}",
        f"--max-memory={int(memory_mb)}",
        f"--output-dir={output_dir}",
        str(linked),
    ]
    try:
        completed = _run_with_deadline(command, timeout=remaining("KLEE"))
        stderr = completed.stderr
    except FileNotFoundError as error:
        return query_result("UNSUPPORTED", reason=f"KLEE unavailable: {error.filename}")
    except OSError as error:
        return query_result("FAILED", reason=f"KLEE could not start: {error}")
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        return query_result("TIMEOUT", reason="KLEE exceeded the wall-clock guard", detail=str(error))
    if completed.returncode != 0:
        return query_result("FAILED", reason=f"KLEE exited with status {completed.returncode}",
                            detail=(completed.stderr or completed.stdout)[-4000:])
    import re
    imprecise_calls = sorted(set(re.findall(r"imprecise: ([A-Za-z0-9_]+)", stderr)))
    if imprecise_calls:
        imprecise = sorted(set(imprecise + imprecise_calls))
        scope["imprecise"] = imprecise
    (query_dir / "klee.log").write_text(stderr, encoding="utf-8", errors="replace")
    goals = sorted(output_dir.glob("*.goal"))
    approximate_model = bool(imprecise or contract_approximate)
    seed_only = not goals
    no_goal_status = "UNSAT" if "completed paths" in stderr and not approximate_model else "UNKNOWN"
    no_goal_reason = ("approximate execution contract cannot establish UNSAT"
                      if no_goal_status == "UNKNOWN" and contract_approximate and not imprecise else None)
    tests = []
    if not goals:
        tests = sorted(output_dir.glob("*.ktest"))[:16]
        if not tests:
            return query_result(no_goal_status, imprecise=imprecise, reason=no_goal_reason)
        ktest = tests[0]
    else:
        ktest = goals[0].with_suffix(".ktest")
    if not ktest.exists():
        return query_result("UNKNOWN", reason="goal without ktest")
    try:
        objects = decode_ktest(ktest)
    except ValueError as error:
        return query_result("UNKNOWN", reason=str(error))
    from fuzzer.corpus.frames import build_testcase

    # Build the candidate directly from the ABI so the worker does not need
    # the full model; reuse the small adapter from search.
    class _Frames:
        def __init__(self, entries, shared_entries):
            self.params = [_SpecParam(entry) for entry in entries]
            self.state_fields = [_SpecParam(entry) for entry in shared_entries]

    class _SpecParam:
        def __init__(self, entry):
            self.name = entry["name"]
            self.spec = _Spec(entry["spec"])
            self.block_kind = entry.get("block_kind", "VAR_INPUT")
            self.constant = False
            self.index = entry.get("index")

    class _Spec:
        def __init__(self, spec):
            self.kind = spec.get("kind")
            self.name = spec.get("name")
            self.c_type = spec.get("c_type")
            self.length = spec.get("length")
            self.size = spec.get("size")
            self._byte_size = spec.get("byte_size")
            self.count = spec.get("count")

        def byte_size(self):
            return int(self.__dict__.get("_byte_size") or self.__dict__.get("size") or 0)

    abi_like = _Frames(abi.get("frame_fields", []), abi.get("shared_fields", []))
    shared = {}
    for line in shared_records.splitlines():
        parts = line.split(",", 2)
        if len(parts) == 3 and parts[0] != "__INIT":
            shared[parts[0]] = parts[2]
    candidate_dt = list(prefix_deltas)
    candidate_dt.extend([dt_ns] * max(1, int(window)))

    def testcase_for(ktest_objects: dict[str, bytes]) -> bytes:
        suffix_frames = []
        for t in range(max(1, int(window))):
            object_prefix = f"f{t}." if int(window) > 1 else ""
            suffix_frames.append(_field_from_objects(
                abi, ktest_objects, base_frame or {}, object_prefix))
        return build_testcase(abi_like, list(prefix_frames or []) + suffix_frames,
                              shared=shared, frame_dt_ns=candidate_dt, include_dt=True)

    candidate = testcase_for(objects)
    candidate_path = query_dir / "candidate.txt"
    candidate_path.write_bytes(candidate)
    if seed_only:
        seed_files = []
        for extra in tests[1:]:
            try:
                extra_candidate = testcase_for(decode_ktest(extra))
            except (OSError, ValueError):
                continue
            extra_path = query_dir / f"candidate-{extra.stem}.txt"
            extra_path.write_bytes(extra_candidate)
            seed_files.append(str(extra_path))
        return query_result(no_goal_status, imprecise=imprecise, reason=no_goal_reason,
                            candidate_file=str(candidate_path), seed_files=seed_files,
                            seed_only=True)
    try:
        observation = probe(target_binary, candidate, work_dir=run_dir / "probe",
                            timeout_s=remaining("native cold replay"))
    except TimeoutError as error:
        return query_result("TIMEOUT", reason=str(error), candidate_file=str(candidate_path))
    if observation.timed_out:
        return query_result("TIMEOUT", reason="native cold replay timed out",
                            candidate_file=str(candidate_path))
    if goal_id not in observation.hits:
        return query_result("REPLAY_MISMATCH", reason="SAT candidate missed the requested goal natively",
                            candidate_file=str(candidate_path), native_stderr=observation.stderr[-1000:])
    runtime_index = load_runtime_index(runtime_ids) if runtime_ids.exists() else {}
    goal_entry = runtime_index.get(goal_id, {})
    if goal_entry.get("modeling") in {"conservative", "opaque"}:
        # A conservative STG mapping may still carry a concrete IR predicate.
        # Promote only the requested predicate after native cold replay; the
        # solver model remains approximate and cannot establish absence.
        observed = classify(observation, runtime_index)
        if observed is not None and observed.get("runtime_id") == goal_id and observed.get("observed_predicate"):
            return query_result("SAT_APPROX", candidate_file=str(candidate_path),
                                candidate=observed, native_goal_reached=True,
                                reason="native cold replay observed the typed conversion predicate")
        return query_result(
            "SAT_APPROX", candidate_file=str(candidate_path), candidate=None,
            native_goal_reached=True,
            reason="native cold replay reached a model-incomplete goal; no vulnerability classification",
        )
    classified = classify(observation, runtime_index)
    if classified is None:
        return query_result("REPLAY_MISMATCH", reason="SAT candidate did not reach a goal natively",
                            candidate_file=str(candidate_path), native_stderr=observation.stderr[-1000:])
    return query_result("SAT_APPROX" if approximate_model else "SAT",
                        candidate_file=str(candidate_path), candidate=classified)
