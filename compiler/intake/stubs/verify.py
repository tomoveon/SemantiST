"""Build check of candidate stubs in every library that needs them.

Runs in the toolchain image. For each library the normalized sources of its
intake result are built with the intake's own per-unit engine
(:func:`compiler.intake.library.build_units`: type check, exclusion of the
units that fail, LLVM IR, native link) with its compatibility profiles and the
stubs of the names it uses (plus the stubs those stubs mention). Units the
intake rejected for a reason other than a missing name start blanked, so that
only the missing names decide. An error is attributed to a stub when

* it is located in the stub file (a stub that does not type check, or one
  whose use of a type fails once the failing units are excluded);
* it is a diagnostic of a library unit on a line that names the stub, a
  member of an instance of the stub's type, or a variable declared with the
  stub's type;
* the final link has an undefined symbol of the stub (a declaration without
  a definition: a generic function, a function block without body).

A stub passes when no library it was built into attributes an error to it;
the result also records how many units that failed only on missing names are
accepted now.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import re
import subprocess
import tempfile
from pathlib import Path

from compiler.parser.st import significant, tokenize_lossless

from ..build import blank
from ..library import build_units, compile_natives, resolve_profiles
from ..structure import split_units

GAP_CONSTRUCTS = {"unresolved_reference", "unknown_type"}
_REFERENCE = re.compile(r"(?:Could not resolve reference to|Unknown type:?)\s+([\w.]+)")
_DECLARATION = re.compile(r"\b([A-Za-z_]\w*)\s*(?:,\s*[A-Za-z_]\w*\s*)*:\s*(?:ARRAY\s*\[[^\]]*\]\s*OF\s+)?"
                          r"(?:REF_TO\s+|POINTER\s+TO\s+|REFERENCE\s+TO\s+)?([A-Za-z_]\w*)", re.I)


def stub_names(declaration: str) -> set[str]:
    return {t.upper for t in significant(tokenize_lossless(declaration)) if t.is_word}


def _needed(names: set[str], stubs: dict[str, str]) -> list[str]:
    """``names`` and, transitively, the stubs their declarations mention."""
    result, todo = set(), [n for n in names if n in stubs]
    while todo:
        name = todo.pop()
        if name in result:
            continue
        result.add(name)
        todo += [m for m in stub_names(stubs[name]) & stubs.keys() if m not in result]
    return sorted(result)


def _gap_only(unit: dict) -> bool:
    return bool(unit["reasons"]) and all(r.get("construct") in GAP_CONSTRUCTS for r in unit["reasons"])


def symbol_belongs_to(symbol: str, name: str) -> bool:
    """Whether a linker symbol is the stub's own: the function, a generic
    instance (``NAME____T____U``), a method (``NAME.m``), or its constructor
    or initializer."""
    return re.search(rf"(?:^|__)(?:INIT_|USER_INIT_|VTABLE_)?{re.escape(name)}(?:__|\.|$)", symbol.upper()) is not None


def blame(message: str, text_line: str, used: list[str], stub_types: set[str],
          instances: dict[str, str]) -> set[str]:
    """The stubs a diagnostic of a library line is attributed to.

    A stub is blamed when the diagnostic is about it: it names the stub (or,
    for a type or function block, mentions the stub's name or an unresolved
    member type, VOID), is an unresolved reference to it or to a member of one
    of its instances, or is on a line that calls it or uses a member of an
    instance of its type. A variable of a stub type that is merely passed to
    some other call does not make an error of that call the stub's."""
    words = {w.upper() for w in re.findall(r"[A-Za-z_]\w*", text_line)}
    referenced = _REFERENCE.search(message)
    if referenced:
        # An unresolved name decides: the stub of that name, or the stub
        # type of the instance it is a member of; another missing name on
        # the same line is not this stub's fault.
        target = referenced.group(1).rsplit(".", 1)[-1].upper()
        owners = {instances.get(m.group(1).upper()) for m in
                  re.finditer(rf"\b([A-Za-z_]\w*)\s*\.\s*{re.escape(target)}\b", text_line, re.I)}
        return ({target} & set(used)) | (owners & stub_types)
    shouted = message.upper()
    mentioned = {n for n in used if re.search(rf"(?<![A-Z0-9_]){re.escape(n)}(?![A-Z0-9_])", shouted)}
    unresolved_type = "VOID" in shouted
    blamed = {n for n in used if n in words and (n not in stub_types or n in mentioned or unresolved_type)}
    for word in words:
        stub = instances.get(word)
        if stub not in stub_types:
            continue
        used_as_member = re.search(rf"\b{re.escape(word)}\s*[.\[]", text_line, re.I) is not None
        if stub in mentioned or unresolved_type or used_as_member:
            blamed.add(stub)
    return blamed


