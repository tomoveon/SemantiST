"""Diagnostic-driven normalization loop.

Each round compiles the current texts together with the library's
compatibility sources, offers every error diagnostic to the dialect's rules in
priority order, and applies the non-overlapping edits the rules propose. Once
the project type checks, located code generation errors are offered the same
way. The loop stops when the project type checks and no rule takes a code
generation error, or when no rule accepts any remaining error; those errors
are reported as unsupported constructs with file, line, column, construct
class and reason. Nothing is deleted or stubbed to make a
file compile.

The result is deterministic: rules are tried in a fixed order, diagnostics are
visited by position, and edits are applied from the end of a file backwards.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import rusty
from ..build import blank
from ..diagnostics import SourceIndex, locate
from ..symbols import defined_names
from .base import Proposal, RuleContext
from .representation import CONVERSION_REBIND, rebind_edits
from .sourcemap import EditableSource, line_map

MAX_ROUNDS = 400
SCHEMA = "semantist.transformations/2.0.0"


@dataclass
class NormalizationResult:
    sources: dict[str, EditableSource]
    transformations: list[dict]
    remaining: list[dict]
    rounds: list[dict]
    panic: str | None = None
    renames: dict[str, str] = field(default_factory=dict)

    @property
    def type_checks(self) -> bool:
        return not self.remaining and self.panic is None

    def texts(self) -> dict[str, str]:
        return {name: source.text for name, source in self.sources.items()}


def _known_names(texts: dict[str, str], extra: list[Path]) -> set[str]:
    names: set[str] = set()
    for text in [*texts.values(), *(p.read_text(encoding="utf-8", errors="replace") for p in extra)]:
        for token in SourceIndex(text).tokens:
            if token.is_word:
                names.add(token.upper)
    return names


def _unparsed_failure(result: rusty.CompileResult, stage: str) -> dict | None:
    """Keep a failed compiler process visible even when its output is not a diagnostic."""
    if result.returncode == 0 or result.errors or result.panic:
        return None
    return {"file": None, "line": 0, "column": 0, "code": "COMPILER_EXIT",
            "construct": "compiler_failure", "unit": None, "unit_kind": None,
            "stage": stage, "returncode": result.returncode,
            "message": result.output.strip()[-1000:] or f"compiler exited with status {result.returncode}",
            "reason": f"{stage} exited nonzero without a parsed error diagnostic"}


def normalize(files: dict[str, str], dialect: dict, library: dict, rules: list, work: Path,
              compatibility_sources: list[Path] = (), compiler_args: list[str] = (),
              rebind: dict[str, str] | None = None) -> NormalizationResult:
    """Normalize ``files`` (name -> original text) until they type check.

    ``rebind`` (from the dialect's compatibility profiles) is applied first,
    see :mod:`.representation`.
    """
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    sources = {name: EditableSource(text) for name, text in files.items()}
    transformations: list[dict] = []
    for name, source in sources.items():
        for start, end, replacement in reversed(rebind_edits(source.text, rebind or {})):
            transformations.append({
                "rule_id": CONVERSION_REBIND.id, "rule_version": CONVERSION_REBIND.version,
                "level": CONVERSION_REBIND.level, "round": None, "file": name, "trigger": None,
                "original": {"start": start, "end": end, "line": source.original.count("\n", 0, start) + 1,
                             "text": source.original[start:end]},
                "replacement": replacement, "moved_to_original": None, "note": "",
            })
            source.replace(start, end, replacement, CONVERSION_REBIND.id)
    transformations.sort(key=lambda item: (item["file"], item["original"]["start"]))
    paths = {name: work / name for name in files}
    known = _known_names(files, [*compatibility_sources, *rusty.stdlib_sources()])
    known |= {name.upper() for name in (rebind or {}).values()}
    standard = defined_names(rusty.stdlib_sources()) | {n.upper() for n in dialect.get("builtin_names", [])}
    rounds: list[dict] = []
    renames: dict[str, str] = {}
    applied_keys: set[tuple] = set()
    # Units with errors no rule handles, keyed by (file, original start offset):
    # they are blanked for later rounds so the remaining units reach the type
    # checker (RuSTy stops after the parse stage while any parse error exists).
    masked: dict[tuple[str, int], list[dict]] = {}
    remaining: list[dict] = []
    panic = None
    panic_search_start = 0
    by_path = {path: name for name, path in paths.items()}
    for round_number in range(MAX_ROUNDS):
        texts = {name: source.text for name, source in sources.items()}
        indices = {name: SourceIndex(text) for name, text in texts.items()}
        mask_spans = {name: [(u.start, u.end) for u in indices[name].units
                             if (name, sources[name].to_original(u.start)) in masked] for name in texts}
        for name, text in texts.items():
            paths[name].parent.mkdir(parents=True, exist_ok=True)
            paths[name].write_text(blank(text, mask_spans[name]), encoding="utf-8")
        result = rusty.check([*paths.values(), *compatibility_sources], extra=list(compiler_args))
        located = []
        configuration_errors = []
        for diagnostic in result.diagnostics:
            if diagnostic.severity != "error":
                continue
            name = by_path.get(Path(diagnostic.path).resolve()) if diagnostic.path else None
            if name is None:
                configuration_errors.append(diagnostic)
                continue
            located.append((name, locate(diagnostic, indices[name])))
        process_failure = _unparsed_failure(result, "check")
        if process_failure:
            configuration_errors.append(process_failure)
        codegen = False
        if not located and not configuration_errors and result.panic is None:
            # The project type checks. Some constructs fail only in code
            # generation; their located errors are offered to the rules once
            # per round as well. A zero exit without fresh IR is still a
            # failed codegen probe.
            codegen_ir = work / "codegen.ll"
            codegen_ir.unlink(missing_ok=True)
            generated = rusty.check([*paths.values(), *compatibility_sources], extra=list(compiler_args),
                                    ir=codegen_ir)
            codegen = True
            for diagnostic in generated.errors:
                name = by_path.get(Path(diagnostic.path).resolve()) if diagnostic.path else None
                if name is not None and diagnostic.line:
                    located.append((name, locate(diagnostic, indices[name])))
                else:
                    configuration_errors.append(diagnostic)
            process_failure = _unparsed_failure(generated, "codegen")
            if process_failure:
                configuration_errors.append(process_failure)
            if generated.ok and not codegen_ir.is_file():
                configuration_errors.append({"file": None, "line": 0, "column": 0,
                                             "code": "CODEGEN_MISSING_IR", "construct": "compiler_failure",
                                             "unit": None, "unit_kind": None, "stage": "codegen",
                                             "message": "compiler exited successfully without producing LLVM IR",
                                             "reason": "codegen output is missing"})
            if generated.panic:
                panic = generated.panic
        located.sort(key=lambda item: (item[0], item[1].offset, item[1].diagnostic.code))
        rounds.append({"round": round_number, "errors": len(located) + len(configuration_errors),
                       "panic": result.panic or (generated.panic if codegen else None),
                       "check_returncode": result.returncode,
                       **({"codegen_returncode": generated.returncode} if codegen else {}),
                       "masked_units": len(masked),
                       **({"stage": "codegen"} if codegen else {})})
        panic = result.panic or (generated.panic if codegen else None)
        if not located and not configuration_errors and panic is None:
            remaining = []
            break
        context = RuleContext(texts, indices, dialect, library, known, standard)
        proposals: list[tuple[Proposal, object, str]] = []
        keys: set[tuple] = set()
        for name, item in located:
            for rule in rules:
                if item.diagnostic.code not in rule.spec.codes:
                    continue
                proposal = rule.propose(context, name, item)
                if proposal is None:
                    continue
                if proposal.key not in keys and proposal.key not in applied_keys:
                    keys.add(proposal.key)
                    proposals.append((proposal, item, name))
                break
        remaining = [_unsupported(name, item, indices[name]) for name, item in located]
        remaining += [d if isinstance(d, dict) else
                      {"file": None, "line": d.line, "column": d.column, "code": d.code,
                       "construct": "configuration", "message": d.message.split("\n", 1)[0],
                       "unit": None, "unit_kind": None,
                       "stage": "codegen" if codegen else "check",
                       "reason": "diagnostic outside the normalized files (compatibility sources)"}
                      for d in configuration_errors]
        if not proposals:
            if codegen:
                break
            if configuration_errors:
                break
            new_masks = {}
            if result.panic:
                active = [(name, unit) for name in sorted(texts) for unit in indices[name].units
                          if (unit.start, unit.end) not in mask_spans[name]]
                culprit = _panic_culprit(texts, paths, mask_spans, active, compatibility_sources, compiler_args,
                                         panic_search_start)
                if culprit is None:
                    remaining.append({"file": None, "line": 0, "column": 0, "code": "PANIC",
                                      "construct": "compiler_panic:" + result.panic, "message": result.panic,
                                      "unit": None, "unit_kind": None, "reason": "pinned RuSTy panics"})
                    break
                panic_search_start = culprit
                name, unit = active[culprit]
                new_masks[(name, sources[name].to_original(unit.start))] = [{
                    "file": name, "line": unit.line, "column": 0, "code": "PANIC",
                    "construct": "compiler_panic:" + result.panic, "message": result.panic,
                    "unit": unit.qualified_name, "unit_kind": unit.kind,
                    "reason": "the pinned RuSTy panics on this unit"}]
            else:
                panic_search_start = 0
                for (name, item), record in zip(located, remaining):
                    if item.unit is None:
                        continue
                    key = (name, sources[name].to_original(item.unit.start))
                    new_masks.setdefault(key, []).append(record)
            new_masks = {k: v for k, v in new_masks.items() if k not in masked}
            if not new_masks:
                break
            masked.update(new_masks)
            continue
        panic_search_start = 0
        accepted = _non_overlapping(proposals)
        for proposal, item, name in accepted:
            applied_keys.add(proposal.key)
            renames.update(proposal.renames)
            for edit in proposal.edits:
                source = sources[edit.file]
                original_start = source.to_original(edit.start)
                original_end = source.to_original(edit.end) if edit.end > edit.start else original_start
                transformations.append({
                    "rule_id": proposal.rule.id,
                    "rule_version": proposal.rule.version,
                    "level": proposal.rule.level,
                    "round": round_number,
                    "file": edit.file,
                    "trigger": {"file": name, "code": item.diagnostic.code, "line": item.diagnostic.line,
                                "column": item.diagnostic.column,
                                "message": item.diagnostic.message.split("\n", 1)[0][:200]},
                    "original": {"start": original_start, "end": original_end,
                                 "line": source.original.count("\n", 0, original_start) + 1,
                                 "text": source.original[original_start:original_end]},
                    "replacement": edit.replacement if edit.move_to is None else None,
                    "moved_to_original": source.to_original(edit.move_to) if edit.move_to is not None else None,
                    "note": proposal.note,
                })
        for name in sources:
            owned = [(e, p.rule.id) for p, _, _ in accepted for e in p.edits if e.file == name]
            moves = [(e, rule_id) for e, rule_id in owned if e.move_to is not None]
            if moves:
                # Moves have disjoint affected ranges (see _non_overlapping); applied
                # from the end of the file, each leaves the coordinates of the
                # earlier ones unchanged.
                for edit, rule_id in sorted(moves, key=lambda item: _move_range(item[0]), reverse=True):
                    sources[name].move(edit.start, edit.end, edit.move_to, rule_id)
                continue
            for edit, rule_id in sorted(owned, key=lambda item: (item[0].start, item[0].end), reverse=True):
                sources[name].replace(edit.start, edit.end, edit.replacement, rule_id)
    else:
        remaining.append({"file": None, "line": 0, "column": 0, "code": "ROUNDS", "construct": "round_limit",
                          "message": f"no fixpoint after {MAX_ROUNDS} rounds", "unit": None, "unit_kind": None,
                          "reason": "normalization did not converge"})
    # Errors of masked units are part of the result: those units are unsupported.
    remaining = [record for key in sorted(masked) for record in masked[key]] + remaining
    for path in paths.values():
        path.unlink(missing_ok=True)
    (work / "codegen.ll").unlink(missing_ok=True)
    return NormalizationResult(sources, transformations, remaining, rounds, panic, renames)


def _panic_culprit(texts, paths, mask_spans, active, compatibility_sources, compiler_args, start):
    """Index in ``active`` of the first unit whose presence makes the check panic."""

    def panics(k: int) -> bool:
        removed = {name: list(spans) for name, spans in mask_spans.items()}
        for name, unit in active[k:]:
            removed[name].append((unit.start, unit.end))
        for name, path in paths.items():
            path.write_text(blank(texts[name], removed[name]), encoding="utf-8")
        return bool(rusty.check([*paths.values(), *compatibility_sources], extra=list(compiler_args)).panic)

    if not active or not panics(len(active)):
        return None
    low, high = start, len(active)
    while low + 1 < high:
        middle = (low + high) // 2
        if panics(middle):
            high = middle
        else:
            low = middle
    return high - 1


def _move_range(edit) -> tuple[int, int]:
    """Text whose coordinates a move changes: the span and the way to its destination."""
    return min(edit.start, edit.move_to), max(edit.end, edit.move_to)


def _non_overlapping(proposals):
    """Drop proposals whose edits overlap an earlier proposal's edits.

    A file either gets replacements or moves in one round. Several moves are
    accepted when their affected ranges are disjoint and do not touch."""
    taken: dict[str, list[tuple[int, int]]] = {}
    moving: dict[str, list[tuple[int, int]]] = {}
    accepted = []
    for proposal, item, name in proposals:
        files = {e.file for e in proposal.edits}
        has_move = any(e.move_to is not None for e in proposal.edits)
        if has_move:
            if len(proposal.edits) != 1:
                raise ValueError(f"{proposal.rule.id}: a move must be the proposal's only edit")
            edit = proposal.edits[0]
            low, high = _move_range(edit)
            if taken.get(edit.file) or any(not (high < s or e < low) for s, e in moving.get(edit.file, [])):
                continue
            moving.setdefault(edit.file, []).append((low, high))
            accepted.append((proposal, item, name))
            continue
        if files & set(moving):
            continue
        spans = [(e.file, e.start, e.end) for e in proposal.edits]
        clash = any(not (end <= s or start >= e) or (start == end == s)
                    for file, start, end in spans for s, e in taken.get(file, []))
        if clash:
            continue
        for file, start, end in spans:
            taken.setdefault(file, []).append((start, end))
        accepted.append((proposal, item, name))
    return accepted


def _unsupported(name: str, item, index: SourceIndex) -> dict:
    unit = item.unit
    return {
        "file": name,
        "line": item.diagnostic.line,
        "column": item.diagnostic.column,
        "code": item.diagnostic.code,
        "construct": item.construct,
        "message": item.diagnostic.message.split("\n", 1)[0][:300],
        "unit": unit.qualified_name if unit else None,
        "unit_kind": unit.kind if unit else None,
        "reason": "no rule of the dialect profile handles this diagnostic",
    }


def write_outputs(result: NormalizationResult, output: Path, metadata: dict) -> dict:
    """Write normalized files, SourceMaps and transformations.json."""
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, source in sorted(result.sources.items()):
        text = source.text
        (output / name).parent.mkdir(parents=True, exist_ok=True)
        (output / name).write_text(text, encoding="utf-8")
        sourcemap = {
            "schema_version": "semantist.sourcemap/1.0.0",
            "file": name,
            "original_sha256": hashlib.sha256(source.original.encode("utf-8")).hexdigest(),
            "normalized_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "segments": [s for s in source.segments() if s["rule"] is not None],
            "line_map": line_map(source),
        }
        (output / (name + ".sourcemap.json")).write_text(json.dumps(sourcemap, sort_keys=True) + "\n",
                                                          encoding="utf-8")
        files[name] = {"original_sha256": sourcemap["original_sha256"],
                       "normalized_sha256": sourcemap["normalized_sha256"]}
    counts: dict[str, int] = {}
    for item in result.transformations:
        counts[item["rule_id"]] = counts.get(item["rule_id"], 0) + 1
    record = {
        "schema_version": SCHEMA,
        **metadata,
        "files": files,
        "rounds": result.rounds,
        "transformations": result.transformations,
        "rule_summary": dict(sorted(counts.items())),
        "renames": dict(sorted(result.renames.items())),
        "remaining_errors": result.remaining,
        "compiler_panic": result.panic,
    }
    (output / "transformations.json").write_text(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True)
                                                 + "\n", encoding="utf-8")
    return record
