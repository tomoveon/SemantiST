"""Run AFL++ and ICSQuartz against the neutral Common PLC Harness."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path

from fuzzer.experiments.artifacts import write_json
from fuzzer.experiments.matrix import ROOT

CLANG = Path("/usr/lib/llvm-21/bin/clang")
CLANGXX = Path("/usr/lib/llvm-21/bin/clang++")
ICSQUARTZ_SINGLE_LIBRARY = Path("/opt/icsquartz/liblibfuzzer_icsquartz_single.a")
ICSQUARTZ_SCAN_LIBRARY = Path("/opt/icsquartz/liblibfuzzer_icsquartz_scan_cycle.a")
STDLIB = Path("/opt/rusty-semantic/target/release/libiec61131std.a")
ICSQUARTZ_VERSION = "ICSQuartz 8021bd44f47147776c6394e008bbea23ac993076"


def _run_checked(command: list[str], log: Path) -> None:
    with log.open("a", encoding="utf-8") as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)


def _compatibility_objects(common: Path) -> list[str]:
    directory = common / "artifacts" / "build" / "compatibility"
    objects = []
    for listing in directory.rglob("link-inputs.txt") if directory.is_dir() else []:
        for line in listing.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            if line.startswith("-"):
                objects.append(line)
                continue
            path = Path(line)
            if not path.is_file() and "/artifacts/" in line:
                path = common / "artifacts" / line.split("/artifacts/", 1)[1]
            if not path.is_file():
                raise FileNotFoundError(f"compatibility link input missing: {line}")
            objects.append(str(path))
    return objects


def _build_icsquartz(common: Path, output: Path) -> None:
    layout = json.loads((common / "build" / "common" / "input-layout.json").read_text(encoding="utf-8"))
    abi = json.loads((common / "template" / "bundle" / "abi.json").read_text(encoding="utf-8"))
    is_fb = layout["kind"] == "FUNCTION_BLOCK"
    state_size = int(abi["target"]["instance_size"]) if is_fb else 0
    library = ICSQUARTZ_SCAN_LIBRARY if is_fb else ICSQUARTZ_SINGLE_LIBRARY
    if not library.is_file() or not STDLIB.is_file():
        raise FileNotFoundError("ICSQuartz engine or ordinary RuSTy standard library is missing")
    build = common / "build" / "common"
    if not (build / "harness.c").is_file() or not (build / "target.o").is_file():
        raise FileNotFoundError("Common PLC Harness build is missing")
    harness_object = output / "harness.o"
    bridge_object = output / "bridge.o"
    log = output / "compiler.log"
    _run_checked([str(CLANG), "-g", "-O1", "-DCOMMON_IN_PROCESS",
                  "-fsanitize=address,undefined", "-fsanitize-coverage=trace-pc-guard",
                  "-fno-omit-frame-pointer", "-c", str(build / "harness.c"),
                  "-o", str(harness_object)], log)
    _run_checked([str(CLANG), "-g", "-O1",
                  f"-DICS_INPUT_SIZE={layout['size']}", f"-DICS_STATE_SIZE={state_size}",
                  f"-DICS_DEFAULT_INPUT_BYTES={_c_initializer(_legal_default_input(layout))}",
                  "-c", str(ROOT / "benchmarks/experiments/icsquartz_bridge.c"),
                  "-o", str(bridge_object)], log)
    _run_checked([str(CLANGXX), "-g", "-O1", "-fsanitize=address,undefined",
                  "-fsanitize-coverage=trace-pc-guard",
                  "-no-pie", "-Wl,--allow-multiple-definition",
                  str(harness_object), str(bridge_object), str(build / "target.o"),
                  str(library), str(STDLIB),
                  *_compatibility_objects(common),
                  "-lgcc_s", "-lutil", "-lrt", "-lpthread", "-lm", "-ldl",
                  "-o", str(output / "fuzz_target")], log)


def _legal_default_input(layout: dict) -> bytes:
    data = bytearray(layout["size"])
    name = layout["target"].upper()
    if name in {"FSTRING_TO_BYTE", "FSTRING_TO_DWORD", "FSTRING_TO_MONTH",
                "TRIM1", "TRIME", "IP4_DECODE"}:
        field = next(field for field in layout["fields"] if field["spec"] == "STRING")
        value = b"2#1" if name in {"FSTRING_TO_BYTE", "FSTRING_TO_DWORD"} else b"1"
        data[field["offset"]:field["offset"] + len(value)] = value
    elif name == "SCHEDULER_2":
        for field in layout["fields"]:
            if field["name"] in {"C0", "C1", "C2", "C3"}:
                data[field["offset"]] = 1
    elif name == "XML_READER":
        ctrl = next(field for field in layout["fields"] if field["name"] == "CTRL")
        # XML_CONTROL.COMMAND starts a parse; START_POS > STOP_POS ends it.
        data[ctrl["offset"] + 1] = 0x80
        data[ctrl["offset"] + 16] = 1
    return bytes(data)


def _c_initializer(data: bytes) -> str:
    entries = [f"[{index}]={value}" for index, value in enumerate(data) if value]
    return "{" + (",".join(entries) if entries else "0") + "}"


def build(tool: str, common: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    scan_cycle = False
    if tool == "aflpp":
        layout = json.loads((common / "build" / "common" / "input-layout.json").read_text(encoding="utf-8"))
        seed = output / "afl-seed"
        seed.write_bytes(_legal_default_input(layout))
        shutil.copy2(common / "build" / "common" / "fuzz_target", output / "fuzz_target")
        version_lines = subprocess.run(["afl-fuzz", "--version"], capture_output=True,
                                       text=True, check=False).stdout.strip().splitlines()
        version = version_lines[0] if version_lines else "AFL++ unavailable"
    else:
        _build_icsquartz(common, output)
        layout = json.loads((common / "build" / "common" / "input-layout.json").read_text(encoding="utf-8"))
        scan_cycle = layout["kind"] == "FUNCTION_BLOCK"
        seed = None
        version = ICSQUARTZ_VERSION
    write_json(output / "build-result.json", {
        "status": "supported", "version": version,
        "canonical_testcases": True, "oracle_equivalent": True,
        "binary": "fuzz_target",
        "seed_file": seed.name if seed is not None else None,
        "testcase_format": "common-raw", "semantic_instrumentation": "off",
        "scan_cycle": scan_cycle,
    })


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def _launch(tool: str, build_dir: Path, output: Path, seed: int,
            seconds: int) -> list[subprocess.Popen]:
    env = os.environ.copy()
    env.update({"AFL_NO_UI": "1", "AFL_NO_AFFINITY": "1", "AFL_SKIP_CPUFREQ": "1",
                "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1",
                "ASAN_OPTIONS": "abort_on_error=1:halt_on_error=1:detect_leaks=0:symbolize=0"})
    binary = build_dir / "fuzz_target"
    cores = sorted(os.sched_getaffinity(0))
    if len(cores) != 2:
        raise RuntimeError(f"baseline trial requires two assigned CPUs, got {cores}")
    if tool == "icsquartz":
        # This LibAFL launcher indexes the inherited affinity set from zero.
        command = [str(binary), "--cores", "0,1",
                   "--broker-port", str(_free_port()),
                   "--seed", str(seed + 1),
                   "--corpus", str(output / "corpus"),
                   "--crashes", str(output / "crashes"),
                   "--fuzzer-stats", str(output / "fuzzer_stats.json"),
                   "--fuzzer-log", str(output / "fuzzer.log"),
                   "--min-input-generation", "64"]
        if json.loads((build_dir / "build-result.json").read_text(encoding="utf-8")).get("scan_cycle"):
            command.extend(["--state-resets", "--scan-cycle-max", "100"])
        commands = [command]
    else:
        inputs = output / "inputs"
        commands = [
            ["taskset", "-c", str(core), "afl-fuzz", "-i", str(inputs),
             "-o", str(output / "afl-out"), "-s", str(seed * 2 + 1 + index),
             "-V", str(seconds), "-t", "1000+", "-m", "none",
             "-M" if index == 0 else "-S", "main" if index == 0 else "secondary",
             "--", str(binary)]
            for index, core in enumerate(cores)
        ]
    processes = []
    for index, command in enumerate(commands):
        log = (output / f"engine-{index}.log").open("w", encoding="utf-8")
        try:
            processes.append(subprocess.Popen(command, cwd=output, env=env,
                                              stdout=log, stderr=subprocess.STDOUT,
                                              start_new_session=True))
        finally:
            log.close()
    return processes


def _stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _statistics(tool: str, output: Path) -> int | None:
    if tool == "icsquartz":
        path = output / "fuzzer_stats.json"
        if path.is_file():
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    return int(json.loads(line).get("executions"))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        return None
    total = 0
    found = False
    for path in (output / "afl-out").glob("*/fuzzer_stats"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("execs_done"):
                total += int(line.split(":", 1)[1].strip())
                found = True
                break
    return total if found else None


def _replay(tool: str, binary: Path, data: bytes, output: Path, name: str,
            *, sequence: bool = False) -> dict:
    testcase = output / "replay" / name
    testcase.parent.mkdir(parents=True, exist_ok=True)
    testcase.write_bytes(data)
    try:
        command = ([str(binary), "--replay-sequence" if sequence else "--replay", str(testcase)]
                   if tool == "icsquartz"
                   else [str(binary), str(testcase)])
        completed = subprocess.run(
            command, cwd=output,
            env={**os.environ, "ASAN_OPTIONS": "abort_on_error=1:halt_on_error=1:detect_leaks=0:symbolize=0"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5, check=False,
        )
        return {"returncode": completed.returncode, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"returncode": None, "timed_out": True}


def _metadata_value(node: object, key: str) -> object | None:
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _metadata_value(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _metadata_value(value, key)
            if found is not None:
                return found
    return None


def _native_sequence(path: Path, current: bytes) -> tuple[bytes | None, int | None]:
    metadata = path.with_name(f".{path.name}.metadata")
    if not metadata.is_file():
        return None, None
    try:
        record = json.loads(metadata.read_text(encoding="utf-8"))
        cycles = _metadata_value(record, "scan_cycles")
        frames_hex = _metadata_value(record, "raw_sequence_hex")
        if not isinstance(frames_hex, list) or not 1 <= len(frames_hex) <= 100:
            return None, cycles if isinstance(cycles, int) else None
        frames = [bytes.fromhex(value) for value in frames_hex]
        if any(len(frame) > (1 << 20) for frame in frames) or frames[-1] != current:
            return None, cycles if isinstance(cycles, int) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, None
    encoded = bytearray(b"ICSQSEQ1" + struct.pack("<I", len(frames)))
    for frame in frames:
        encoded.extend(struct.pack("<I", len(frame)))
        encoded.extend(frame)
    return bytes(encoded), cycles if isinstance(cycles, int) else None


def _candidates(tool: str, build_dir: Path, output: Path,
                started_ns: int, seconds: int) -> list[dict]:
    directories = ([output / "crashes"] if tool == "icsquartz"
                   else list((output / "afl-out").glob("*/crashes")))
    binary = build_dir / "fuzz_target"
    candidates = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if (not path.is_file() or path.name.startswith(".")
                    or path.name == "README.txt"):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            produced_s = max(0.0, (path.stat().st_mtime_ns - started_ns) / 1e9)
            if produced_s <= seconds:
                relative = path.relative_to(output)
                name = str(len(candidates))
                raw_replay = _replay(tool, binary, data, output, name)
                candidate = {"testcase_file": str(relative),
                             "first_candidate_s": round(produced_s, 3),
                             "replay": raw_replay, "size": len(data)}
                if tool == "icsquartz":
                    sequence, cycles = _native_sequence(path, data)
                    candidate["scan_cycles"] = cycles
                    candidate["raw_replay"] = raw_replay
                    if sequence is not None:
                        sequence_path = output / "sequences" / f"{path.name}.icsqseq"
                        sequence_path.parent.mkdir(exist_ok=True)
                        sequence_path.write_bytes(sequence)
                        candidate["sequence_file"] = str(sequence_path.relative_to(output))
                        candidate["sequence_frames"] = struct.unpack_from("<I", sequence, 8)[0]
                        candidate["replay"] = _replay(tool, binary, sequence, output,
                                                      f"sequence-{name}", sequence=True)
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: item["first_candidate_s"])


def run(tool: str, common: Path, build_dir: Path, output: Path,
        seed: int, seconds: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    raw_seed = build_dir / "afl-seed" if tool == "aflpp" else None
    if tool == "aflpp":
        inputs = output / "inputs"
        inputs.mkdir()
        if raw_seed.is_file():
            shutil.copy2(raw_seed, inputs / raw_seed.name)
    started = time.monotonic()
    started_ns = time.time_ns()
    processes = _launch(tool, build_dir, output, seed, seconds)
    early_failure = False
    try:
        while time.monotonic() - started < seconds:
            if any(process.poll() not in (None, 0) for process in processes):
                early_failure = True
                break
            if all(process.poll() is not None for process in processes):
                break
            time.sleep(0.2)
    finally:
        online_elapsed = min(float(seconds), time.monotonic() - started)
        _stop(processes)
    candidates = _candidates(tool, build_dir, output, started_ns, seconds)
    executions = _statistics(tool, output)
    corpus_empty = (tool == "icsquartz" and
                    not any(path.is_file() for path in (output / "corpus").glob("*")))
    status = "failed" if early_failure or not executions or corpus_empty else "completed"
    reason = ("fuzzer failed before deadline" if early_failure else
              "fuzzer completed without executing an input" if not executions else
              "fuzzer did not create a live corpus" if corpus_empty else None)
    write_json(output / "adapter-result.json", {
        "status": status, "reason": reason,
        "testcase_format": "common-raw", "cold_reset": True,
        "online_elapsed_s": round(online_elapsed, 3),
        "executions": executions, "scans": None,
        "native_crash_events": len(candidates) if tool == "icsquartz" else None,
        "native_sequence_saved": (sum("sequence_file" in item for item in candidates)
                                  if tool == "icsquartz" else None),
        "candidates": candidates,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "run"))
    parser.add_argument("--tool", choices=("aflpp", "icsquartz"), required=True)
    parser.add_argument("--common-build", type=Path, required=True)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args()
    if args.action == "build":
        build(args.tool, args.common_build, args.output)
    else:
        run(args.tool, args.common_build, args.build, args.output,
            args.seed, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
