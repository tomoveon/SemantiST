"""Missing names of the intake results and their deterministic signatures.

A *gap* is a name that the dependency report lists as unresolved and that the
normalized sources use as a call target, a type or a global value. Formal
parameter names of calls to a missing function (``F(IN1 := x)``), member
names (``a.b``) and names that are only spelled in comments are not gaps.

For every gap the call sites give a signature: the types of the arguments
(from the declarations of the variables passed, or the kind of a literal), the
formal names, the type a result is assigned to or the boolean context it is
used in, and the members read and written on instances of a missing type. An
element nobody can determine stays ``None``; :func:`inferred_declaration`
then returns ``None`` and the stub needs another source.
"""

from __future__ import annotations

import bisect
import collections
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from compiler.parser.st import RUSTY_KEYWORDS, LosslessToken, significant, tokenize_lossless

from ..structure import split_units

VAR_BLOCKS = {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR_GLOBAL", "VAR_STAT", "VAR_INST",
              "VAR_EXTERNAL"}
# Generic type classes of IEC 61131-3 (and S7's ANY): built into the compiler, not declarable.
GENERIC_TYPES = {"ANY", "ANY_NUM", "ANY_INT", "ANY_REAL", "ANY_BIT", "ANY_STRING", "ANY_DATE", "ANY_DURATION",
                 "ANY_ELEMENTARY", "ANY_MAGNITUDE", "ANY_CHAR", "ANY_UNSIGNED", "ANY_SIGNED"}
BOOLEAN_BEFORE = {"IF", "ELSIF", "WHILE", "UNTIL", "AND", "OR", "XOR", "NOT", "AND_THEN", "OR_ELSE"}
BOOLEAN_AFTER = {"THEN", "DO", "AND", "OR", "XOR", "AND_THEN", "OR_ELSE"}
STATEMENT_START = {";", "THEN", "ELSE", "DO", "REPEAT", "END_VAR", ":"}
MAX_CONTEXTS = 4


def _semantic_context(source: str, call_text: str) -> dict[str, list[str]]:
    """Extract bounded, reviewable environment clues for a missing call.

    This is deliberately evidence collection, not a behavior model.  The
    original unit excerpt remains alongside these clues so an LLM or reviewer
    can inspect the exact source when the patterns are ambiguous.
    """
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    persistent: list[str] = []
    updates: list[str] = []
    boundaries: list[str] = []
    outputs: list[str] = []
    clock_io: list[str] = []
    in_persistent = False
    persistent_blocks = {"VAR_STAT", "VAR_INST", "VAR_RETAIN", "VAR_PERSISTENT", "VAR_GLOBAL"}
    for line in lines:
        upper = line.upper()
        head = upper.split()[0] if upper.split() else ""
        if head in persistent_blocks:
            in_persistent = True
            continue
        if head == "END_VAR":
            in_persistent = False
            continue
        if in_persistent and ":" in line and not line.startswith("("):
            persistent.append(line[:240])
        if ":=" in line:
            updates.append(line[:240])
            if re.search(r"\b(VAR_OUTPUT|OUT|RESULT|RETURN)\b", upper) or re.search(r"\b[A-Z_][A-Z0-9_]*\s*:=", upper):
                outputs.append(line[:240])
        if re.search(r"(?:>=|<=|<>|=|>|<|\bMOD\b|\bLIMIT\b|\bMIN\b|\bMAX\b|\[[^]]+\])", upper):
            boundaries.append(line[:240])
        if re.search(r"\b(?:TIME|LTIME|DATE|TOD|DT|TON|TOF|TP|R_TRIG|F_TRIG|CLOCK|TICK|SCAN|CYCLE|NOW)\b|%[IQMW]", upper):
            clock_io.append(line[:240])
        if re.search(r"\b(?:VAR_OUTPUT|VAR_IN_OUT)\b", upper):
            outputs.append(line[:240])
    def unique(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))[:12]
    return {
        "persistent_state": unique(persistent),
        "clock_io": unique(clock_io),
        "per_scan_updates": unique(updates),
        "boundaries": unique(boundaries),
        "outputs": unique(outputs),
        "call_point": [call_text[:240]],
    }


def _unit_excerpt(text: str, start: int, end: int, call: int) -> tuple[str, bool]:
    """Bound a source excerpt while retaining declarations, the call, and the unit tail."""
    source = text[start:end]
    if len(source) <= 8000:
        return source, False
    center = call - start
    low = max(0, center - 1500)
    high = min(len(source), center + 2500)
    return source[:2000] + "\n(* ... omitted source ... *)\n" + source[low:high] + \
        "\n(* ... omitted source ... *)\n" + source[-2000:], True


