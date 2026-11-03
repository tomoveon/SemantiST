"""Compatibility report of one library (design section 4.3).

Syntax intake and behaviour evidence are recorded separately: a POU that type
checks, generates IR and links is *accepted* syntactically; that says nothing
about equal behaviour. Behaviour evidence is graded from what the intake did:

* ``rule_equivalence`` -- only rewrites with an equivalence argument, no
  adapters with assumptions, no time sources;
* ``unverified_environment`` -- additionally uses semantic adapters with stated
  assumptions, the byte pointer arithmetic switch, or host-clock time sources;
* ``needs_platform`` -- external providers are missing (unresolved names);
* ``tested`` -- the library's own unit tests ran on RuSTy (set by
  :mod:`compiler.intake.unittests`).
"""

from __future__ import annotations

import collections

from compiler.parser.st import significant, tokenize_lossless

from . import pragmas, unsupported
from .structure import split_units


def compatibility_report(identity, transformations, verification, dependencies, dialect, config, profiles, texts,
                         specs, primary: str | None = None) -> dict:
    """``primary`` names the library file whose units are reported; units of
    dependency files are built with it but not counted."""
    pragma_items, construct_items = [], []
    for name, text in texts.items():
        tokens = tokenize_lossless(text)
        units = split_units(text, tokens)
        pragma_items += [{**item, "file": name} for item in pragmas.inventory(tokens, units)]
        construct_items += [{**item, "file": name} for item in unsupported.detect(significant(tokens), units)]
    blocking_pragmas = collections.defaultdict(list)
    for item in pragma_items:
        if item["class"] in {"layout", "semantic", "unclassified"} and item["unit"]:
            blocking_pragmas[item["unit"]].append(item)
    blocking_constructs = collections.defaultdict(list)
    for item in construct_items:
        if item["unit"]:
            blocking_constructs[item["unit"]].append(item)
    unresolved = set(dependencies["classifications"]["unresolved"])
    units = []
    for unit in verification["units"]:
        if primary is not None and unit["file"] != primary:
            continue
        reasons = []
        if unit["status"] != "ok":
            first = unit["diagnostics"][0] if unit["diagnostics"] else {}
            reasons.append({"stage": "type_check" if unit["status"] in {"type_error", "depends_on_unsupported"}
                            else unit["status"], "code": first.get("code"), "construct": first.get("construct"),
                            "line": first.get("line"), "message": first.get("message")})
        elif unit["ir"] != "generated":
            reasons.append({"stage": "ir", "message": "LLVM IR was not generated"})
        elif unit["link"] != "passed" and unit["kind"] in {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "CLASS"}:
            message = ("undefined symbols: " + ", ".join(unit["undefined_symbols"]) if
                       unit["link"] == "undefined_symbols" else
                       verification.get("link_output", "").strip()[-300:] or
                       f"native link did not pass ({unit['link']})")
            reasons.append({"stage": "link", "code": unit["link"], "message": message})
        for item in blocking_pragmas.get(unit["name"], []):
            reasons.append({"stage": "pragma", "line": item["line"], "construct": item["key"],
                            "message": f"{item['class']} pragma: {item['reason']}"})
        for item in blocking_constructs.get(unit["name"], []):
            reasons.append({"stage": "construct", "line": item["line"], "construct": item["construct"],
                            "message": item["reason"]})
        units.append({"name": unit["name"], "kind": unit["kind"], "file": unit["file"], "line": unit["line"],
                      "type_check": unit["status"], "ir": unit["ir"], "link": unit["link"],
                      "accepted": not reasons, "reasons": reasons})
    pous = [u for u in units if u["kind"] in {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "CLASS"}]
    accepted = [u for u in pous if u["accepted"]]
    orphans = [o for o in verification.get("orphan_lines", []) if primary is None or o.get("file") == primary]
    if pous and len(accepted) == len(pous) and all(u["accepted"] for u in units) and not orphans:
        syntax_level = "full"
    elif accepted:
        syntax_level = "partial"
    else:
        syntax_level = "none"
    rules_used = transformations["rule_summary"]
    assumptions = []
    for name, profile in profiles["profiles"].items():
        if profile.get("assumptions") and any(
                use["source"] == "profile:" + name for use in dependencies["external_uses"]):
            assumptions.append({"profile": name, "assumptions": profile["assumptions"]})
    unverified = []
    if dependencies["time_sources"]:
        unverified.append("time sources read the host clock (no virtual clock yet); timer boundary behaviour "
                          "not verified against the vendor runtime")
    if "--byte-pointer-arithmetic" in dialect.get("compiler_args", []) and any(
            "POINTER" in t.upper() for t in texts.values()):
        unverified.append("POINTER TO arithmetic in bytes (dialect switch); not compared with a vendor runtime")
    unverified_calls = dialect.get("unverified_calls", {})
    if unverified_calls:
        used = sorted({tok.upper for text in texts.values() for tok, nxt in
                       zip(significant(tokenize_lossless(text)), significant(tokenize_lossless(text))[1:])
                       if tok.is_word and nxt.text == "(" and tok.upper in unverified_calls})
        for name in used:
            unverified.append(f"{name}: {unverified_calls[name]}")
    if dependencies["implicit_check_functions"]:
        unverified.append("implicit check functions are defined but RuSTy never calls them: "
                          + ", ".join(dependencies["implicit_check_functions"]))
    if config.get("assumption"):
        unverified.append("library configuration: " + config["assumption"])
    if unresolved:
        behaviour = "needs_platform"
    elif assumptions or unverified:
        behaviour = "unverified_environment"
    else:
        behaviour = "rule_equivalence"
    pragma_counts = collections.Counter(item["class"] for item in pragma_items)
    return {
        "schema_version": "semantist.compatibility-report/1.0.0",
        "target_identity": identity,
        "syntax": {
            "level": syntax_level,
            "units": len(units),
            "pous": len(pous),
            "pous_accepted": len(accepted),
            "type_checked": sum(1 for u in units if u["type_check"] == "ok"),
            "ir_generated": sum(1 for u in units if u["ir"] == "generated"),
            "linked": sum(1 for u in units if u["link"] == "passed"),
            "unparsed_lines": len(orphans),
        },
        "unparsed_lines": orphans[:50],
        "behaviour": {
            "level": behaviour,
            "adapter_assumptions": assumptions,
            "unverified": unverified,
            "tests": None,
        },
        "rules_applied": rules_used,
        "rule_versions": {rule_id: specs[rule_id].version for rule_id in rules_used if rule_id in specs},
        "representation": {key: dialect.get(key) for key in ("time", "pointer", "string")},
        "pragmas": {"counts": dict(sorted(pragma_counts.items())),
                    "blocking": [i for i in pragma_items if i["class"] in {"layout", "semantic", "unclassified"}]},
        "unsupported_constructs": construct_items,
        "time_sources": dependencies["time_sources"],
        "implicit_check_functions": dependencies["implicit_check_functions"],
        "dependencies": dependencies["gate"],
        "units": units,
    }
