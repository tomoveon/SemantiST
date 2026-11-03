"""Toolchain-side part of :mod:`.evaluate` (runs in the toolchain image).

Usage: ``python3 -m compiler.intake.stubs.evaluate_check REQUEST RESULT``.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
from pathlib import Path

from .. import rusty
from ..build import blank
from ..library import resolve_profiles
from ..normalize.probes import PRINTF, run_program
from ..structure import split_units


def _sources(directory: Path, files: list[str], dependency_accepted: dict[str, list[int]], work: Path) -> list[Path]:
    """The normalized files with the units the intake rejected blanked. The
    library's report covers its own file; a dependency file keeps the units
    its own library accepted (``dependency_accepted``: file -> unit lines)."""
    report = json.loads((directory / "compatibility-report.json").read_text(encoding="utf-8"))
    accepted = {(u["file"], u["line"]) for u in report["units"] if u["accepted"]}
    accepted |= {(name, line) for name, lines in dependency_accepted.items() for line in lines}
    paths = []
    for name in files:
        text = (directory / "normalized" / name).read_text(encoding="utf-8")
        spans = [(u.start, u.end) for u in split_units(text) if (name, u.line) not in accepted]
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(blank(text, spans), encoding="utf-8")
        paths.append(path)
    return paths


def build(job: dict) -> dict:
    directory = Path(job["dir"])
    meta = json.loads((directory / "compatibility.json").read_text(encoding="utf-8"))
    report = json.loads((directory / "compatibility-report.json").read_text(encoding="utf-8"))
    profiles = resolve_profiles(meta.get("profiles", []))
    accepted = [(u["file"], u["line"], u["name"]) for u in report["units"] if u["accepted"] and u["file"] == job["main"]]
    with tempfile.TemporaryDirectory(prefix="semantist-stub-eval-") as work:
        work = Path(work)
        paths = _sources(directory, [job["main"]], {}, work)
        stub = work / "semantist_stubs.st"
        stub.write_text(job["stubs"] + "\n", encoding="utf-8")
        result = rusty.check([*paths, *profiles["st_sources"], stub], extra=list(meta["compiler"]["args"]))
        text = (directory / "normalized" / job["main"]).read_text(encoding="utf-8")
    ranges = {u.line: u.end_line for u in split_units(text)}
    failing = {d.line for d in result.errors if d.path and Path(d.path).name == job["main"]}
    kept = [name for file, line, name in accepted if not any(line <= f <= ranges.get(line, line) for f in failing)]
    stub_errors = [f"{d.code} {d.message.splitlines()[0][:160]}" for d in result.errors
                   if d.path and Path(d.path).name == stub.name]
    return {"library": job["library"], "accepted_with_dependency": len(accepted),
            "still_type_check_with_stubs": len(kept), "stub_errors": stub_errors[:10], "panic": result.panic}


def behaviour(item: tuple[str, str, dict]) -> tuple[str, dict]:
    key, program, job = item
    directory = Path(job["dir"])
    meta = json.loads((directory / "compatibility.json").read_text(encoding="utf-8"))
    report = json.loads((directory / "compatibility-report.json").read_text(encoding="utf-8"))
    dependency_files = [d["file"] for d in report["target_identity"].get("dependency_files", [])]
    with tempfile.TemporaryDirectory(prefix="semantist-stub-eval-") as work:
        work = Path(work)
        paths = _sources(directory, dependency_files, job["dependency_accepted"], work)
        profiles = resolve_profiles(meta.get("profiles", []))
        code, output = run_program(program, [PRINTF, *paths, *profiles["st_sources"]], list(meta["compiler"]["args"]))
    if code != 0:
        lines = output.strip().splitlines()
        first = next((line for line in lines if "error" in line and "aborted" not in line), lines[-1] if lines else "")
        return key, {"error": first[:200] or f"exit {code}"}
    pairs = [line.split() for line in output.splitlines() if len(line.split()) == 2]
    agree = sum(1 for a, b in pairs if a == b or _close(a, b))
    return key, {"samples": len(pairs), "agree": agree, "first_difference": next(
        (f"real {a}, stub {b}" for a, b in pairs if not (a == b or _close(a, b))), None)}


def _close(a: str, b: str) -> bool:
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    return abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y))


def main(argv: list[str]) -> int:
    request = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    jobs = {job["library"]: job for job in request["builds"]}
    with multiprocessing.Pool(16) as pool:
        builds = pool.map(build, request["builds"])
        programs = dict(pool.map(behaviour, [(key, program, jobs[key.rsplit(":", 1)[0]])
                                             for key, program in sorted(request["programs"].items())]))
    result_path = Path(argv[1])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({"builds": builds, "programs": programs}, ensure_ascii=False, indent=1,
                                      sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