@dataclass
class Gap:
    family: str
    name: str
    uses: collections.Counter = field(default_factory=collections.Counter)  # call / type / value / instance
    libraries: set[str] = field(default_factory=set)
    contexts: list[dict] = field(default_factory=list)
    formals: dict[str, collections.Counter] = field(default_factory=lambda: collections.defaultdict(collections.Counter))
    positional: dict[int, collections.Counter] = field(default_factory=lambda: collections.defaultdict(collections.Counter))
    arity: collections.Counter = field(default_factory=collections.Counter)
    results: collections.Counter = field(default_factory=collections.Counter)
    members_read: dict[str, collections.Counter] = field(default_factory=lambda: collections.defaultdict(collections.Counter))
    members_written: dict[str, collections.Counter] = field(default_factory=lambda: collections.defaultdict(collections.Counter))
    value_types: collections.Counter = field(default_factory=collections.Counter)
    # False for a keyword (TYPE, ANY) or a compiler-internal name (__SYSTEM):
    # no declaration can provide it, so it is never verified or frozen.
    declarable: bool = True

    @property
    def key(self) -> str:
        return f"{self.family}:{self.name.upper()}"

    @property
    def kind(self) -> str:
        if self.uses["type"]:
            return "FUNCTION_BLOCK" if self.uses["instance_call"] else "TYPE"
        if self.uses["call"]:
            return "FUNCTION"
        return "VAR_GLOBAL"

    def to_dict(self) -> dict:
        def counters(d):
            return {k: dict(v.most_common()) for k, v in sorted(d.items(), key=lambda item: str(item[0]))}
        return {"family": self.family, "name": self.name, "kind": self.kind, "uses": dict(self.uses),
                "libraries": sorted(self.libraries), "contexts": self.contexts,
                "formals": counters(self.formals), "positional": counters(self.positional),
                "arity": dict(self.arity), "results": dict(self.results.most_common()),
                "members_read": counters(self.members_read), "members_written": counters(self.members_written),
                "value_types": dict(self.value_types.most_common())}


def literal_type(token: LosslessToken) -> str | None:
    text = token.text
    upper = token.upper
    if upper in {"TRUE", "FALSE"}:
        return "BOOL"
    if text.startswith("'"):
        return "STRING"
    if text.startswith('"'):
        return "WSTRING"
    match = re.match(r"^([A-Za-z_]+)#", text)
    if match:
        prefix = match.group(1).upper()
        return {"T": "TIME", "TIME": "TIME", "LT": "LTIME", "LTIME": "LTIME", "D": "DATE", "DATE": "DATE",
                "DT": "DT", "TOD": "TOD"}.get(prefix, prefix)
    if re.fullmatch(r"\d[\d_]*\.\d[\d_]*([eE][+-]?\d+)?|\d[\d_]*[eE][+-]?\d+", text):
        return "REAL"
    if re.fullmatch(r"\d[\d_]*|(2|8|16)#[0-9A-Fa-f_]+", text):
        return "DINT"
    return None


class UnitScope:
    """Declared variable types of one top-level unit (members included)."""

    def __init__(self, text: str, tokens: list[LosslessToken], first: int, last: int):
        self.types: dict[str, str] = {}
        self.external: set[str] = set()  # names declared in VAR_EXTERNAL blocks
        i = first
        while i < last:
            if tokens[i].is_word and tokens[i].upper in VAR_BLOCKS:
                in_external = tokens[i].upper == "VAR_EXTERNAL"
                i += 1
                start_of_declaration = True
                while i < last and tokens[i].upper != "END_VAR":
                    if start_of_declaration and tokens[i].kind in {"identifier", "keyword"}:
                        names = [tokens[i].upper]
                        j = i + 1
                        while j + 1 < last and tokens[j].text == "," and tokens[j + 1].kind in {"identifier", "keyword"}:
                            names.append(tokens[j + 1].upper)
                            j += 2
                        if j < last and tokens[j].upper == "AT":
                            # The address (%MW1.44) is several tokens: skip to the type colon.
                            while j < last and tokens[j].text not in {":", ";"}:
                                j += 1
                        if j < last and tokens[j].text == ":":
                            k = j + 1
                            while k < last and tokens[k].text not in {";", ":="}:
                                k += 1
                            declared = text[tokens[j + 1].start:tokens[k - 1].end] if k > j + 1 else ""
                            if tokens[j + 1].upper == "STRUCT":
                                declared = ""  # an inline structure: no type name a stub could use
                            for name in names:
                                self.types.setdefault(name, " ".join(declared.split()))
                                if in_external:
                                    self.external.add(name)
                            i = k
                        start_of_declaration = False
                        continue
                    if tokens[i].text == ";":
                        start_of_declaration = True
                    i += 1
            i += 1

    def expression_type(self, tokens: list[LosslessToken], start: int, end: int) -> str | None:
        if end - start == 1:
            token = tokens[start]
            if token.kind == "identifier" or token.kind == "keyword":
                return self.types.get(token.upper) or literal_type(token) or None
            return literal_type(token)
        if end - start == 2 and tokens[start].text == "-":
            return literal_type(tokens[start + 1])
        return None


