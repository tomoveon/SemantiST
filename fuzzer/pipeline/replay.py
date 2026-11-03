"""Replay one confirmed finding package."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from fuzzer.pipeline.probe import Observation, classify, load_runtime_index, probe

REPLAY_BUILD_TIMEOUT_S = 180.0


class ReplayContractError(ValueError):
    """The repro package does not match its recorded execution contract."""


def _run_rebuild(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a rebuild in its own process group so timeout cleanup reaches tools."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=REPLAY_BUILD_TIMEOUT_S)
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
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            REPLAY_BUILD_TIMEOUT_S,
            output=stdout or error.output,
            stderr=stderr or error.stderr,
        ) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _replay_status(observation: Observation, candidate: dict | None, expected: str | None) -> str:
    """Map native replay evidence to the shared report status vocabulary."""
    if observation.timed_out:
        return "TIMEOUT"
    if candidate is None:
        # A target crash is a valid native observation and is classified below
        # only when the probe decoded it. An otherwise unclassified non-zero
        # process exit remains a replay failure.
        if observation.returncode not in (None, 0):
            return "FAILED"
        return "REPLAY_MISMATCH" if expected else "candidate"
    if expected and candidate.get("oracle") != expected:
        return "REPLAY_MISMATCH"
    return "confirmed" if expected else "candidate"


def _load_contract(package: Path, finding: dict) -> dict | None:
    path = package / "execution-contract.json"
    if not path.exists():
        return None
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReplayContractError(f"invalid execution contract: {error}") from error
    if not isinstance(contract, dict):
        raise ReplayContractError("execution contract is not a JSON object")
    return contract


def _target_from_contract(package: Path, finding: dict, contract: dict | None) -> Path | None:
    for candidate in (
        package / "binary" / "fuzz_target",
        os.environ.get("SEMANTIST_TARGET_BINARY"),
        finding.get("target_binary"),
    ):
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = package / path
        if path.is_file():
            return path
    return None


def _build_from_package(package: Path, work_dir: Path) -> tuple[Path, Path] | None:
    source = package / "source" / "target.st"
    if not source.exists():
        return None
    function = json.loads((package / "finding.json").read_text(encoding="utf-8")).get("function")
    if not function:
        return None
    run_dir = work_dir / "replay-run"
    command = [
        sys.executable,
        "-m",
        "fuzzer.pipeline.core",
        "--st-file",
        str(source),
        "--function",
        function,
        "--run-dir",
        str(run_dir),
        "--build-only",
        "--skip-report",
    ]
    try:
        repro = json.loads((package / "repro-manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        repro = {}
    resources = {item.get("kind"): item.get("path") for item in repro.get("resources", [])
                 if isinstance(item, dict) and item.get("kind") and item.get("path")}
    for kind, option in (("library", "--library"), ("stubs", "--stubs"),
                         ("compatibility_manifest", "--compatibility-manifest")):
        relative = resources.get(kind)
        if relative:
            path = package / relative
            if path.exists():
                command.extend([option, str(path)])
    profile = repro.get("profile")
    if isinstance(profile, str) and profile.strip():
        command.extend(["--profile", profile])
    try:
        completed = _run_rebuild(command)
    except subprocess.TimeoutExpired as error:
        print(f"replay: rebuild timed out after {REPLAY_BUILD_TIMEOUT_S:.0f}s: {error}", file=sys.stderr)
        return None
    except OSError as error:
        print(f"replay: rebuild could not start: {error}", file=sys.stderr)
        return None
    if completed.returncode != 0:
        print(completed.stdout[-2000:], file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        return None
    target = run_dir / "fuzz_target"
    if not target.exists():
        candidates = list((run_dir.parent / "build" / "targets").glob("*/fuzz_target"))
        target = candidates[0] if candidates else target
    runtime = run_dir / "stg" / "stg-runtime-ids.json"
    return target, runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args(argv)
    package = args.package.resolve()
    testcase_path = package / "minimized.txt"
    if not testcase_path.exists():
        testcase_path = package / "testcase.txt"
    try:
        testcase = testcase_path.read_bytes()
        finding = json.loads((package / "finding.json").read_text(encoding="utf-8"))
        if not isinstance(finding, dict):
            raise ValueError("finding.json is not a JSON object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(json.dumps({"status": "UNSUPPORTED", "execution_status": {"replay": "UNSUPPORTED"},
                          "reason": f"invalid replay package: {error}"}))
        print(f"replay: invalid replay package: {error}", file=sys.stderr)
        return 2
    try:
        contract = _load_contract(package, finding)
    except ReplayContractError as error:
        print(json.dumps({"status": "UNSUPPORTED", "execution_status": {"replay": "UNSUPPORTED"},
                          "reason": str(error)}))
        print(f"replay: {error}", file=sys.stderr)
        return 2
    temporary = None
    target = _target_from_contract(package, finding, contract)
    runtime_path = None
    if target is None:
        temporary = tempfile.TemporaryDirectory()
        built = _build_from_package(package, Path(temporary.name))
        if built is None:
            print(json.dumps({"status": "FAILED", "execution_status": {"replay": "FAILED"},
                              "reason": "no target binary and no rebuild recipe"}))
            print("replay: no target binary and no rebuild recipe", file=sys.stderr)
            return 2
        target, runtime_path = built
    else:
        runtime_path = package / "source" / "stg-runtime-ids.json"
    runtime_index = load_runtime_index(runtime_path) if runtime_path and runtime_path.exists() else {}
    observation = probe(target, testcase, work_dir=package / ".replay")
    candidate = classify(observation, runtime_index)
    expected = finding.get("oracle")
    status = _replay_status(observation, candidate, expected)
    print(json.dumps({"expected": expected, "observed": candidate, "returncode": observation.returncode,
                      "status": status,
                      "support_status": finding.get("support_status") or (contract or {}).get("support_status"),
                      "execution_status": {"replay": status},
                      "semantic_fidelity": finding.get("semantic_fidelity") or (contract or {}).get("semantic_fidelity"),
                      "stub_used": finding.get("stub_used") or (contract or {}).get("stub_used", []),
                      "model_scope": finding.get("model_scope") or (contract or {}).get("model_scope", {}),
                      "stderr": observation.stderr.splitlines()[:3]}, indent=2))
    if temporary is not None:
        temporary.cleanup()
    if candidate is None:
        return 1
    if expected and candidate.get("oracle") != expected:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
