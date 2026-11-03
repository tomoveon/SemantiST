"""Intake of one library: normalize, validate per POU, write the evidence.

Output directory layout (consumed by ``compiler/toolchain/compatibility`` and
the fuzzing pipeline through ``compatibility.json``)::

    normalized/<file>.st            normalized library sources
    normalized/<file>.st.sourcemap.json
    transformations.json            rules applied (id, version, trigger, spans)
    compatibility.json              project manifest (sources, profiles, compiler args)
    dependency-report.json          external symbols and their providers
    verification.json               per-POU type check, IR and link results
    compatibility-report.json       syntax intake level and behaviour evidence level
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from compiler.parser.st import significant, tokenize_lossless

from . import rusty, unsupported
from .build import LLVM_BIN, BuildEvidence, blank, validate
from .closure import closure, config_for
from .dependencies import dependency_report
from .normalize.engine import normalize, write_outputs
from .normalize.profiles import dialect_identity, load_dialect
from .normalize.rules import RULES, rules_for
from .report import compatibility_report
from .structure import split_units

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "compiler" / "toolchain" / "compatibility" / "profiles"


def resolve_profiles(names: list[str]) -> dict:
    st_sources, native_sources, rebind, provided = [], [], {}, {}
    for name in names:
        root = PROFILES / name
        profile = json.loads((root / "profile.json").read_text(encoding="utf-8"))
        st_sources.extend(root / item for item in profile.get("st_sources", []))
        native_sources.extend(root / item for item in profile.get("native_sources", []))
        rebind.update(profile.get("rebind", {}))
        provided[name] = profile
    return {"st_sources": st_sources, "native_sources": native_sources, "rebind": rebind, "profiles": provided}


def compile_natives(sources: list[Path], work: Path) -> list[str]:
    work.mkdir(parents=True, exist_ok=True)
    objects = []
    for source in sources:
        digest = hashlib.sha256(str(source).encode()).hexdigest()[:12]
        output = work / f"{source.stem}-{digest}.o"
        subprocess.run([str(LLVM_BIN / "clang"), "-g", "-O2", "-fno-omit-frame-pointer", "-c", str(source), "-o",
                        str(output)], check=True)
        objects.append(str(output))
    return objects


def build_units(texts: dict[str, str], work: Path, compatibility_sources: list[Path], natives: list[str],
                compiler_args: list[str]) -> BuildEvidence:
    """Per-unit type check, IR and link; units with a detected unsupported
    construct start excluded."""
    excluded = {}
    for name, text in texts.items():
        tokens = tokenize_lossless(text)
        for item in unsupported.detect(significant(tokens), split_units(text, tokens)):
            if item["unit"]:
                excluded.setdefault((name, item["unit"]), f"{item['construct']}: {item['reason']}")
    return validate(texts, work, compatibility_sources, natives, compiler_args, excluded)


def write_accepted(evidence: BuildEvidence, output: Path) -> list[str]:
    """``accepted/<file>``: the files as built, rejected units blanked (same
    lines as the normalized files), so that the accepted POUs compile and
    link from the manifest; none when no IR was built."""
    paths = []
    for name, text in sorted(evidence.accepted_texts.items()):
        path = output / "accepted" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(f"accepted/{name}")
    return paths


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def supplement_text(config: dict, primary: str) -> tuple[str, str] | None:
    """Declarations missing from ``primary`` taken from the configured sibling export."""
    item = config.get("supplement")
    if not item:
        return None
    path = CORPUS_OUTPUTS() / item["vendor"] / ".st文件" / item["library"]
    text = path.read_text(encoding="utf-8")
    present = {u.qualified_name.upper() for u in split_units(primary) if u.name}
    declared = set()
    for token_line in primary.splitlines():
        match = re.match(r"^\s*([A-Za-z_]\w*)\s*(?:AT\s+\S+\s*)?:", token_line)
        if match:
            declared.add(match.group(1).upper())
    pieces = []
    for unit in split_units(text):
        if unit.kind not in set(item.get("kinds", ["TYPE", "VAR_GLOBAL"])):
            continue
        body = text[unit.start:unit.end]
        if unit.kind == "VAR_GLOBAL":
            names = {m.group(1).upper() for m in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*:", body)}
            if names and names <= declared:
                continue
        elif unit.qualified_name.upper() in present:
            continue
        pieces.append(body)
    header = (f"(* Declarations missing from {primary and 'the library'}, taken from "
              f"{item['vendor']}/.st文件/{item['library']}: {item['reason']} *)\n")
    return f"{Path(item['library']).stem}.supplement.st", header + "\n\n".join(pieces) + "\n"


def project_namespaces(source: Path, files: dict[str, str]) -> dict[str, set[str]]:
    """Library namespace (TwinCAT project name) -> names its file defines."""
    namespaces: dict[str, set[str]] = {}
    for name, text in files.items():
        units = split_units(text)
        match = re.match(r"^xml_.+?__([^_].*)\.st$", name)
        if match:
            namespaces[match.group(1).upper()] = {u.name.upper() for u in units if u.name}
        # Global variable lists qualify their variables (GVL.x): the extractor
        # names each list in the marker comment in front of it.
        for unit in units:
            if unit.kind != "VAR_GLOBAL" or not unit.marker:
                continue
            list_name = unit.marker.rsplit("/", 1)[-1].removeprefix("Global variable list: ").strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", list_name):
                continue
            body = text[unit.start:unit.end]
            variables = {m.group(1).upper() for m in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*(?:AT\s+\S+\s*)?:", body)}
            namespaces.setdefault(list_name.upper(), set()).update(variables)
    return namespaces


def CORPUS_OUTPUTS() -> Path:
    return Path(os.environ.get("SEMANTIST_EXTRACT_OUTPUTS", ROOT / "artifacts" / "intake" / "extract" / "outputs"))


def intake(source: Path, vendor, output: Path, extra_sources: list[Path] = ()) -> dict:
    """Run the intake of ``source`` (one extracted library file) into ``output``.

    The library is normalized and built together with its dependency closure
    (:mod:`.closure`) and an optional declaration supplement; only the units of
    ``source`` itself are counted in the compatibility report.
    """
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    dialect = load_dialect(vendor.dialect)
    config = config_for(source.name)
    profile_names = [*dialect.get("compatibility_profiles", []), *config.get("profiles", [])]
    profiles = resolve_profiles(profile_names)
    compatibility_sources = [*profiles["st_sources"], *extra_sources]
    compiler_args = list(dialect.get("compiler_args", []))
    files = {source.name: source.read_text(encoding="utf-8")}
    dependency_files = []
    for path, reason in closure(source):
        name = path.name if path.name not in files else f"{path.parent.parent.name}__{path.name}"
        text = path.read_text(encoding="utf-8")
        # A dependency is used as a library: its own application entry points
        # (PROGRAM MAIN, CONFIGURATION) are left out of this build.
        entries = [u for u in split_units(text) if u.kind in {"PROGRAM", "CONFIGURATION"}]
        files[name] = blank(text, [(u.start, u.end) for u in entries])
        dependency_files.append({"file": name, "origin": str(path.relative_to(path.parents[2])),
                                 "sha256": sha256(text), "reason": reason,
                                 "omitted_entry_points": [u.qualified_name for u in entries]})
    supplement = supplement_text(config, files[source.name])
    if supplement:
        files[supplement[0]] = supplement[1]
        dependency_files.append({"file": supplement[0], "origin": config["supplement"]["library"],
                                 "sha256": sha256(supplement[1]), "reason": config["supplement"]["reason"]})
    rules = rules_for(dialect)
    config = dict(config)
    config["_namespaces"] = project_namespaces(source, files)
    result = normalize(files, dialect, config, rules, output / "work" / "normalize", compatibility_sources,
                       compiler_args, profiles["rebind"])
    identity = {
        "source": {"file": source.name, "sha256": sha256(files[source.name])},
        "dependency_files": dependency_files,
        "dialect": dialect_identity(dialect),
        "library_config": {"id": config.get("id"), "version": config.get("version")} if config else None,
        "compatibility_profiles": profile_names,
        "rules": {rule.spec.id: rule.spec.version for rule in rules},
        "toolchain": rusty.toolchain_identity(),
    }
    record = write_outputs(result, output / "normalized", {"target_identity": identity})
    shutil.move(str(output / "normalized" / "transformations.json"), output / "transformations.json")
    natives = compile_natives(profiles["native_sources"], output / "work" / "native")
    evidence = build_units(result.texts(), output / "work" / "build", compatibility_sources, natives, compiler_args)
    accepted = write_accepted(evidence, output)
    verification = {
        "schema_version": "semantist.intake-verification/2.0.0",
        "target_identity": identity,
        "summary": evidence.summary(),
        "rounds": evidence.rounds,
        "configuration_errors": evidence.configuration_errors,
        "orphan_lines": evidence.orphan_lines,
        "undefined_symbols": evidence.undefined_symbols,
        "link_output": evidence.link_output,
        "units": [unit.to_dict() for unit in evidence.units],
    }
    (output / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=1,
                                                         sort_keys=True) + "\n", encoding="utf-8")
    dependencies = dependency_report(result.texts(), evidence, compatibility_sources, profiles["profiles"])
    (output / "dependency-report.json").write_text(json.dumps(dependencies, ensure_ascii=False, indent=1,
                                                              sort_keys=True) + "\n", encoding="utf-8")

    def relative(path: Path) -> str:
        return os.path.relpath(path, output)

    normalized = [f"normalized/{name}" for name in files]
    compat = [relative(p) for p in compatibility_sources]
    manifest = {
        "schema_version": "semantist.compatibility/1.1.0",
        "name": source.stem,
        "dialect": dialect["id"],
        "profiles": profile_names,
        "library": {"sources": [*normalized, *compat], "normalized_sources": normalized,
                    "accepted_sources": accepted, "compatibility_sources": compat},
        "native": {"sources": [relative(p) for p in profiles["native_sources"]], "objects": [], "libraries": [],
                   "compile_args": [], "link_args": []},
        "compiler": {"args": compiler_args},
        "execution": {"string_bytes": "ascii" if dialect.get("string", {}).get("encoding") == "single-byte"
                      else "any"},
        "toolchain": {"stdlib_glob": str(rusty.stdlib_dir() / "*.st")},
    }
    report = compatibility_report(identity, record, verification, dependencies, dialect, config, profiles,
                                  result.texts(), {rule_id: RULES[rule_id].spec for rule_id in RULES},
                                  primary=source.name)
    fidelity = ("semantic" if report["syntax"]["level"] == "full"
                and report["behaviour"]["level"] == "rule_equivalence" and not extra_sources
                else "approximate")
    manifest["support_status"] = fidelity if report["syntax"]["level"] != "none" else "unsupported"
    manifest["semantic_fidelity"] = fidelity
    manifest["stub_used"] = [relative(path) for path in extra_sources]
    manifest["model_scope"] = {"dialect": dialect["id"], "profiles": profile_names,
                               "behaviour_evidence": report["behaviour"]["level"]}
    (output / "compatibility.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
    (output / "compatibility-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1,
                                                                 sort_keys=True) + "\n", encoding="utf-8")
    shutil.rmtree(output / "work", ignore_errors=True)
    return report


def unit_names(text: str) -> list[str]:
    return [u.qualified_name for u in split_units(text) if u.name]