def _statement_lhs(tokens, k: int, scope: UnitScope) -> str | None:
    """Type of ``v`` in ``v := NAME(...)`` (``k`` is NAME's index)."""
    if k >= 2 and tokens[k - 1].text == ":=" and tokens[k - 2].kind in {"identifier", "keyword"} and \
            (k < 3 or tokens[k - 3].text in STATEMENT_START or tokens[k - 3].upper in STATEMENT_START):
        return scope.types.get(tokens[k - 2].upper)
    return None


def _boolean_context(tokens, k: int, end: int) -> bool:
    before = tokens[k - 1].upper if k else ""
    after = tokens[end].upper if end < len(tokens) else ""
    return before in BOOLEAN_BEFORE or after in BOOLEAN_AFTER


def _arguments(tokens, opening: int):
    """Top-level argument spans of the call whose '(' is at ``opening``, and the ')' index."""
    depth, spans, start, j = 0, [], opening + 1, opening
    while j < len(tokens):
        text = tokens[j].text
        if text in {"(", "["}:
            depth += 1
        elif text in {")", "]"}:
            depth -= 1
            if depth == 0:
                if j > start:
                    spans.append((start, j))
                return spans, j
        elif text == "," and depth == 1:
            spans.append((start, j))
            start = j + 1
        j += 1
    return spans, len(tokens) - 1


def _record_call(gap: Gap, tokens, k: int, scope: UnitScope) -> int:
    spans, closing = _arguments(tokens, k + 1)
    gap.arity[len(spans)] += 1
    for position, (a, b) in enumerate(spans):
        if b - a >= 3 and tokens[a + 1].text in {":=", "=>"} and tokens[a].kind in {"identifier", "keyword"}:
            direction = "out" if tokens[a + 1].text == "=>" else "in"
            gap.formals[f"{tokens[a].text}|{direction}"][scope.expression_type(tokens, a + 2, b) or "?"] += 1
        else:
            gap.positional[position][scope.expression_type(tokens, a, b) or "?"] += 1
    result = _statement_lhs(tokens, k, scope)
    if result:
        gap.results[result] += 1
    elif _boolean_context(tokens, k, closing + 1):
        gap.results["BOOL"] += 1
    elif k and (tokens[k - 1].text in STATEMENT_START or tokens[k - 1].upper in STATEMENT_START) and \
            closing + 1 < len(tokens) and tokens[closing + 1].text == ";":
        gap.results["<statement>"] += 1
    return closing


HEADERS = {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "METHOD", "PROPERTY", "ACTION", "TYPE", "INTERFACE", "CLASS",
           "PROPERTY_GET", "PROPERTY_SET", "DATA_BLOCK", "ORGANIZATION_BLOCK"}
