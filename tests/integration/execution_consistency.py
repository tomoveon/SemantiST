#!/usr/bin/env python3
"""Check that persistent and in-process execution report what fork execution reports.

Each target runs one fixed batch of inputs through ``semantist
--consistency-dump``: twice in fork mode, then in every requested mode in the
batch order and in a shuffled order. The batch mixes historical corpus entries,
inputs generated from the base seed and long state-driving sequences. Each
input must yield the same AFL edge map, semantic bitmap, telemetry, exit kind
and harness output values (SEMANTIST_HARNESS_OUTPUT_TRACE) as its first fork
run. Inputs whose two fork runs already disagree are reported as not
reproducible instead of being compared; crashing inputs are listed separately.

Usage:
    python3 tests/integration/execution_consistency.py \\
        --engine artifacts/.../semantist \\
        --target ctrl_pi:CTRL_PI:artifacts/perf/targets/ctrl_pi \\
        --history ctrl_pi:artifacts/experiments/<run>/unified-corpus/runtime \\
        --mode persistent --report artifacts/perf/consistency.json

A target directory holds ``fuzz_target``, ``stg/`` and
``semantic-task-plan.json`` (an adapter preprocessing directory, for example).
``--inprocess NAME:BINARY`` adds the in-process fuzzer of that target.

OSCAT function blocks read the wall clock (``TIME()``, timers), so two fork
runs of one input can differ. ``--fixed-clock`` relinks each target from its
build objects (``harness.c``, ``target.o``, ``target.semantic-runtime.o``) with
a constant ``clock_gettime``; harness and target code are unchanged. It needs
the experiment image toolchain (SEMANTIST_LLVM_BIN, SEMANTIST_AFL_RUNTIME,
SEMANTIST_RUSTY_STDLIB_LIB).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OBSERVED = ("exit_kind", "edges", "semantic", "telemetry")
FAILED_EXITS = {"crash", "timeout", "oom"}

INTEGER_TYPES = {
    "SINT": (-128, 127), "INT": (-32768, 32767), "DINT": (-(2**31), 2**31 - 1),
    "LINT": (-(2**63), 2**63 - 1), "USINT": (0, 255), "UINT": (0, 65535),
    "UDINT": (0, 2**32 - 1), "ULINT": (0, 2**64 - 1), "BYTE": (0, 255),
    "WORD": (0, 65535), "DWORD": (0, 2**32 - 1), "LWORD": (0, 2**64 - 1),
}
FIXED_CLOCK_C = """
#include <time.h>

