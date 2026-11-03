"""Dependency report of a normalized library (layer L4).

Every external name the compiler or linker could not resolve, and every name
the library takes from a provider outside itself, is classified in the fixed
resolution order:

1. compiler intrinsic, 2. RuSTy IEC ST standard library, 3. RuSTy runtime
archive, 4. the library itself, 5. compatibility profiles (semantic adapters,
declarations, environment models), otherwise ``unresolved``.

``unresolved`` blocks intake of the units that need it. Time sources (TIME(),
TON/TOF/TP, RTC and vendor clock functions such as OSCAT's T_PLC_MS) are
flagged separately: the pinned standard library reads the host clock, and a
later virtual-clock profile must be able to provide same-named implementations
at link time, so the intake never binds them to anything else.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from compiler.parser.st import significant, tokenize_lossless

from . import rusty
from .symbols import defined_in_text, defined_names

COMPILER_INTRINSICS = {"ADR", "REF", "SIZEOF", "SEL", "MUX", "MOVE", "LEN", "MIN", "MAX", "LIMIT"}
TIME_SOURCES = {
    "TIME": "reads the host clock (RuSTy stdlib TIME())",
    "TON": "timer FB in libiec61131std, uses the host clock",
    "TOF": "timer FB in libiec61131std, uses the host clock",
    "TP": "timer FB in libiec61131std, uses the host clock",
    "TON_TIME": "timer FB in libiec61131std, uses the host clock",
    "TOF_TIME": "timer FB in libiec61131std, uses the host clock",
    "TP_TIME": "timer FB in libiec61131std, uses the host clock",
    "RTC": "real-time clock FB in libiec61131std, uses the host clock",
    "T_PLC_MS": "OSCAT PLC millisecond clock (calls TIME())",
    "T_PLC_US": "OSCAT PLC microsecond clock (calls TIME())",
    "SYSTIMEGETMS": "CODESYS SysTime library",
    "SYSTIMEGETUS": "CODESYS SysTime library",
    "SYSTIMERTCGET": "CODESYS SysTimeRtc library",
    "SYSRTCGETTIME": "CODESYS SysRtc library",
    "GETSYSTEMTIME": "TwinCAT system clock",
    "F_GETSYSTEMTIME": "TwinCAT system clock",
}
IMPLICIT_CHECKS = {"CHECKBOUNDS", "CHECKDIVINT", "CHECKDIVDINT", "CHECKDIVREAL", "CHECKDIVLREAL",
                   "CHECKRANGESIGNED", "CHECKRANGEUNSIGNED", "CHECKLRANGESIGNED", "CHECKLRANGEUNSIGNED",
                   "CHECKPOINTER"}


@functools.lru_cache(maxsize=4)
def archive_symbols(path: str) -> frozenset[str]:
    if not Path(path).exists():
        return frozenset()
    result = subprocess.run(["nm", "-g", "--defined-only", path], text=True, capture_output=True, check=False)
    symbols = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-2] in {"T", "D", "B", "R", "W", "V"}:
            symbols.add(fields[-1].upper())
    return frozenset(symbols)


@functools.lru_cache(maxsize=1)
def stdlib_names() -> frozenset[str]:
    return frozenset(defined_names(rusty.stdlib_sources()))


TIME_SOURCE_TYPES = {"TON", "TOF", "TP", "TON_TIME", "TOF_TIME", "TP_TIME", "RTC"}


def used_words(texts: dict[str, str]) -> dict[str, list[tuple[str, int, str]]]:
    """Words used as callee (``NAME(``) or declared type (``: NAME``), with locations and role."""
    found: dict[str, list[tuple[str, int, str]]] = {}
    for name, text in texts.items():
        tokens = significant(tokenize_lossless(text))
        for previous, token, following in zip([None, *tokens], tokens, [*tokens[1:], None]):
            if not token.is_word:
                continue
            if following is not None and following.text == "(":
                role = "call"
            elif previous is not None and previous.text == ":" and following is not None and \
                    following.text in {";", ":=", "("}:
                role = "type"
            else:
                continue
            found.setdefault(token.upper, []).append((name, token.line, role))
    return found


def is_time_source(word: str, places) -> bool:
    if word not in TIME_SOURCES:
        return False
    if word in TIME_SOURCE_TYPES:
        return any(role in {"type", "call"} for _, _, role in places)
    return any(role == "call" for _, _, role in places)


def dependency_report(texts: dict[str, str], evidence, compatibility_sources: list[Path], profiles: dict) -> dict:
    library = set()
    for text in texts.values():
        library |= defined_in_text(text)
    adapters: dict[str, str] = {}
    for profile_name, profile in profiles.items():
        for name in profile.get("provides", []):
            adapters[name.upper()] = profile_name
    compatibility = defined_names(compatibility_sources)
    runtime = archive_symbols(str(rusty.runtime_archive()))
    standard = stdlib_names()

    def resolve(name: str) -> tuple[str, str]:
        key = name.upper()
        if key in COMPILER_INTRINSICS:
            return "compiler_intrinsic", "provided"
        if key in standard:
            return "rusty_iec_st_library", "provided"
        if key in runtime:
            return "libiec61131std.a", "provided"
        if key in library:
            return "input_library", "provided"
        if key in adapters:
            kind = profiles[adapters[key]].get("kind", "semantic_adapter")
            return "profile:" + adapters[key], kind
        if key in compatibility:
            return "compatibility_sources", "declaration"
        return "none", "unresolved"

    dependencies: dict[str, dict] = {}
    for unit in evidence.units:
        for diagnostic in unit.diagnostics:
            if diagnostic.get("code") not in {"E048", "E052"} or diagnostic.get("construct") == "cascade:excluded_unit":
                continue
            message = diagnostic.get("message", "")
            name = message.rsplit(" ", 1)[-1].strip(".")
            entry = dependencies.setdefault(name.upper(), {"name": name, "kind": diagnostic["code"],
                                                           "units": []})
            if unit.name not in entry["units"]:
                entry["units"].append(unit.name)
    for symbol in evidence.undefined_symbols:
        entry = dependencies.setdefault(symbol.upper(), {"name": symbol, "kind": "linker_undefined", "units": []})
        for unit in evidence.units:
            if symbol in unit.undefined_symbols and unit.name not in entry["units"]:
                entry["units"].append(unit.name)
    for entry in dependencies.values():
        entry["source"], entry["classification"] = resolve(entry["name"])
        if entry["kind"] == "linker_undefined" and entry["classification"] != "unresolved":
            # Declared but not linked: the provider is missing at link time.
            entry["classification"] = "unresolved"
            entry["source"] = "declared_without_link_provider"
        entry["units"].sort()
    used = used_words(texts)
    # Uses of names provided by compatibility profiles (adapters, models). Names
    # nobody provides are only reported when the compiler or linker says so.
    external_uses = []
    for word, places in sorted(used.items()):
        source, classification = resolve(word)
        if source.startswith("profile:") or source == "compatibility_sources":
            external_uses.append({"name": word, "source": source, "classification": classification,
                                  "uses": len(places), "first_use": {"file": places[0][0], "line": places[0][1]}})
    time_sources = [
        {"name": word, "uses": len(places), "first_use": {"file": places[0][0], "line": places[0][1]},
         "provider": resolve(word)[0], "note": TIME_SOURCES[word], "replaceable_at_link": True}
        for word, places in sorted(used.items())
        if is_time_source(word, places) and resolve(word)[0] != "input_library"
    ]
    library_time_functions = sorted(w for w in TIME_SOURCES if w in library)
    implicit = sorted(name for name in library if name in IMPLICIT_CHECKS)
    grouped = {"declaration": [], "semantic_adapter": [], "environment_model": [], "unresolved": []}
    for entry in sorted(dependencies.values(), key=lambda e: e["name"].upper()):
        if entry["classification"] in grouped:
            grouped[entry["classification"]].append(entry["name"])
    for use in external_uses:
        if use["classification"] in grouped and use["name"] not in grouped[use["classification"]]:
            grouped[use["classification"]].append(use["name"])
    return {
        "schema_version": "semantist.dependency-report/2.0.0",
        "resolution_order": ["compiler_intrinsic", "rusty_iec_st_library", "libiec61131std.a", "input_library",
                             "compatibility_profiles", "unresolved"],
        "dependencies": sorted(dependencies.values(), key=lambda e: (e["classification"], e["name"].upper())),
        "external_uses": external_uses,
        "classifications": {k: sorted(v) for k, v in grouped.items()},
        "time_sources": time_sources,
        "library_time_functions": library_time_functions,
        "implicit_check_functions": implicit,
        "gate": {"unresolved_count": len(grouped["unresolved"]),
                 "status": "blocked" if grouped["unresolved"] else "resolved"},
    }