def check_library(job: dict) -> dict:
    directory = Path(job["dir"])
    stubs: dict[str, str] = job["stubs"]
    used = _needed(set(job["names"]), stubs)
    if not used:
        return {"id": job["id"], "stubs": {}, "usable": False}
    report = json.loads((directory / "compatibility-report.json").read_text(encoding="utf-8"))
    meta = json.loads((directory / "compatibility.json").read_text(encoding="utf-8"))
    profiles = resolve_profiles(meta.get("profiles", []))
    rejected = {(u["file"], u["line"]) for u in report["units"] if not u["accepted"] and not _gap_only(u)}
    gap_units = {(u["file"], u["line"]) for u in report["units"] if not u["accepted"] and _gap_only(u)}
    originals: dict[str, str] = {}
    texts: dict[str, str] = {}
    normalized_dir = directory / "normalized"
    for source in sorted(normalized_dir.rglob("*.st")):
        text = source.read_text(encoding="utf-8")
        name = source.relative_to(normalized_dir).as_posix()
        originals[name] = text
        spans = [(u.start, u.end) for u in split_units(text) if (name, u.line) in rejected]
        texts[name] = blank(text, spans)
    with tempfile.TemporaryDirectory(prefix="semantist-stub-verify-") as work:
        work = Path(work)
        pieces, owner, line = [], [], 1  # owner: (first line, last line, stub name)
        for name in used:
            declaration = stubs[name].strip("\n")
            count = declaration.count("\n") + 1
            owner.append((line, line + count - 1, name))
            pieces.append(declaration)
            line += count + 1
        stub_path = work / "semantist_stubs.st"
        stub_path.write_text("\n\n".join(pieces) + "\n", encoding="utf-8")
        try:
            natives = compile_natives(profiles["native_sources"], work / "native")
            evidence = build_units(texts, work / "build", [*profiles["st_sources"], stub_path], natives,
                                   list(meta["compiler"]["args"]))
        except (OSError, subprocess.CalledProcessError) as error:
            reason = f"native compatibility build failed: {error}"
            return {"id": job["id"], "usable": False, "link_checked": False,
                    "stubs": {name: [reason] for name in used},
                    "unattributed_errors": [reason], "gap_units": len(gap_units),
                    "gap_units_accepted": 0}
    stub_types = {name for name in used if re.match(r"\s*(FUNCTION_BLOCK|TYPE)\b", stubs[name], re.I)}
    instances = {file: {m.group(1).upper(): m.group(2).upper() for m in _DECLARATION.finditer(text)}
                 for file, text in originals.items()}
    errors: dict[str, list[str]] = {name: [] for name in used}
    unattributed: list[str] = []
    unusable = False
    for error in evidence.configuration_errors:
        located = error.get("path") and Path(error["path"]).name == stub_path.name and error.get("line")
        if located:
            for first, last, name in owner:
                if first <= error["line"] <= last:
                    message = str(error.get("message", "")).split("\n", 1)[0][:240]
                    errors[name].append(f"in the stub, line {error['line'] - first + 1}: {error.get('code')} {message}")
        else:
            message = str(error.get("message", "")).split("\n", 1)[0][:240]
            unattributed.append(f"{error.get('code', 'BUILD_ERROR')}: {message}")
            unusable = True
    for unit in evidence.units:
        if unit.status == "ok" or unit.file not in originals:
            continue
        # Gap-only units are the explicitly permitted approximate fallback;
        # all other unowned failures must remain visible at library scope.
        if (unit.file, unit.line) in gap_units:
            continue
        lines = originals[unit.file].splitlines()
        blamed_any = False
        for diagnostic in unit.diagnostics:
            number = diagnostic.get("line")
            if not number or number > len(lines):
                continue
            message = str(diagnostic.get("message", "")).split("\n", 1)[0][:240]
            blamed = blame(message, lines[number - 1], used, stub_types, instances[unit.file])
            for name in blamed:
                blamed_any = True
                errors[name].append(f"at a call site ({lines[number - 1].strip()[:120]}): "
                                    f"{diagnostic.get('code')} {message}")
        if not blamed_any:
            unattributed.append(f"unit {unit.file}:{unit.line} status={unit.status}")
            unusable = True
    for symbol in evidence.undefined_symbols:
        for name in used:
            if symbol_belongs_to(symbol, name):
                errors[name].append(f"link: undefined symbol {symbol} (a declaration without a definition)")
    for unit in evidence.units:
        if unit.status == "ok" and unit.ir == "generated" and unit.link != "passed":
            attributed = any(symbol_belongs_to(symbol, name) for symbol in unit.undefined_symbols for name in used)
            if not attributed:
                unattributed.append(f"unit {unit.file}:{unit.line} link={unit.link}")
                unusable = True
    accepted = {(u.file, u.line) for u in evidence.units
                if u.status == "ok" and u.ir == "generated" and u.link == "passed"}
    return {"id": job["id"], "usable": not unusable, "link_checked": evidence.ir_path is not None,
            "stubs": {name: sorted(set(errs))[:6] for name, errs in errors.items()},
            "unattributed_errors": sorted(set(unattributed))[:12],
            "gap_units": len(gap_units), "gap_units_accepted": len(gap_units & accepted)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=32)
    args = parser.parse_args(argv)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    # Largest libraries first so that the pool stays busy to the end.
    jobs = sorted(request["jobs"], key=lambda j: -sum(p.stat().st_size for p in
                                                      (Path(j["dir"]) / "normalized").rglob("*.st")))
    with multiprocessing.Pool(args.jobs) as pool:
        libraries = pool.map(check_library, jobs, chunksize=1)
    verdict: dict[str, dict] = {}
    for library in sorted(libraries, key=lambda item: item["id"]):
        for name, errs in library["stubs"].items():
            entry = verdict.setdefault(f"{request['family_of'][library['id']]}:{name}",
                                       {"errors": [], "libraries": 0, "unusable_libraries": 0,
                                        "link_checked_libraries": 0,
                                        "unattributed_errors": []})
            entry["unattributed_errors"] = sorted(set(entry["unattributed_errors"]) |
                                                    set(library.get("unattributed_errors", [])))[:12]
            if library["usable"]:
                entry["libraries"] += 1
                entry["link_checked_libraries"] += int(library["link_checked"])
                entry["errors"] = sorted(set(entry["errors"]) | set(errs))[:6]
            else:
                entry["unusable_libraries"] += 1
    for entry in verdict.values():
        entry["ok"] = entry["libraries"] > 0 and not entry["errors"]
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps({"stubs": verdict, "libraries": sorted(libraries, key=lambda l: l["id"])},
                                      ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
