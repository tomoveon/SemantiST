"""Generate, verify and freeze environment stubs (offline; host side).

    python3 -m compiler.intake.stubs.generate --libraries artifacts/intake/libraries \\
        --work artifacts/intake/stubs --library-out compiler/intake/stubs/library

The LLM (:mod:`.llm`, cached) writes a declaration and a category per gap from
the call sites, the signature inferred from them and the manual passages;
:mod:`.verify` compiles every library that needs the stubs in the toolchain
image (``podman``), and the errors attributed to a stub go back to the LLM,
for at most ``--max-rounds`` LLM rounds per stub. A stub that still fails is
replaced by the signature inferred from the call sites when that one passes;
otherwise it is dropped and the dependent POUs stay rejected. The frozen result has one
JSON file per family; ``per-library/<vendor>/<library>.st`` holds the stubs
each library needs, for the intake's ``--stubs`` option.
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .docs import find as find_passages
from .gaps import Gap, collect, inferred_declaration, minimal_declaration
from .llm import BudgetExceeded, ClaudeCLI
from .verify import _needed

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "localhost/semantist:executor-perf"
DIALECTS = {
    "codesys": "CODESYS 2.3/3 and TwinCAT (Beckhoff Tc2_*/Tc3_* system libraries, CODESYS SysLibs)",
    "siemens-scl": "Siemens S7 SCL (TIA Portal / STEP 7 system functions and blocks; the source was "
                   "normalized to IEC syntax: no quotes, no #)",
    "simatic-ax": "Siemens SIMATIC AX (namespaces were flattened: NS.Name became NS__Name)",
    "iec": "IEC 61131-3 as in MATIEC/OpenPLC",
    "sysmac": "Omron Sysmac Studio / KEYENCE KV",
    "gxworks": "Mitsubishi GX Works (device names such as M8002 or D8020Z3 are PLC devices)",
    "br": "B&R Automation Studio",
    "pcworx": "Phoenix Contact PC WORX",
    "plcnext": "Phoenix Contact PLCnext Engineer",
    "control-expert": "Schneider Control Expert",
}
SYSTEM = """You write stub declarations for names that a PLC library uses but that a vendor platform library \
defines, which is not available. The stubs let the library compile with the RuSTy compiler, which accepts \
IEC 61131-3 Structured Text. Rules:
1. Base each declaration on the documentation passages when there are any, otherwise on what you know about \
the vendor library, otherwise on the call sites; say which in "evidence".
2. Every call site shown must compile against the declaration: number and order of arguments, formal \
parameter names, argument types, how the result or the members are used. RuSTy requires every parameter of \
a FUNCTION call to be passed, so declare only the parameters the calls pass.
3. kind is FUNCTION, FUNCTION_BLOCK (instances are called or have members), TYPE (STRUCT, enumeration or \
alias such as TYPE T_MaxString : STRING[255]; END_TYPE) or VAR_GLOBAL (a global variable, or a constant \
written VAR_GLOBAL CONSTANT name : type := value; END_VAR with its documented value).
4. category: "sink" (only logs, reports or writes outside the program), "source" (reads time, hardware, \
files, network or system state), "pure" (computes its outputs from its inputs only), "constant", \
"unknown".
5. Bodies: for sink, source and unknown write no statements, so the result and outputs keep their initial \
(zero) values. For pure, implement the documented behaviour only when the passage or the vendor's \
documented semantics leave no doubt; otherwise write no statements.
6. Plain IEC syntax only: no pragmas, attributes, namespaces, VAR_STAT, VAR_INST or vendor keywords. Use \
types that exist in the library or IEC (STRING[n], ARRAY[a..b] OF T, REF_TO T, POINTER TO T). Do not \
declare names the call sites do not need, and never redeclare another stub's name.
Answer with one entry per requested name."""
LINK_GUIDANCE = ("Some stubs also failed to link: a declaration without a definition leaves an undefined "
                 "symbol. Every function and function block stub must be a plain definition with its own "
                 "body (statements or none): no generic functions (<T : ANY>), no {external} or @EXTERNAL.")
SCHEMA = {
    "type": "object",
    "properties": {"stubs": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["FUNCTION", "FUNCTION_BLOCK", "TYPE", "VAR_GLOBAL"]},
            "category": {"type": "string", "enum": ["sink", "source", "pure", "constant", "unknown"]},
            "evidence": {"type": "string", "enum": ["documentation", "knowledge", "call_sites"]},
            "declaration": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["name", "kind", "category", "evidence", "declaration", "note"],
        "additionalProperties": False}}},
    "required": ["stubs"],
    "additionalProperties": False,
}


def _describe(gap: Gap, passages: list[dict], previous: dict | None = None) -> str:
    data = gap.to_dict()
    lines = [f"### {gap.name}", f"uses: {data['uses']} in {len(gap.libraries)} libraries"]
    lines.append("call sites and enclosing scan logic (source excerpts; omitted sections are marked):")
    lines += [f"- {c['file']}:{c['line']} {c['unit']}: {c['text']}\n"
              f"  persistent_state={json.dumps(c.get('persistent_state', []))}\n"
              f"  clock_io={json.dumps(c.get('clock_io', []))}\n"
              f"  per_scan_updates={json.dumps(c.get('per_scan_updates', []))}\n"
              f"  boundaries={json.dumps(c.get('boundaries', []))}\n"
              f"  outputs={json.dumps(c.get('outputs', []))}\n{c['unit_source']}"
              for c in gap.contexts]
    lines.append("For each call, account for persistent state, clock and IO reads, per-scan updates, "
                 "boundary conditions, and observable outputs. If the source does not establish one of these "
                 "semantics, state that it is unknown; a silent fallback is approximate.")
    if data["formals"]:
        lines.append("formal arguments (name|in/out: types seen): " + json.dumps(data["formals"]))
    if data["positional"]:
        lines.append("positional arguments (position: types seen): " + json.dumps(data["positional"]))
    if data["arity"]:
        lines.append("argument counts seen: " + json.dumps(data["arity"]))
    if data["results"]:
        lines.append("result used as: " + json.dumps(data["results"]))
    if data["members_read"] or data["members_written"]:
        lines.append("members read: " + json.dumps(data["members_read"]) + "; members written: "
                     + json.dumps(data["members_written"]))
    if data["value_types"]:
        lines.append("value used as: " + json.dumps(data["value_types"]))
    lines.append("signature inferred from the call sites: " + (inferred_declaration(gap) or "none"))
    for number, passage in enumerate(passages, 1):
        lines.append(f"documentation passage {number} ({passage['file']}, line {passage['line']}):")
        lines.append(passage["text"])
    if not passages:
        lines.append("documentation passages: none found in the collected manuals")
    if previous:
        lines.append("your previous declaration:")
        lines.append(previous["declaration"])
        lines.append("errors of the pinned compiler with it:")
        lines += [f"- {e}" for e in previous["errors"]]
    return "\n".join(lines)


def stub_fidelity(entry: dict) -> str:
    """Compilation is ABI evidence; only documented behavior with a body is semantic evidence."""
    from .evaluate import has_statements

    if entry.get("evidence") == "documentation" and has_statements(entry["declaration"]):
        return "semantic"
    return "approximate"


def _ask(llm: ClaudeCLI, family: str, gaps: list[Gap], passages, previous: dict | None = None) -> dict[str, dict]:
    head = (f"Dialect: {DIALECTS.get(family, family)}.\n"
            + ("These stubs failed the compile check; return corrected stubs for every name below.\n"
               if previous else "Write stubs for every name below.\n"))
    if previous and any(e.startswith("link:") for p in previous.values() for e in p["errors"]):
        head += (LINK_GUIDANCE + "\n")
    body = "\n\n".join(_describe(g, passages.get((family, g.name.upper()), []),
                                 previous.get(g.key) if previous else None) for g in gaps)
    answer, key = llm.ask(SYSTEM, head + "\n" + body, SCHEMA)
    wanted = {g.name.upper(): g for g in gaps}
    result = {}
    for entry in answer.get("stubs", []):
        gap = wanted.get(str(entry.get("name", "")).upper())
        if gap is not None and entry.get("declaration", "").strip():
            result[gap.key] = {**entry, "name": gap.name, "llm_key": key}
    return result


def _verify(work: Path, round_number: int | str, candidates: dict[str, dict], gaps: dict[str, Gap],
            library_dirs: dict[str, tuple[str, Path]], keys: set[str]) -> dict:
    stubs_by_family: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for key, entry in candidates.items():
        family, name = key.split(":", 1)
        stubs_by_family[family][name] = entry["declaration"]
    names_by_library: dict[str, set[str]] = collections.defaultdict(set)
    for key in keys:
        for library in gaps[key].libraries:
            names_by_library[library].add(key.split(":", 1)[1])
    jobs, family_of = [], {}
    for library, names in sorted(names_by_library.items()):
        family, directory = library_dirs[library]
        jobs.append({"id": library, "dir": str(directory.relative_to(ROOT)), "names": sorted(names),
                     "stubs": stubs_by_family[family]})
        family_of[library] = family
    request = work / f"verify-request-{round_number}.json"
    result = work / f"verify-result-{round_number}.json"
    request.write_text(json.dumps({"jobs": jobs, "family_of": family_of}, ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
    subprocess.run(["podman", "run", "--rm", "-v", f"{ROOT}:/src:Z", "-w", "/src", IMAGE, "python3", "-m",
                    "compiler.intake.stubs.verify", "--request", str(request.relative_to(ROOT)), "--result",
                    str(result.relative_to(ROOT)), "--jobs", "96"], check=True)
    return json.loads(result.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--libraries", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--library-out", type=Path, required=True)
    parser.add_argument("--family", action="append", default=[])
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--budget", type=float, default=40.0)
    parser.add_argument("--offline", action="store_true", help="use cached LLM answers only")
    args = parser.parse_args(argv)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    entries = json.loads((args.libraries / "corpus.json").read_text(encoding="utf-8"))["libraries"]
    library_dirs = {f"{e['vendor']}/{Path(e['library']).stem}":
                    (e["family"], (args.libraries / e["vendor"] / Path(e["library"]).stem).resolve())
                    for e in entries if e["status"] == "done" and (not args.family or e["family"] in args.family)}
    gaps = collect([(family, library, directory) for library, (family, directory) in sorted(library_dirs.items())])
    (work / "gaps.json").write_text(json.dumps([g.to_dict() for g in gaps.values()], ensure_ascii=False, indent=1)
                                    + "\n", encoding="utf-8")
    names: dict[str, set[str]] = collections.defaultdict(set)
    for gap in gaps.values():
        names[gap.family].add(gap.name)
    passages = find_passages(names)
    llm = ClaudeCLI(work / "llm-cache", offline=args.offline, budget_usd=args.budget)

    def batches(keys):
        # System variables declared by VAR_EXTERNAL only are batched apart
        # from the other gaps, whose batches (and cached answers) stay put.
        for external in (False, True):
            by_family = collections.defaultdict(list)
            for key in sorted(keys):
                if bool(gaps[key].uses["external"]) == external:
                    by_family[gaps[key].family].append(gaps[key])
            for family, items in sorted(by_family.items()):
                for i in range(0, len(items), args.batch):
                    yield family, items[i:i + args.batch]

    candidates: dict[str, dict] = {}
    history: dict[str, list[dict]] = collections.defaultdict(list)
    pending = set(gaps)
    status: dict[str, dict] = {}
    llm_blocked: list[dict] = []
    for round_number in range(args.max_rounds):
        previous = {key: {"declaration": candidates[key]["declaration"], "errors": status[key]["errors"]}
                    for key in pending if key in candidates and key in status}
        asked = set()
        work_items = list(batches(pending))
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_ask, llm, family, items, passages, previous if round_number else None)
                       for family, items in work_items]
            for (family, items), future in zip(work_items, futures):  # in batch order: deterministic
                try:
                    answers = future.result()
                except (BudgetExceeded, RuntimeError, KeyError, OSError, subprocess.TimeoutExpired) as error:
                    if isinstance(error, KeyError):
                        reason = "offline_cache_miss"
                    elif isinstance(error, subprocess.TimeoutExpired):
                        reason = "timeout"
                    elif isinstance(error, BudgetExceeded):
                        reason = "budget_exceeded"
                    elif isinstance(error, OSError):
                        reason = "cli_unavailable"
                    else:
                        reason = "failed"
                    llm_blocked.append({"round": round_number + 1, "family": family,
                                        "names": len(items), "status": "blocked", "reason": reason})
                    print(f"[stubs] batch of {len(items)} {family} names: LLM unavailable "
                          f"({reason}); trying deterministic fallback")
                    continue
                for key, entry in answers.items():
                    if not gaps[key].declarable:
                        continue
                    candidates[key] = {**entry, "source": "llm", "rounds": round_number + 1}
                    history[key].append({"round": round_number + 1, "llm_key": entry["llm_key"]})
                asked |= {g.key for g in items}
        # The first round builds every candidate; later rounds only the libraries
        # of the stubs asked again (the final check below covers the rest).
        checked = set(candidates) if round_number == 0 else asked & set(candidates)
        verdict = _verify(work, round_number, candidates, gaps, library_dirs, checked)
        status.update({key: verdict["stubs"].get(key, {"ok": False, "errors": ["not compiled in any library"],
                                                       "libraries": 0}) for key in checked})
        pending = {key for key in asked if not status.get(key, {}).get("ok") and status.get(key, {}).get("libraries")}
        print(f"[stubs] round {round_number + 1}: {sum(1 for s in status.values() if s['ok'])} stubs pass, "
              f"{len(pending)} to repair, LLM calls {llm.calls}, cost {llm.spent_usd:.2f} USD")
        if not pending or llm.spent_usd >= args.budget:
            break
    # A deterministic, silent model is preferable to dropping a compilable
    # target. The build verifier is the ABI gate; the model remains approximate.
    fallback = {}
    for key, gap in gaps.items():
        if status.get(key, {}).get("ok") or not gap.declarable:
            continue
        inferred = inferred_declaration(gap)
        declaration = inferred or minimal_declaration(gap)
        if declaration:
            fallback[key] = {"name": gap.name, "kind": gap.kind, "category": "unknown", "evidence": "call_sites",
                             "declaration": declaration,
                             "note": "silent substitute; ABI checked against call sites, vendor behavior unverified",
                             "source": "inferred" if inferred else "minimal",
                             "rounds": 0}
    if fallback:
        trial = {**{k: v for k, v in candidates.items() if status.get(k, {}).get("ok")}, **fallback}
        verdict = _verify(work, args.max_rounds, trial, gaps, library_dirs, set(fallback))
        for key, entry in fallback.items():
            if verdict["stubs"].get(key, {}).get("ok"):
                candidates[key] = entry
                status[key] = verdict["stubs"][key]
    # The rounds verify every candidate together, failing ones included; the
    # frozen set must pass on its own (a stub may use a dropped stub's type).
    to_check = {k for k, v in candidates.items() if status.get(k, {}).get("ok")}
    for settle in range(args.max_rounds):
        accepted = {k: v for k, v in candidates.items() if status.get(k, {}).get("ok")}
        checked = to_check & set(accepted)
        verdict = _verify(work, f"final-{settle}", accepted, gaps, library_dirs, checked)
        dropped = sorted(k for k in checked if not verdict["stubs"].get(k, {}).get("ok"))
        for key in dropped:
            status[key] = verdict["stubs"].get(key, {"ok": False, "errors": ["not compiled in any library"]})
        print(f"[stubs] final check {settle + 1}: {len(checked) - len(dropped)} of {len(checked)} stubs pass alone, "
              f"{len(dropped)} dropped")
        if not dropped:
            break
        # A dropped stub changes the libraries it was built into: check the stubs there again.
        lost = set().union(*(gaps[k].libraries for k in dropped))
        to_check = {k for k in accepted if k not in dropped and gaps[k].libraries & lost}
    frozen: dict[str, list] = collections.defaultdict(list)
    for key in sorted(candidates):
        if not status.get(key, {}).get("ok"):
            continue
        entry = candidates[key]
        family = key.split(":", 1)[0]
        frozen[family].append({
            "name": entry["name"], "kind": entry["kind"], "category": entry["category"],
            "evidence": entry["evidence"], "source": entry["source"], "rounds": entry.get("rounds", 0),
            "declaration": entry["declaration"].strip() + "\n", "note": entry.get("note", ""),
            "passages": [{k: p[k] for k in ("file", "line", "sha256")}
                         for p in passages.get((family, entry["name"].upper()), [])],
            "llm": history.get(key, []), "verified": "compile",
            "support_status": stub_fidelity(entry), "semantic_fidelity": stub_fidelity(entry),
            "libraries": len(gaps[key].libraries)})
    args.library_out.mkdir(parents=True, exist_ok=True)
    for family, stubs in sorted(frozen.items()):
        (args.library_out / f"{family}.json").write_text(json.dumps(
            {"schema_version": "semantist.environment-stubs/1.0.0", "family": family, "stubs": stubs},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    write_per_library(args.library_out, work / "per-library", library_dirs, gaps)
    summary = {
        "gaps": len(gaps), "gaps_by_family": dict(collections.Counter(g.family for g in gaps.values())),
        "with_passages": sum(1 for g in gaps.values() if passages.get((g.family, g.name.upper()))),
        "stubs": {f: len(s) for f, s in sorted(frozen.items())},
        "by_source": dict(collections.Counter(e["source"] for s in frozen.values() for e in s)),
        "by_category": dict(collections.Counter(e["category"] for s in frozen.values() for e in s)),
        "by_evidence": dict(collections.Counter(e["evidence"] for s in frozen.values() for e in s)),
        "rounds": dict(collections.Counter(e["rounds"] for s in frozen.values() for e in s)),
        "failed": sorted(k for k in gaps if not status.get(k, {}).get("ok")),
        "undeclarable": sorted(k for k, g in gaps.items() if not g.declarable),
        "unsupported": sorted(k for k in gaps if not status.get(k, {}).get("ok")),
        "by_fidelity": dict(collections.Counter(e["semantic_fidelity"] for s in frozen.values() for e in s)),
        "llm": {"calls": llm.calls, "cache_hits": llm.cache_hits, "cost_usd": round(llm.spent_usd, 4),
                "model": llm.model, "effort": llm.effort,
                "status": "blocked" if llm_blocked else "ready",
                "blocked_batches": llm_blocked},
    }
    (work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "failed"}, indent=1))
    return 0


def write_per_library(library_out: Path, out: Path, library_dirs: dict, gaps: dict[str, Gap]) -> None:
    """``out/<vendor>/<library>.st``: the frozen stubs each library needs."""
    frozen = {}
    for path in sorted(library_out.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        frozen[data["family"]] = {s["name"].upper(): s["declaration"] for s in data["stubs"]}
    if out.exists():
        shutil.rmtree(out)  # a library that no longer needs stubs keeps no stale file
    needs: dict[str, set[str]] = collections.defaultdict(set)
    for gap in gaps.values():
        for library in gap.libraries:
            needs[library].add(gap.name.upper())
    for library, names in sorted(needs.items()):
        family = library_dirs[library][0]
        stubs = frozen.get(family, {})
        used = _needed(names, stubs)
        if not used:
            continue
        target = out / f"{library}.st"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("(* Environment stubs (compiler/intake/stubs): approximate, see the stub library. *)\n\n"
                          + "\n\n".join(stubs[n].strip() for n in used) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