/* Consistency checks only: every clock reads one instant. */
int clock_gettime(clockid_t clock, struct timespec *now) {
    (void)clock;
    now->tv_sec = 1700000000;
    now->tv_nsec = 0;
    return 0;
}
"""
REAL_VALUES = ("0.0", "-0.0", "1.0", "-1.0", "0.5", "1000.0", "-1000.0", "3.0e38", "-3.0e38", "1.0e-38")


@dataclass
class Target:
    name: str
    function: str
    directory: Path
    history: list[Path] = field(default_factory=list)
    inprocess: Path | None = None
    fuzz_target: str = "fuzz_target"


@dataclass
class Record:
    line: str
    observed: dict
    output: str | None


def parse_seed(text: str) -> tuple[list[tuple[str, str, str]], dict[int, list[tuple[str, str, str]]]]:
    shared: list[tuple[str, str, str]] = []
    cycles: dict[int, list[tuple[str, str, str]]] = {}
    for line in text.splitlines():
        parts = line.split(",", 2)
        if len(parts) != 3:
            continue
        name, ty, value = parts
        cycle, dot, field_name = name.partition(".")
        if dot and cycle.isdigit():
            cycles.setdefault(int(cycle), []).append((field_name, ty, value))
        else:
            shared.append((name, ty, value))
    return shared, cycles


def random_value(rng: random.Random, ty: str, extreme: bool) -> str:
    ty = ty.upper()
    if ty == "BOOL":
        return rng.choice("01")
    if ty in INTEGER_TYPES:
        low, high = INTEGER_TYPES[ty]
        if extreme or rng.random() < 0.3:
            return str(rng.choice((low, high, 0, 1, -1 if low < 0 else 2)))
        return str(rng.randint(max(low, -100_000), min(high, 100_000)))
    if ty in ("REAL", "LREAL"):
        if extreme or rng.random() < 0.3:
            return rng.choice(REAL_VALUES)
        return f"{rng.uniform(-10_000, 10_000):.3f}"
    if ty in ("STRING", "WSTRING"):
        return "".join(rng.choice("abcXYZ019;_ ") for _ in range(rng.randint(0, 40 if extreme else 12)))
    if ty in ("ARRAY", "POINTER"):
        return "0x" + "".join(f"{rng.randrange(256):02x}" for _ in range(rng.randint(1, 32)))
    return str(rng.randint(-1000, 1000))


def generated_inputs(base: str, count: int, rng: random.Random) -> list[bytes]:
    """Inputs derived from the base seed; FB sequences up to the 256-cycle bound."""
    shared, cycles = parse_seed(base)
    fields = next(iter(cycles.values()), [])
    inputs: list[bytes] = []
    for index in range(count):
        extreme = index % 4 == 0
        lines = [f"{name},{ty},{random_value(rng, ty, extreme)}" for name, ty, _ in shared]
        if fields:
            # Every fifth input drives the persistent FB state for the full
            # cycle bound, so a later short input would expose leftover state.
            length = 256 if index % 5 == 0 else rng.randint(1, 24)
            for cycle in range(length):
                lines += [f"{cycle}.{name},{ty},{random_value(rng, ty, extreme)}" for name, ty, _ in fields]
        inputs.append(("\n".join(lines) + "\n").encode())
    return inputs


def history_inputs(directories: list[Path], limit: int) -> list[bytes]:
    paths = sorted(
        path
        for directory in directories
        for path in directory.rglob("*")
        if path.is_file() and not path.name.startswith(".") and path.suffix not in (".json", ".metadata", ".log")
    )
    inputs = []
    for path in paths[:limit]:
        data = path.read_bytes()
        if b"," in data:
            inputs.append(data)
    return inputs


def run_dump(engine: Path, target: Target, inputs: list[Path], mode: str, work: Path) -> list[Record]:
    """One engine process executes every input in the given order."""
    if work.exists():
        shutil.rmtree(work)
    (work / "unified-corpus" / "seeds").mkdir(parents=True)
    shutil.copytree(target.directory / "stg", work / "stg")
    shutil.copy2(target.directory / "semantic-task-plan.json", work / "semantic-task-plan.json")
    listing = work / "inputs.txt"
    listing.write_text("".join(f"{path}\n" for path in inputs), encoding="utf-8")
    dump = work / "dump.jsonl"
    trace = work / "outputs.txt"
    binary = target.inprocess if mode == "inprocess" else engine
    command = [
        str(binary),
        "--target", str(target.directory / target.fuzz_target),
        "--function", target.function,
        "--run-dir", str(work),
        "--semantic-task-plan", str(work / "semantic-task-plan.json"),
        "--semantic-model", str(work / "stg" / "stg-model.json"),
        "--timeout-ms", "1000",
        "--consistency-dump", str(dump),
        "--consistency-inputs", str(listing),
    ]
    if mode != "inprocess":
        command += ["--execution", mode]
    env = dict(os.environ)
    env.update({
        "SEMANTIST_FB_MAX_CYCLES": "256",
        "SEMANTIST_FB_STALE_THRESHOLD": "0",
        "SEMANTIST_SEMANTIC_RUNTIME_IDS": str(work / "stg" / "stg-runtime-ids.json"),
        "SEMANTIST_HARNESS_OUTPUT_TRACE": str(trace),
        "SEMANTIST_PERSISTENT_ITERATIONS": "1000000",
    })
    result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"{target.name} {mode}: engine failed: {result.stderr[-2000:]}")
    rows = [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines()]
    outputs = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
    return records_from_dump(rows, outputs)


def records_from_dump(rows: list[dict], outputs: list[str]) -> list[Record]:
    """Pair engine rows with harness output lines; failing inputs write no line."""
    records = []
    lines = iter(outputs)
    for row in rows:
        observed = {key: row[key] for key in OBSERVED}
        output = None if row["exit_kind"] in FAILED_EXITS else next(lines, None)
        records.append(Record(row["input"], observed, output))
    return records


def compare(reference: Record, other: Record) -> list[str]:
    fields = [key for key in OBSERVED if reference.observed[key] != other.observed[key]]
    if reference.output != other.output:
        fields.append("outputs")
    return fields


def check_target(target: Target, engine: Path, modes: list[str], work: Path, args) -> dict:
    rng = random.Random(f"{args.seed}:{target.name}")
    base = next(iter(sorted((target.directory / "base-seeds").glob("*"))), None)
    batch = history_inputs(target.history, args.history_limit)
    batch += generated_inputs(base.read_text(encoding="utf-8") if base else "", args.generated, rng)
    inputs_dir = work / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, data in enumerate(batch):
        path = inputs_dir / f"{index:05d}"
        path.write_bytes(data)
        paths.append(path)
    shuffled = paths[:]
    rng.shuffle(shuffled)

    fork = run_dump(engine, target, paths, "fork", work / "fork-1")
    fork_again = run_dump(engine, target, paths, "fork", work / "fork-2")
    reference = {record.line: record for record in fork}
    unreproducible = {
        record.line: compare(record, again) for record, again in zip(fork, fork_again) if compare(record, again)
    }
    crashes = sorted(Path(record.line).name for record in fork if record.observed["exit_kind"] in FAILED_EXITS)
    report = {
        "target": target.name,
        "fuzz_target": target.fuzz_target,
        "inputs": len(paths),
        "history_inputs": len(batch) - args.generated,
        "generated_inputs": args.generated,
        "failing_inputs": crashes,
        "unreproducible_in_fork": {Path(line).name: fields for line, fields in unreproducible.items()},
        "modes": {},
    }
    for mode in modes:
        if mode == "inprocess" and target.inprocess is None:
            continue
        for order, ordered in (("batch", paths), ("shuffled", shuffled)):
            if mode == "inprocess":
                # A crash ends the in-process fuzzer; failing inputs run alone.
                ordered = [path for path in ordered if Path(path).name not in crashes]
            records = run_dump(engine, target, ordered, mode, work / f"{mode}-{order}")
            mismatches = {}
            for record in records:
                if record.line in unreproducible:
                    continue
                fields = compare(reference[record.line], record)
                if fields:
                    mismatches[Path(record.line).name] = fields
            report["modes"][f"{mode}/{order}"] = {"executed": len(records), "mismatches": mismatches}
        if mode == "inprocess":
            failing = {}
            for name in crashes:
                path = inputs_dir / name
                record = run_dump(engine, target, [path], mode, work / f"{mode}-failing-{name}")[0]
                if record.observed["exit_kind"] != reference[str(path)].observed["exit_kind"]:
                    failing[name] = [record.observed["exit_kind"], reference[str(path)].observed["exit_kind"]]
            report["modes"][f"{mode}/failing-alone"] = {"executed": len(crashes), "mismatches": failing}
    return report


def link_fixed_clock(target: Target, work: Path, sanitizers: str) -> None:
    """Relink the target's own build objects with a constant clock."""
    env = os.environ
    llvm = Path(env.get("SEMANTIST_LLVM_BIN", "/usr/lib/llvm-21/bin"))
    clang = str(llvm / "clang")
    work.mkdir(parents=True, exist_ok=True)
    source = work / "fixed_clock.c"
    source.write_text(FIXED_CLOCK_C, encoding="ascii")
    clock = work / "fixed_clock.o"
    subprocess.run([clang, "-O2", "-c", str(source), "-o", str(clock)], check=True)
    directory = target.directory
    inputs = [str(directory / name) for name in ("harness.c", "target.o", "target.semantic-runtime.o")]
    inputs += [
        env.get("SEMANTIST_AFL_RUNTIME", "/usr/local/lib/afl/afl-compiler-rt.o"),
        env.get("SEMANTIST_RUSTY_STDLIB_LIB", "/opt/rusty-semantic/target/release/libiec61131std.a"),
    ]
    link_inputs = directory / "build" / "compatibility" / target.name / "link-inputs.txt"
    if link_inputs.exists():
        inputs += [line for line in link_inputs.read_text(encoding="utf-8").splitlines() if line]
    output = directory / "fuzz_target.fixed-clock"
    subprocess.run(
        [clang, "-g", "-O1", "-fsanitize-coverage=trace-pc-guard", *sanitizers.split(), *inputs, str(clock),
         "-ldl", "-lpthread", "-lm", "-o", str(output)],
        check=True,
    )
    target.fuzz_target = output.name


