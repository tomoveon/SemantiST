"""Per-POU build evidence for a normalized library.

Three stages, each reported per unit (POU, type, global variable list):

1. **Type check.** The normalized files are compiled with the compatibility
   sources. Units with errors are recorded with their diagnostics and blanked
   (spaces, newlines kept) for the next round, so that the remaining units can
   be checked; diagnostics that only name a blanked unit mark the referring
   unit as ``depends_on_unsupported``. Compiler panics are attributed to a unit
   by a prefix search.
2. **LLVM IR.** The units that type check are compiled to one LLVM module.
3. **Native link.** The module, the compatibility profiles' native sources and
   the RuSTy runtime archive are linked into an executable. Undefined symbols
   are attributed to every POU whose IR (transitively) refers to them.

The blanked "accepted subset" exists only as validation evidence; the
normalized library itself keeps every unit.
"""

from __future__ import annotations

import collections
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import rusty
from .diagnostics import SourceIndex, locate, referenced_name

MAX_ROUNDS = 60
LLVM_BIN = Path(os.environ.get("SEMANTIST_LLVM_BIN", "/usr/lib/llvm-21/bin"))
POU_KINDS = {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "CLASS", "METHOD"}


def blank(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if chars[i] not in "\r\n":
                chars[i] = " "
    return "".join(chars)


@dataclass
class UnitResult:
    file: str
    kind: str
    name: str
    line: int
    status: str = "ok"            # ok | type_error | depends_on_unsupported | compiler_panic | codegen_panic
    ir: str = "not_run"           # generated | failed | not_run
    link: str = "not_run"         # passed | undefined_symbols | object_failed | link_failed | not_run
    diagnostics: list[dict] = field(default_factory=list)
    undefined_symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class BuildEvidence:
    units: list[UnitResult]
    rounds: list[dict]
    ir_path: Path | None
    executable: Path | None
    link_output: str = ""
    undefined_symbols: list[str] = field(default_factory=list)
    configuration_errors: list[dict] = field(default_factory=list)
    # Errors in text outside any unit; those lines are excluded from the build.
    orphan_lines: list[dict] = field(default_factory=list)
    # The files as built: rejected units and declarations blanked (same
    # offsets and lines as the normalized text); empty when no IR was built.
    accepted_texts: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict:
        def count(attribute, value):
            return sum(1 for u in self.units if getattr(u, attribute) == value)
        return {
            "units": len(self.units),
            "type_checked": count("status", "ok"),
            "ir_generated": count("ir", "generated"),
            "linked": count("link", "passed"),
        }


def _culprit_prefix(texts, paths, spans, units, compile_prefix, start):
    """First unit (index >= start) whose presence makes ``compile_prefix``
    true; ``compile_prefix`` gets the files with ``units[k:]`` blanked."""

    def fails(k):
        removed = {name: list(s) for name, s in spans.items()}
        for unit in units[k:]:
            removed[unit[0]].append((unit[1].start, unit[1].end))
        for name, path in paths.items():
            path.write_text(blank(texts[name], removed[name]), encoding="utf-8")
        return compile_prefix()

    # No candidate left: the error comes from the compatibility sources.
    if start >= len(units) or not fails(len(units)):
        return None
    low, high = start, len(units)
    while low + 1 < high:
        middle = (low + high) // 2
        if fails(middle):
            high = middle
        else:
            low = middle
    return high - 1


def validate(texts: dict[str, str], work: Path, compatibility_sources: list[Path], native_inputs: list[str],
             compiler_args: list[str], excluded: dict[tuple[str, str], str] | None = None) -> BuildEvidence:
    """``excluded`` maps (file, unit name) to the reason a unit is unsupported
    before compiling (detected constructs); such units start blanked."""
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    paths = {name: work / name for name in texts}
    indices = {name: SourceIndex(text) for name, text in texts.items()}
    by_path = {path: name for name, path in paths.items()}
    units: list[UnitResult] = []
    unit_refs = []
    for name, index in sorted(indices.items()):
        for unit in index.units:
            result = UnitResult(name, unit.kind, unit.qualified_name, unit.line)
            units.append(result)
            unit_refs.append((name, unit, result))
    by_start = {(name, unit.start): result for name, unit, result in unit_refs}
    spans: dict[str, list[tuple[int, int]]] = {name: [] for name in texts}
    blanked_names: set[str] = set()
    for name, unit, result in unit_refs:
        reason = (excluded or {}).get((name, unit.qualified_name))
        if reason:
            result.status = "unsupported_construct"
            result.diagnostics.append({"code": "UNSUPPORTED", "construct": "unsupported_construct",
                                       "line": unit.line, "message": reason})
            spans[name].append((unit.start, unit.end))
            blanked_names.update(_unit_names(unit, texts[name]))
    rounds = []
    configuration_errors: list[dict] = []
    orphan_lines: list[dict] = []
    ir = work / "library.ll"
    partial_rounds: collections.Counter = collections.Counter()
    panic_start = 0
    generated = None
    for round_number in range(MAX_ROUNDS + len(units)):
        for name, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(blank(texts[name], spans[name]), encoding="utf-8")
        result = rusty.check([*paths.values(), *compatibility_sources], extra=compiler_args)
        failing: list[tuple[str, object]] = []
        orphans: list[tuple[str, tuple[int, int]]] = []
        partial: dict[tuple[str, int], set[tuple[int, int]]] = {}  # global list -> failing declarations
        unlocated = []
        errors = 0
        for diagnostic in result.diagnostics:
            if diagnostic.severity != "error":
                continue
            errors += 1
            name = by_path.get(Path(diagnostic.path).resolve()) if diagnostic.path else None
            if name is None:
                unlocated.append(diagnostic)
                continue
            located = locate(diagnostic, indices[name])
            unit = located.unit
            record = {"code": diagnostic.code, "line": diagnostic.line, "column": diagnostic.column,
                      "construct": located.construct, "message": diagnostic.message.split("\n", 1)[0][:300]}
            if unit is None:
                # Text outside any unit that the normalizer could not handle:
                # excluded line by line and reported, never built.
                line_start = indices[name].line_starts[diagnostic.line - 1] if diagnostic.line else 0
                line_end = texts[name].find("\n", line_start)
                span = (line_start, len(texts[name]) if line_end < 0 else line_end)
                if span not in spans[name]:
                    orphan_lines.append({**record, "file": name})
                    orphans.append((name, span))
                continue
            target = by_start[(name, unit.start)]
            referenced = referenced_name(diagnostic)
            if referenced and referenced.upper() in blanked_names and target.status == "ok":
                target.status = "depends_on_unsupported"
            elif target.status == "ok":
                target.status = "type_error"
            if len(target.diagnostics) < 20:
                target.diagnostics.append(record)
            failing.append((name, unit))
            declaration = _declaration_span(indices[name], unit, located.offset)
            if declaration is not None:
                partial.setdefault((name, unit.start), set()).add(declaration)
        if result.returncode != 0 and errors == 0 and not result.panic:
            configuration_errors.append({"code": "COMPILER_EXIT", "stage": "check",
                                         "returncode": result.returncode,
                                         "message": _scrub(result.output.strip())[-1000:] or
                                         f"compiler exited with status {result.returncode}"})
        if unlocated and not failing and not orphans and not result.panic:
            # Errors without a source position (a RuSTy defect, e.g. E068 in
            # some method bodies): the unit is found by a prefix search.
            codes = {d.code for d in unlocated}
            active = [(name, unit) for name, unit, r in unit_refs if r.status == "ok"]

            def unlocated_error():
                check = rusty.check([*paths.values(), *compatibility_sources], extra=compiler_args)
                return any(d.severity == "error" and not d.path and d.code in codes for d in check.diagnostics)

            culprit_index = _culprit_prefix(texts, paths, spans, active, unlocated_error, 0)
            if culprit_index is None:
                configuration_errors += [d.to_dict() for d in unlocated]
            else:
                name, unit = active[culprit_index]
                target = by_start[(name, unit.start)]
                target.status = "type_error"
                first = unlocated[0]
                target.diagnostics.append({"code": first.code, "line": None, "column": None,
                                           "construct": "unlocated_error",
                                           "message": first.message.split("\n", 1)[0][:300]})
                failing.append((name, unit))
        panic = result.panic
        panic_stage = "check" if panic else None
        if not panic and errors == 0 and not configuration_errors:
            generated = rusty.check([*paths.values(), *compatibility_sources], extra=compiler_args, ir=ir)
            if generated.panic:
                panic, panic_stage = generated.panic, "codegen"
            elif not generated.ok:
                located_any = False
                for d in generated.errors:
                    name = by_path.get(Path(d.path).resolve()) if d.path else None
                    unit = locate(d, indices[name]).unit if name else None
                    if unit is None:
                        configuration_errors.append({**d.to_dict(), "stage": "codegen"})
                        continue
                    target = by_start[(name, unit.start)]
                    target.status = "codegen_error"
                    target.diagnostics.append({"code": d.code, "line": d.line, "column": d.column,
                                               "construct": "codegen_error", "message": _scrub(d.message)[:300]})
                    failing.append((name, unit))
                    located_any = True
                if not generated.errors:
                    # Codegen reports some errors without a location, e.g. a
                    # method returning an interface that was excluded. When the
                    # error names an excluded type, the units that use it
                    # depend on that unit; otherwise the error is reported.
                    missing = _UNKNOWN_TYPE.search(generated.output)
                    users = _users(indices, unit_refs, missing.group(1)) if missing and \
                        missing.group(1).upper() in blanked_names else []
                    # An AST dump naming a variable of an excluded global list
                    # (STRING(STRING_LENGTH) after the list declaring it was
                    # excluded: RuSTy reports nothing at the type check).
                    for dumped in sorted(set(re.findall(r'name: "(\w+)"', generated.output))):
                        if dumped.upper() in blanked_names and not missing:
                            missing = re.match(r"(.*)", f"needs {dumped}, declared in an excluded unit")
                            users += [u for u in _users(indices, unit_refs, dumped) if u not in users]
                    for name, unit, target in users:
                        target.status = "depends_on_unsupported"
                        target.diagnostics.append({"code": "CODEGEN", "construct": "codegen_error",
                                                   "message": missing.group(0)})
                        failing.append((name, unit))
                        located_any = True
                    culprit = None
                    message = _codegen_key(generated.output)
                    isolated = None
                    if not users and re.search(r"[A-Za-z]{3}", message):
                        active = [(name, unit) for name, unit, r in unit_refs if r.status == "ok"]
                        isolated = _exclusion_culprit(texts, paths, spans, active, indices, by_path, message,
                                                      generated.output, compatibility_sources, compiler_args, ir)
                    if isolated is not None:
                        (name, unit), dependents = isolated
                        target = by_start[(name, unit.start)]
                        target.status = "codegen_error"
                        target.diagnostics.append({"code": "CODEGEN", "line": None, "column": None,
                                                   "construct": "unlocated_codegen_error",
                                                   "message": message[:300]})
                        failing.append((name, unit))
                        for dependent_name, dependent in dependents:
                            other = by_start[(dependent_name, dependent.start)]
                            if other.status == "ok":
                                other.status = "depends_on_unsupported"
                                other.diagnostics.append({"code": "CODEGEN", "construct": "cascade:excluded_unit",
                                                          "message": f"needs {unit.qualified_name}, excluded for "
                                                                     f"a code generation error"})
                            failing.append((dependent_name, dependent))
                        located_any = True
                        culprit = True
                    elif not users and re.search(r"[A-Za-z]{3}", message):
                        # Otherwise the unit is found by a prefix search on the
                        # message (a message without words, such as the "}" that
                        # ends a multi-line dump, would match any output).
                        active = [(name, unit) for name, unit, r in unit_refs if r.status == "ok"]
                        culprit = _culprit_prefix(
                            texts, paths, spans, active,
                            lambda: message in rusty.check([*paths.values(), *compatibility_sources],
                                                           extra=compiler_args, ir=ir).output, 0)
                        if culprit is not None:
                            name, unit = active[culprit]
                            target = by_start[(name, unit.start)]
                            target.status = "codegen_error"
                            target.diagnostics.append({"code": "CODEGEN", "line": None, "column": None,
                                                       "construct": "unlocated_codegen_error",
                                                       "message": message[:300]})
                            failing.append((name, unit))
                            located_any = True
                    if not users and culprit is None:
                        configuration_errors.append({"code": "CODEGEN", "stage": "codegen",
                                                     "message": _scrub(generated.output.strip())[-300:]})
                rounds.append({"round": round_number, "stage": "codegen", "errors": len(generated.errors)})
                if not located_any:
                    break
                generated = None
        rounds.append({"round": round_number, "stage": panic_stage or ("type" if errors else "ir"),
                       "errors": errors, "panic": panic})
        if panic:
            active = [(name, unit) for name, unit, r in unit_refs if r.status == "ok"]
            stage_ir = ir if panic_stage == "codegen" else None
            culprit_index = _culprit_prefix(
                texts, paths, spans, active,
                lambda: bool(rusty.check([*paths.values(), *compatibility_sources], extra=compiler_args,
                                         ir=stage_ir).panic),
                panic_start)
            if culprit_index is None:
                configuration_errors.append({"code": "PANIC", "message": panic, "stage": panic_stage})
                break
            name, unit = active[culprit_index]
            target = by_start[(name, unit.start)]
            blocked = sorted(_mentioned(indices[name], unit, blanked_names))
            if blocked:
                # The unit refers to a unit that is already excluded (e.g. a
                # string length constant of a rejected global list), and the
                # pinned compiler panics instead of reporting the reference.
                target.status = "depends_on_unsupported"
                target.diagnostics.append({"code": "PANIC", "message": panic, "stage": panic_stage,
                                           "construct": "panic_after_excluded_dependency",
                                           "excluded": blocked[:10]})
            else:
                target.status = "compiler_panic" if panic_stage == "check" else "codegen_panic"
                target.diagnostics.append({"code": "PANIC", "message": panic, "stage": panic_stage})
            failing.append((name, unit))
            panic_start = culprit_index
        else:
            panic_start = 0
        if generated is not None and generated.ok and not panic:
            break
        if configuration_errors and not failing and not orphans:
            break
        for name, span in orphans:
            spans[name].append(span)
        new = [(name, unit) for name, unit in failing if (unit.start, unit.end) not in spans[name]]
        if not new and not orphans:
            break
        for name, unit in {(n, u.start): (n, u) for n, u in new}.values():
            declarations = partial.get((name, unit.start), set()) - set(spans[name])
            if declarations and partial_rounds[(name, unit.start)] < PARTIAL_LIMIT:
                # A global variable list loses only its failing declarations,
                # so that the other variables stay defined.
                partial_rounds[(name, unit.start)] += 1
                for start, end in sorted(declarations):
                    spans[name].append((start, end))
                    blanked_names.update(_declared_globals(texts[name][start:end]))
                continue
            spans[name].append((unit.start, unit.end))
            blanked_names.update(_unit_names(unit, texts[name]))
    ok_ir = generated is not None and generated.ok and ir.exists()
    evidence = BuildEvidence(units, rounds, ir if ok_ir else None, None,
                             configuration_errors=configuration_errors, orphan_lines=orphan_lines,
                             accepted_texts={name: blank(texts[name], spans[name]) for name in texts} if ok_ir
                             else {})
    for unit in units:
        if unit.status == "ok":
            unit.ir = "generated" if ok_ir else "failed"
    if ok_ir:
        _link(evidence, work, native_inputs)
    for path in paths.values():
        path.unlink(missing_ok=True)
    return evidence


_UNKNOWN_TYPE = re.compile(r"Unknown type: ([\w.]+)")
_PATH = re.compile(r"(?:/[^\s:'\"/]+)+/([^\s:'\"/]+)")


def _scrub(text: str) -> str:
    """Absolute paths reduced to their file name: the build runs in temporary
    directories, and the evidence must not depend on where."""
    return _PATH.sub(r"\1", text)


def _exclusion_culprit(texts, paths, spans, active, indices, by_path, key: str, output: str,
                       compatibility_sources, compiler_args, ir, limit: int = 24):
    """The unit an unlocated code generation error comes from, and the units
    that need it: candidates are the units that mention a name the error
    names (the structure of an LLVM ``insertvalue``, the identifiers of an
    AST dump); a candidate is excluded together with the units that then fail
    to type check, and is the culprit when the error is gone. A plain prefix
    search cannot find it: a prefix whose units call later, blanked units
    stops at the type check and never reaches code generation."""
    names = {n.upper() for n in re.findall(r"insertvalue %([\w.]+)", output) + re.findall(r'name: "(\w+)"', output)}
    if not names:
        return None
    candidates = [(name, unit) for name, unit in active
                  if any(t.is_word and t.upper in names for t in indices[name].significant
                         [indices[name].significant_index(unit.start):indices[name].significant_index(unit.end)])]
    for name, unit in candidates[:limit]:
        removed = {n: list(s) for n, s in spans.items()}
        removed[name].append((unit.start, unit.end))
        dependents = []
        for _ in range(8):
            for n, path in paths.items():
                path.write_text(blank(texts[n], removed[n]), encoding="utf-8")
            checked = rusty.check([*paths.values(), *compatibility_sources], extra=compiler_args)
            failed = []
            for d in checked.errors:
                file = by_path.get(Path(d.path).resolve()) if d.path else None
                if file is None:
                    continue
                located = locate(d, indices[file])
                other = located.unit
                if other is None:
                    continue
                span = _declaration_span(indices[file], other, located.offset) or (other.start, other.end)
                if span not in removed[file] and (other.start, other.end) not in removed[file]:
                    failed.append((file, other, span))
            if checked.panic or not checked.errors or not failed:
                break
            for file, other, span in failed:
                if span not in removed[file] and (other.start, other.end) not in removed[file]:
                    removed[file].append(span)
                    if span == (other.start, other.end):
                        dependents.append((file, other))
        if checked.panic or checked.errors:
            continue
        if key not in _scrub(rusty.check([*paths.values(), *compatibility_sources], extra=compiler_args,
                                         ir=ir).output):
            return (name, unit), dependents
    return None


def _codegen_key(output: str) -> str:
    """A stable text to find an unlocated code generation error by: what an
    LLVM ``error:`` line says after the (temporary) path, otherwise the last
    non-warning line without its location."""
    for line in output.splitlines():
        if "error:" in line and "warning[" not in line:
            return line.rsplit("error:", 1)[1].strip()
    lines = [line for line in output.splitlines()
             if line.strip() and "warning[" not in line and not line.startswith("Hint:")]
    return lines[-1].split(" at:", 1)[0].strip() if lines else ""


PARTIAL_LIMIT = 20
GLOBAL_MODIFIERS = {"CONSTANT", "RETAIN", "PERSISTENT", "NON_RETAIN"}


def _declared_globals(text: str) -> set[str]:
    return {m.group(1).upper() for m in re.finditer(r"(?m)(?:^|;)\s*([A-Za-z_]\w*)\s*(?:,\s*[A-Za-z_]\w*\s*)*"
                                                    r"(?:AT\s+\S+\s*)?:", text)}


def _declaration_span(index: SourceIndex, unit, offset: int) -> tuple[int, int] | None:
    """The declaration ``name : type [:= init];`` of a global variable list
    that contains ``offset``; None for other units or inline structures."""
    if unit.kind != "VAR_GLOBAL":
        return None
    tokens = index.significant
    first, last = index.significant_index(unit.start), index.significant_index(unit.end)
    k = min(index.significant_index(offset), last - 1)
    start = k
    while start > first + 1 and tokens[start - 1].text != ";":
        start -= 1
    while start < k and tokens[start].upper in GLOBAL_MODIFIERS | {"VAR_GLOBAL"}:
        start += 1
    end = k
    while end < last and tokens[end].text != ";":
        end += 1
    if end >= last or start > k or any(tokens[i].upper in {"STRUCT", "END_VAR", "VAR_GLOBAL"}
                                       for i in range(start, end)):
        return None
    return tokens[start].start, tokens[end].end


def _unit_names(unit, text: str = "") -> set[str]:
    """Names by which other units refer to ``unit`` (an action P.A also as A,
    a global variable list by its variables)."""
    names = {unit.name.upper(), unit.qualified_name.upper()} - {""}
    if unit.kind == "VAR_GLOBAL" and text:
        names |= {m.group(1).upper() for m in re.finditer(r"(?m)^\s*([A-Za-z_]\w*)\s*(?:AT\s+\S+\s*)?:",
                                                           text[unit.start:unit.end])}
    if unit.kind == "ACTION":
        names.add(unit.name.rsplit(".", 1)[-1].upper())
    return names


def _mentioned(index: SourceIndex, unit, names: set[str]) -> set[str]:
    """Which of ``names`` the unit's own tokens mention (its own name excluded)."""
    start, end = index.significant_index(unit.start), index.significant_index(unit.end)
    own = _unit_names(unit)
    return {t.upper for t in index.significant[start:end] if t.is_word and t.upper in names} - own


def _users(indices: dict[str, SourceIndex], unit_refs, type_name: str) -> list:
    """Units that still build and mention ``type_name``."""
    wanted = type_name.upper()
    found = []
    for name, unit, result in unit_refs:
        if result.status != "ok":
            continue
        index = indices[name]
        start, end = index.significant_index(unit.start), index.significant_index(unit.end)
        if any(t.is_word and t.upper == wanted for t in index.significant[start:end]):
            found.append((name, unit, result))
    return found


_DEFINE = re.compile(r'^define [^@]*@("(?:[^"\\]|\\.)*"|[\w.$]+)\(', re.M)
_GLOBAL_REF = re.compile(r'@("(?:[^"\\]|\\.)*"|[\w.$]+)')
_UNDEFINED = re.compile(r"undefined (?:reference to|symbol:) [`'‘]?([^'’`\s]+)")


def _symbol(raw: str) -> str:
    return raw[1:-1] if raw.startswith('"') else raw


def reference_graph(ir_text: str) -> dict[str, set[str]]:
    """Global symbols each defined function refers to."""
    graph: dict[str, set[str]] = {}
    current = None
    for line in ir_text.splitlines():
        match = _DEFINE.match(line)
        if match:
            current = _symbol(match.group(1))
            graph[current] = set()
            continue
        if current is not None:
            if line.startswith("}"):
                current = None
                continue
            graph[current].update(_symbol(m) for m in _GLOBAL_REF.findall(line))
    return graph


def internalize(source: Path, output: Path, keep: list[str]) -> subprocess.CompletedProcess:
    """Make every definition except ``keep`` internal, as the fuzz build does
    (compiler/instrumentation/compile.py): standard-library functions compiled
    from ST then never clash with the runtime archive's copies."""
    return subprocess.run([str(LLVM_BIN / "opt"), "-S", "-passes=internalize,globaldce",
                           f"--internalize-public-api-list={','.join(keep)}", str(source), "-o", str(output)],
                          capture_output=True, text=True)


def _link(evidence: BuildEvidence, work: Path, native_inputs: list[str]) -> None:
    clang = str(LLVM_BIN / "clang")
    obj = work / "library.o"
    graph = reference_graph(evidence.ir_path.read_text(encoding="utf-8", errors="replace"))
    names = {u.name.upper() for u in evidence.units if u.ir == "generated"}
    keep = sorted(s for s in graph if s.upper() in names or s.upper().split(".", 1)[0] in names
                  or s.upper().removesuffix("__CTOR") in names)
    public = work / "library.public.ll"
    result = internalize(evidence.ir_path, public, keep)
    if result.returncode == 0:
        evidence.ir_path = public
    main = work / "link_main.c"
    main.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    executable = work / "link-check"
    compile_result = subprocess.run([clang, "-c", "-O0", "-Wno-override-module", str(evidence.ir_path), "-o",
                                     str(obj)], capture_output=True, text=True)
    if compile_result.returncode:
        evidence.link_output = compile_result.stderr[-4000:]
        for unit in evidence.units:
            if unit.ir == "generated":
                unit.link = "object_failed"
        return
    command = [clang, str(main), str(obj), *native_inputs, str(rusty.runtime_archive()), "-ldl", "-lpthread", "-lm",
               "-Wl,--unresolved-symbols=report-all", "-o", str(executable)]
    result = subprocess.run(command, capture_output=True, text=True)
    output = result.stdout + result.stderr
    evidence.link_output = output[-8000:]
    undefined = sorted(set(_UNDEFINED.findall(output)))
    evidence.undefined_symbols = undefined
    bad = set(undefined)

    def closure(symbol: str) -> set[str]:
        seen, stack = set(), [symbol]
        while stack:
            item = stack.pop()
            if item in seen:
                continue
            seen.add(item)
            stack.extend(graph.get(item, ()))
        return seen

    for unit in evidence.units:
        if unit.ir != "generated":
            continue
        if result.returncode == 0:
            unit.link = "passed"
            continue
        roots = [s for s in graph if s.upper() == unit.name.upper() or s.upper().startswith(unit.name.upper() + ".")
                 or s.upper() == "__INIT_" + unit.name.upper()]
        missing = sorted({s for root in roots for s in closure(root)} & bad)
        unit.undefined_symbols = missing
        unit.link = "undefined_symbols" if missing else "link_failed"
    if result.returncode == 0:
        evidence.executable = executable