MODIFIERS = {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL", "ABSTRACT", "FINAL", "OVERRIDE"}


def declared_names(tokens: list[LosslessToken]) -> set[str]:
    """Every name the text declares: variables of any block, POUs, members,
    types and enumeration values. RuSTy reports such names as unresolved when
    their declaring unit was excluded; they are not platform gaps."""
    names: set[str] = set()
    in_block = False
    in_external = False
    start_of_declaration = False
    for i, token in enumerate(tokens[:-1]):
        upper = token.upper if token.is_word else token.text
        if token.is_word and (upper in VAR_BLOCKS or upper == "STRUCT"):
            in_block, start_of_declaration = True, True
            in_external = upper == "VAR_EXTERNAL"
            continue
        if upper in {"END_VAR", "END_STRUCT"}:
            in_block = in_external = False
            continue
        if in_block:
            if start_of_declaration and token.kind in {"identifier", "keyword"} and tokens[i + 1].text in {":", ","}:
                # VAR_EXTERNAL only refers to a global defined elsewhere: when
                # nothing defines it, it is a platform name (a system variable).
                if not in_external:
                    names.add(upper)
                start_of_declaration = tokens[i + 1].text == ","
            elif token.text == ";":
                start_of_declaration = True
            elif token.text != ",":
                start_of_declaration = False
            continue
        if token.is_word and upper in HEADERS:
            j = i + 1
            while j < len(tokens) and tokens[j].upper in MODIFIERS:
                j += 1
            if j < len(tokens) and tokens[j].kind in {"identifier", "keyword"}:
                names.add(tokens[j].upper)
                if upper == "TYPE" and j + 3 < len(tokens) and tokens[j + 1].text == ":" and tokens[j + 2].text == "(":
                    k = j + 3
                    while k < len(tokens) and tokens[k].text != ")":
                        if tokens[k].kind in {"identifier", "keyword"} and tokens[k - 1].text in {"(", ","}:
                            names.add(tokens[k].upper)
                        k += 1
    return names


def collect(library_dirs: list[tuple[str, str, Path]]) -> dict[str, Gap]:
    """Gaps of the given intake results: (family, library id, intake directory)."""
    gaps: dict[str, Gap] = {}
    for family, library, directory in library_dirs:
        dependencies = json.loads((directory / "dependency-report.json").read_text(encoding="utf-8"))
        unresolved = {name.upper(): name for name in dependencies["classifications"]["unresolved"]}
        if not unresolved:
            continue
        normalized_dir = directory / "normalized"
        sources = {path: path.read_text(encoding="utf-8") for path in sorted(normalized_dir.rglob("*.st"))}
        for text in sources.values():
            for name in declared_names(significant(tokenize_lossless(text))):
                unresolved.pop(name, None)
        if not unresolved:
            continue
        for path in sources:
            text = sources[path]
            lossless = tokenize_lossless(text)
            tokens = significant(lossless)
            starts = [t.start for t in tokens]
            for unit in split_units(text, lossless):
                first = _index(starts, unit.start)
                last = _index(starts, unit.end)
                scope = None
                instances: dict[str, str] = {}
                for k in range(first, last):
                    token = tokens[k]
                    if not token.is_word or token.upper not in unresolved or tokens[k - 1].text == ".":
                        continue
                    following = tokens[k + 1].text if k + 1 < len(tokens) else ""
                    if following in {":=", "=>"} and tokens[k - 1].text in {"(", ","}:
                        continue  # a formal parameter name of a call
                    scope = scope or UnitScope(text, tokens, first, last)
                    gap = gaps.setdefault(f"{family}:{token.upper}", Gap(family, unresolved[token.upper]))
                    gap.libraries.add(library)
                    if token.kind == "keyword" or token.upper in RUSTY_KEYWORDS or token.upper in GENERIC_TYPES \
                            or token.upper.startswith("__"):
                        gap.declarable = False
                    if token.upper in scope.external and (following in {":", ","} or tokens[k + 1].upper == "AT") \
                            and tokens[k - 1].upper in {";", ",", "VAR_EXTERNAL", "CONSTANT", "RETAIN", "PERSISTENT"}:
                        # The declaration `NAME [AT %addr] : TYPE;` of a VAR_EXTERNAL block: the global's type.
                        declared = scope.types.get(token.upper)
                        gap.uses["external"] += 1
                        gap.uses["value"] += 1
                        if declared:
                            gap.value_types[declared] += 5
                        kind = "external"
                    elif tokens[k - 1].text == ":" or tokens[k - 1].upper in {"OF", "TO", "REF_TO", "EXTENDS"}:
                        gap.uses["type"] += 1
                        if tokens[k - 1].text == ":" and tokens[k - 2].kind in {"identifier", "keyword"}:
                            instances[tokens[k - 2].upper] = gap.key
                        kind = "type"
                    elif following == "(":
                        gap.uses["call"] += 1
                        _record_call(gap, tokens, k, scope)
                        kind = "call"
                    elif following == "." and k + 2 < len(tokens):
                        gap.uses["value"] += 1
                        gap.uses["qualified"] += 1
                        kind = "qualified"
                    else:
                        gap.uses["value"] += 1
                        lhs = _statement_lhs(tokens, k, scope)
                        if lhs:
                            gap.value_types[lhs] += 1
                        elif _boolean_context(tokens, k, k + 1):
                            gap.value_types["BOOL"] += 1
                        kind = "value"
                    if len(gap.contexts) < MAX_CONTEXTS:
                        line = text.count("\n", 0, token.start) + 1
                        snippet = text.splitlines()[line - 1].strip()[:200]
                        if all(c["text"] != snippet for c in gap.contexts):
                            unit_source, truncated = _unit_excerpt(text, unit.start, unit.end, token.start)
                            semantic = _semantic_context(unit_source, snippet)
                            gap.contexts.append({"library": library,
                                                 "file": path.relative_to(normalized_dir).as_posix(),
                                                 "line": line, "use": kind,
                                                 "text": snippet, "unit": unit.qualified_name,
                                                 "unit_source": unit_source, "unit_source_truncated": truncated,
                                                 **semantic})
                if instances:
                    scope = scope or UnitScope(text, tokens, first, last)
                    _record_instances(gaps, instances, tokens, first, last, scope)
    # A name used only as a qualifier (Lib.Name) is a missing library or
    # namespace, not something a stub declaration can provide.
    return {key: gap for key, gap in sorted(gaps.items())
            if gap.uses["call"] or gap.uses["type"] or gap.uses["value"] > gap.uses["qualified"]}


def _record_instances(gaps, instances: dict[str, str], tokens, first: int, last: int, scope: UnitScope) -> None:
    """Calls and member accesses on variables whose type is a missing name."""
    for k in range(first, last - 1):
        token = tokens[k]
        if not token.is_word or token.upper not in instances or tokens[k - 1].text == ".":
            continue
        gap = gaps[instances[token.upper]]
        if tokens[k + 1].text == "(":
            gap.uses["instance_call"] += 1
            spans, _ = _arguments(tokens, k + 1)
            for a, b in spans:
                if b - a >= 3 and tokens[a + 1].text in {":=", "=>"}:
                    target = gap.members_written if tokens[a + 1].text == ":=" else gap.members_read
                    target[tokens[a].text][scope.expression_type(tokens, a + 2, b) or "?"] += 1
        elif tokens[k + 1].text == "." and k + 2 < last and tokens[k + 2].kind in {"identifier", "keyword"}:
            member = tokens[k + 2].text
            after = tokens[k + 3].text if k + 3 < last else ""
            if after == ":=":
                end = k + 4
                while end < last and tokens[end].text != ";":
                    end += 1
                gap.members_written[member][scope.expression_type(tokens, k + 4, end) or "?"] += 1
            else:
                result = _statement_lhs(tokens, k, scope)
                gap.members_read[member][result or ("BOOL" if _boolean_context(tokens, k, k + 3) else "?")] += 1


def _index(starts: list[int], offset: int) -> int:
    return bisect.bisect_left(starts, offset)


def _best(counter: collections.Counter) -> str | None:
    known = [(n, t) for t, n in counter.items() if t not in {"?", "<statement>"}]
    if not known:
        return None
    known.sort(key=lambda item: (-item[0], item[1]))
    return known[0][1]


def inferred_declaration(gap: Gap) -> str | None:
    """ST declaration of a stub from the call sites alone, or None when an
    element cannot be determined. Bodies follow the default behaviour: no
    statements, so outputs and the result keep their initial (zero) values."""
    kind = gap.kind
    if kind == "FUNCTION":
        if len(gap.arity) != 1:
            return None
        (count,) = gap.arity
        inputs = []
        if gap.formals and not gap.positional:
            names = sorted({key.split("|")[0] for key in gap.formals})
            if len(names) != count:
                return None
            for key in sorted(gap.formals):
                name, direction = key.split("|")
                declared = _best(gap.formals[key])
                if declared is None or direction != "in":
                    return None
                inputs.append((name, declared))
        elif gap.positional and not gap.formals:
            for position in range(count):
                declared = _best(gap.positional[position])
                if declared is None:
                    return None
                inputs.append((f"IN{position + 1}" if count > 1 else "IN", declared))
        elif count:
            return None
        result = _best(gap.results) or ("BOOL" if gap.results.get("<statement>") else None)
        if result is None:
            return None
        lines = [f"FUNCTION {gap.name} : {result}"]
        if inputs:
            lines += ["VAR_INPUT", *[f"    {n} : {t};" for n, t in inputs], "END_VAR"]
        return "\n".join(lines + ["END_FUNCTION"])
    if kind in {"FUNCTION_BLOCK", "TYPE"}:
        members = {}
        for member, counter in sorted(gap.members_written.items()):
            members[member] = ("VAR_INPUT", _best(counter))
        for member, counter in sorted(gap.members_read.items()):
            if member not in members:
                members[member] = ("VAR_OUTPUT", _best(counter))
        if any(declared is None for _, declared in members.values()):
            return None
        if kind == "TYPE":
            if not members:
                return None
            body = [f"    {m} : {t};" for m, (_, t) in members.items()]
            return "\n".join([f"TYPE {gap.name} :", "STRUCT", *body, "END_STRUCT", "END_TYPE"])
        lines = [f"FUNCTION_BLOCK {gap.name}"]
        for block in ("VAR_INPUT", "VAR_OUTPUT"):
            names = [(m, t) for m, (b, t) in members.items() if b == block]
            if names:
                lines += [block, *[f"    {m} : {t};" for m, t in names], "END_VAR"]
        return "\n".join(lines + ["END_FUNCTION_BLOCK"])
    declared = _best(gap.value_types)
    if declared is None:
        return None
    return f"VAR_GLOBAL\n    {gap.name} : {declared};\nEND_VAR"


DEFAULT_TYPE = "DINT"


def minimal_declaration(gap: Gap) -> str:
    """A declaration that compiles and links but computes nothing: the last
    resort for a name no better stub could be verified for.

    It takes every element the call sites show (parameters and members with
    the most frequent type seen, the result type or BOOL for a call used as
    a statement) and :data:`DEFAULT_TYPE` where the sites do not tell. There
    are no statements, so results, outputs and members keep their initial
    (zero) values. The call sites that need other types stay rejected; the
    stub never makes a unit accept that would not otherwise compile.
    """
    kind = gap.kind
    if kind == "FUNCTION":
        count = gap.arity.most_common(1)[0][0] if gap.arity else 0
        inputs, outputs = [], []
        if gap.formals:
            for key in sorted(gap.formals):
                name, direction = key.split("|")
                declared = _best(gap.formals[key]) or DEFAULT_TYPE
                (inputs if direction == "in" else outputs).append((name, declared))
        else:
            for position in range(count):
                declared = _best(gap.positional.get(position, collections.Counter())) or DEFAULT_TYPE
                inputs.append((f"IN{position + 1}" if count > 1 else "IN", declared))
        result = _best(gap.results) or ("BOOL" if gap.results.get("<statement>") else DEFAULT_TYPE)
        lines = [f"FUNCTION {gap.name} : {result}"]
        if inputs:
            lines += ["VAR_INPUT", *[f"    {n} : {t};" for n, t in inputs], "END_VAR"]
        if outputs:
            lines += ["VAR_OUTPUT", *[f"    {n} : {t};" for n, t in outputs], "END_VAR"]
        return "\n".join(lines + ["END_FUNCTION"])
    if kind in {"FUNCTION_BLOCK", "TYPE"}:
        members: dict[str, tuple[str, str]] = {}
        for member, counter in sorted(gap.members_written.items()):
            members[member] = ("VAR_INPUT", _best(counter) or DEFAULT_TYPE)
        for member, counter in sorted(gap.members_read.items()):
            members.setdefault(member, ("VAR_OUTPUT", _best(counter) or DEFAULT_TYPE))
        for key in sorted(gap.formals):
            name, direction = key.split("|")
            members.setdefault(name, ("VAR_INPUT" if direction == "in" else "VAR_OUTPUT",
                                      _best(gap.formals[key]) or DEFAULT_TYPE))
        if kind == "TYPE":
            body = [f"    {m} : {t};" for m, (_, t) in members.items()] or [f"    reserved : BYTE;"]
            return "\n".join([f"TYPE {gap.name} :", "STRUCT", *body, "END_STRUCT", "END_TYPE"])
        lines = [f"FUNCTION_BLOCK {gap.name}"]
        for block in ("VAR_INPUT", "VAR_OUTPUT"):
            names = [(m, t) for m, (b, t) in members.items() if b == block]
            if names:
                lines += [block, *[f"    {m} : {t};" for m, t in names], "END_VAR"]
        return "\n".join(lines + ["END_FUNCTION_BLOCK"])
    return f"VAR_GLOBAL\n    {gap.name} : {_best(gap.value_types) or DEFAULT_TYPE};\nEND_VAR"