def parse_targets(args) -> list[Target]:
    targets = {}
    for spec in args.target:
        name, function, directory = spec.split(":", 2)
        targets[name] = Target(name, function, Path(directory).resolve())
    for spec in args.history:
        name, directory = spec.split(":", 1)
        targets[name].history.append(Path(directory).resolve())
    for spec in args.inprocess:
        name, binary = spec.split(":", 1)
        targets[name].inprocess = Path(binary).resolve()
    return list(targets.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", required=True, type=Path, help="forkserver semantist binary")
    parser.add_argument("--target", action="append", required=True, help="NAME:FUNCTION:DIRECTORY")
    parser.add_argument("--history", action="append", default=[], help="NAME:CORPUS_DIRECTORY")
    parser.add_argument("--inprocess", action="append", default=[], help="NAME:IN_PROCESS_FUZZER")
    parser.add_argument("--mode", action="append", choices=("persistent", "inprocess"), default=[])
    parser.add_argument("--history-limit", type=int, default=300)
    parser.add_argument("--generated", type=int, default=200)
    parser.add_argument("--seed", default="semantist-consistency-v1")
    parser.add_argument("--fixed-clock", action="store_true", help="relink targets with a constant clock first")
    parser.add_argument("--sanitizers", default="-fsanitize=address -fno-omit-frame-pointer")
    parser.add_argument("--work", type=Path, help="keep run directories here (default: temporary)")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    modes = args.mode or ["persistent"]
    engine = args.engine.resolve()
    with tempfile.TemporaryDirectory(prefix="semantist-consistency-") as temporary:
        work_root = args.work.resolve() if args.work else Path(temporary)
        targets = parse_targets(args)
        if args.fixed_clock:
            for target in targets:
                link_fixed_clock(target, work_root / target.name / "fixed-clock", args.sanitizers)
        reports = [check_target(target, engine, modes, work_root / target.name, args) for target in targets]
    failed = False
    for report in reports:
        summary = ", ".join(
            f"{name}: {len(result['mismatches'])}/{result['executed']} mismatched"
            for name, result in report["modes"].items()
        )
        failed |= any(result["mismatches"] for result in report["modes"].values())
        print(
            f"[consistency] {report['target']}: {report['inputs']} inputs, "
            f"{len(report['failing_inputs'])} failing, "
            f"{len(report['unreproducible_in_fork'])} not reproducible in fork; {summary}"
        )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("[consistency] PASS" if not failed else "[consistency] FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
