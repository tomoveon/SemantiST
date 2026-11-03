"""L1 rules for CODESYS-family export formats."""

from __future__ import annotations

from ...diagnostics import Located
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec


def as_comment(text: str) -> str:
    """Wrap text in a block comment that cannot close early or nest."""
    return "(* " + text.replace("(*", "( *").replace("*)", "* )") + " *)"


class ExportLibraryBlock(Rule):
    spec = RuleSpec(
        id="export-library-block",
        version=1,
        title="CODESYS 2.3 export metadata (LIBRARY ... END_LIBRARY, _ALARMCONFIG ... _END_ALARMCONFIG) -> comment",
        level="L1",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007"}),
        premise="CODESYS 2.3 .EXP/.TXT exports list each referenced library in a LIBRARY ... END_LIBRARY "
                "block and the project's alarm and visualisation settings in _NAME ... _END_NAME blocks at top "
                "level; they contain no declarations or statements.",
        not_applicable="Any other use of the word LIBRARY.",
        equivalence="The block has no run-time meaning; it is kept verbatim inside a comment and the "
                    "referenced library is recorded by the dependency report.",
        probe="export-library-block",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or located.unit is not None:
            return None
        if token.upper == "LIBRARY":
            closing = "END_LIBRARY"
        elif token.kind == "identifier" and token.upper.startswith("_") and not token.upper.startswith("_END_"):
            closing = "_END" + token.upper  # _ALARMCONFIG ... _END_ALARMCONFIG
        else:
            return None
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(token.start)
        if i > 0 and tokens[i - 1].text != ";" and tokens[i - 1].upper not in {
                "END_VAR", "END_TYPE", "END_FUNCTION", "END_FUNCTION_BLOCK", "END_PROGRAM", "END_LIBRARY"} \
                and not tokens[i - 1].upper.startswith("_END_"):
            return None
        j = i + 1
        while j < len(tokens) and tokens[j].upper != closing:
            if tokens[j].upper in {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "TYPE", "VAR_GLOBAL"}:
                return None
            j += 1
        if j >= len(tokens):
            return None
        end = tokens[j].end
        text = index.text[token.start:end]
        return Proposal(self.spec, (self.spec.id, file, token.start),
                        [Edit(file, token.start, end, as_comment(text))],
                        note=" ".join(text.split()[1:3]))


class EndFunctionBlockKeyword(Rule):
    spec = RuleSpec(
        id="end-functionblock-keyword",
        version=1,
        title="FUNCTIONBLOCK ... END_FUNCTIONBLOCK -> ... END_FUNCTION_BLOCK",
        level="L1",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007"}),
        premise="Older CODESYS versions accepted FUNCTIONBLOCK as the function block keyword; CODESYS 3 "
                "reports 'C0098: The keyword \"FUNCTIONBLOCK\" is no longer supported. Use \"FUNCTION_BLOCK\" "
                "instead.' (CODESYS 3 programming reference, compiler error C0098). The pinned RuSTy accepts "
                "the FUNCTIONBLOCK header (warning E013) but not the END_FUNCTIONBLOCK terminator, and reports "
                "the following units instead.",
        not_applicable="END_FUNCTIONBLOCK that does not close a function block at the top level (structural "
                       "scan), or is used as a name.",
        equivalence="Only the terminator's spelling changes; the block's declarations and body are unchanged.",
        probe="end-functionblock-keyword",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        edits = []
        for unit in index.units:
            if unit.kind != "FUNCTION_BLOCK" or not unit.closed:
                continue
            last = index.significant_index(unit.end) - 1
            if tokens[last].text == ";":
                last -= 1
            if tokens[last].upper == "END_FUNCTIONBLOCK":
                if located.offset < unit.start and located.diagnostic.line:
                    continue  # the error is before this block, not caused by it
                edits.append(Edit(file, tokens[last].start, tokens[last].end, "END_FUNCTION_BLOCK"))
        return Proposal(self.spec, (self.spec.id, file), edits) if edits else None


HEADER_KEYWORDS = {"METHOD", "FUNCTION", "FUNCTION_BLOCK", "PROGRAM"}
VAR_BLOCKS = {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR_INST", "VAR_STAT"}


class HeaderSemicolon(Rule):
    spec = RuleSpec(
        id="header-semicolon",
        version=1,
        title="METHOD M : T; VAR_INPUT ... -> METHOD M : T VAR_INPUT ... (also FUNCTION, FUNCTION_BLOCK, PROGRAM)",
        level="L1",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007"}),
        premise="CODESYS accepts a semicolon after a POU or method header; its own documentation writes "
                "'FUNCTION SUM: INT;' followed by VAR_IN_OUT (CODESYS 3 programming reference, data type "
                "ARRAY, arrays of variable length), and TwinCAT exports such method headers (TcUnit, TcOpen). "
                "The pinned RuSTy ends the header at the semicolon and parses the following VAR block as "
                "statements.",
        not_applicable="A semicolon that is not the end of a header directly followed by a VAR block (for "
                       "example a header followed by statements, which the pinned RuSTy already accepts): "
                       "not rewritten.",
        equivalence="The separator carries no meaning in the declaration part; it is kept as a comment.",
        probe="header-semicolon",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        k = min(index.significant_index(located.offset), len(tokens) - 1)
        # The error is reported at the VAR block or the first token after it.
        for block in range(k, max(k - 3, 0), -1):
            if tokens[block].upper in VAR_BLOCKS and tokens[block - 1].text == ";":
                break
        else:
            return None
        semicolon = block - 1
        for header in range(semicolon - 1, max(semicolon - 40, -1), -1):
            word = tokens[header].upper
            if word in HEADER_KEYWORDS:
                break
            if tokens[header].text == ";" or word.startswith("END_") or word in VAR_BLOCKS:
                return None
        else:
            return None
        if semicolon - header < 2:
            return None
        start, end = tokens[semicolon].start, tokens[semicolon].end
        return Proposal(self.spec, (self.spec.id, file, start), [Edit(file, start, end, as_comment(";"))])


STATEMENT_START = {";", "THEN", "ELSE", "DO", "REPEAT", "END_VAR", ":"}


def _reference_end(tokens, i: int, last: int) -> int | None:
    """End (exclusive) of the variable reference starting at ``i``:
    ``name`` followed by ``.member``, ``[index]`` or ``^``; None if none."""
    if i >= last or tokens[i].kind != "identifier":
        return None
    j = i + 1
    while j < last:
        if tokens[j].text == "." and j + 1 < last and tokens[j + 1].kind in {"identifier", "keyword"}:
            j += 2
        elif tokens[j].text == "^":
            j += 1
        elif tokens[j].text == "[":
            depth = 0
            while j < last:
                depth += {"[": 1, "]": -1}.get(tokens[j].text, 0)
                j += 1
                if depth == 0:
                    break
            if depth:
                return None
        else:
            break
    return j


def chained_assignment(tokens, start: int, last: int):
    """``t1 := t2 := ... := expr;`` at ``start``: the target spans, the
    expression span and the index of the ';'; None for anything else."""
    targets = []
    i = start
    while True:
        end = _reference_end(tokens, i, last)
        if end is None or end >= last or tokens[end].text != ":=":
            break
        targets.append((i, end))
        i = end + 1
    if len(targets) < 2:
        return None
    depth = 0
    j = i
    while j < last and not (depth == 0 and tokens[j].text == ";"):
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(tokens[j].text, 0)
        j += 1
    if j >= last or j == i:
        return None
    return targets, (i, j), j


class ChainedAssignment(Rule):
    spec = RuleSpec(
        id="chained-assignment",
        version=1,
        title="a := b := expr; -> a := expr; b := expr; (expression without calls or targets)",
        level="L2",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"CODEGEN"}),
        premise="CODESYS allows several assignments in one statement ('pbBeta := pbAlpha := 16#01; // Multiple "
                "assignment', CODESYS 3 reference, compiler error C0509), and 'all assignments refer to the "
                "operands at the end of the code line' (ExST Assignment S=, Multiple assignments): every target "
                "receives the value of the final expression. The pinned RuSTy parses the chain but fails in code "
                "generation ('Expected Struct-literal, got Assignment').",
        not_applicable="Expressions that call a function or mention one of the targets, and targets whose index "
                       "expressions do (the expression would be evaluated more than once, or a later assignment "
                       "would see an earlier one).",
        equivalence="Without calls and without the targets in the expression, evaluating the expression once per "
                    "target gives the same value each time, so each target receives the value the chain gives it, "
                    "converted to its own type as before. The statements stay on the same line.",
        probe="chained-assignment",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        if not located.diagnostic.message.startswith("Expected Struct-literal, got Assignment") or located.unit is None:
            return None
        index = context.indices[file]
        tokens = index.significant
        first, last = index.significant_index(located.unit.start), index.significant_index(located.unit.end)
        for k in range(first + 1, last):
            if tokens[k - 1].text not in STATEMENT_START and tokens[k - 1].upper not in STATEMENT_START:
                continue
            chain = chained_assignment(tokens, k, last)
            if chain is None:
                continue
            targets, (e0, e1), semicolon = chain
            if not tokens[k].start <= located.offset <= tokens[semicolon].end:
                continue
            names = {tokens[s].upper for s, _ in targets}
            spans = [range(s, e) for s, e in targets] + [range(e0, e1)]
            for span in spans:
                for m in span:
                    if tokens[m].text == "(" and m > 0 and tokens[m - 1].kind == "identifier" and m - 1 in span:
                        return None  # a call
            if any(tokens[m].is_word and tokens[m].upper in names for m in range(e0, e1)):
                return None
            for s, e in targets:
                if any(tokens[m].is_word and tokens[m].upper in names for m in range(s + 1, e)):
                    return None
            expression = index.text[tokens[e0].start:tokens[e1 - 1].end]
            statements = " ".join(f"{index.text[tokens[s].start:tokens[e - 1].end]} := {expression};"
                                  for s, e in targets)
            return Proposal(self.spec, (self.spec.id, file, tokens[k].start),
                            [Edit(file, tokens[k].start, tokens[semicolon].end, statements)],
                            note=f"{len(targets)} targets")
        return None


ZERO_LITERALS = {
    "BOOL": "FALSE", "SINT": "0", "INT": "0", "DINT": "0", "LINT": "0", "USINT": "0", "UINT": "0", "UDINT": "0",
    "ULINT": "0", "BYTE": "0", "WORD": "0", "DWORD": "0", "LWORD": "0", "REAL": "0.0", "LREAL": "0.0",
    "STRING": "''", "WSTRING": '""', "TIME": "T#0s", "LTIME": "LTIME#0s",
}


def struct_defaults(context: RuleContext, type_name: str) -> list[tuple[str, str, bool, bool]] | None:
    """Members of ``TYPE type_name : STRUCT ... END_STRUCT`` as (name, declared
    initial value or the zero value of its elementary type, value is zero, is a
    string); None when the type is not such a struct or a member has another type."""
    for _, index in sorted(context.indices.items()):
        tokens = index.significant
        for i, token in enumerate(tokens[:-3]):
            if token.upper != "TYPE" or tokens[i + 1].upper != type_name:
                continue
            k = i + 2 + (tokens[i + 2].text == ":")
            if tokens[k].upper != "STRUCT":
                return None
            members = []
            k += 1
            while k < len(tokens) and tokens[k].upper != "END_STRUCT":
                if not (tokens[k].kind in {"identifier", "keyword"} and tokens[k + 1].text == ":"):
                    return None
                name, j = tokens[k].text, k + 2
                type_end = j
                while type_end < len(tokens) and tokens[type_end].text not in {";", ":="}:
                    type_end += 1
                base = tokens[j].upper
                sized_string = base in {"STRING", "WSTRING"} and type_end > j + 1 and tokens[j + 1].text in {"(", "["}
                if base not in ZERO_LITERALS or (type_end != j + 1 and not sized_string):
                    return None
                end = type_end
                if tokens[type_end].text == ":=":
                    while end < len(tokens) and tokens[end].text != ";":
                        end += 1
                    value = index.text[tokens[type_end + 1].start:tokens[end - 1].end]
                else:
                    value = ZERO_LITERALS[base]
                members.append((name, value, value == ZERO_LITERALS[base], base in {"STRING", "WSTRING"}))
                k = end + 1
            return members or None
    return None


class EmptyStructInitializer(Rule):
    spec = RuleSpec(
        id="empty-struct-initializer",
        version=1,
        title="ARRAY ... OF S := (...), (), ... -> each () written out with the members' initial values",
        level="L2",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007"}),
        premise="An element written () in an initializer of an array of structures gives no value to any "
                "member, and 'elements to which no initialization value is assigned explicitly are initialized "
                "internally with the default value' (CODESYS 3 reference, ARRAY data type, Initialize "
                "partially): every member takes its declared initial value, or the zero value of its type.",
        not_applicable="Structures with members of a structured, enumerated or unknown type, and () outside "
                       "an array-of-structure initializer (reported unsupported).",
        equivalence="The literal names every member with a non-zero initial value; RuSTy zero-fills the "
                    "members a structure literal leaves out, which is their initial value. (Omitting the whole "
                    "element would not be equivalent: the pinned RuSTy zero-fills omitted elements of local "
                    "arrays even where the structure declares initial values.)",
        probe="empty-struct-initializer",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        k = index.significant_index(located.offset)
        if not (0 < k < len(tokens) and tokens[k].text == ")" and tokens[k - 1].text == "("):
            return None
        # The declaration whose initializer holds the element: ... OF S := ...
        start = k - 1
        while start > 1 and tokens[start].text != ";" and not (
                tokens[start].text == ":=" and tokens[start - 2].upper == "OF"):
            start -= 1
        if tokens[start].text != ":=":
            return None
        literal_members = struct_defaults(context, tokens[start - 1].upper)
        if literal_members is None:
            return None
        # Members whose value is zero are left out: RuSTy zero-fills members a
        # structure literal omits, and the pinned codegen sizes a string
        # literal in a local structure literal wrongly ([81 x i8] for a
        # STRING(10) member). One member is always written, a non-string first.
        written = [m for m in literal_members if not m[2]] or \
            [next((m for m in literal_members if not m[3]), literal_members[0])]
        literal = "(" + ", ".join(f"{name} := {value}" for name, value, _, _ in written) + ")"
        edits = []
        depth = 0
        j = start + 1
        while j < len(tokens) and not (depth == 0 and tokens[j].text == ";"):
            if tokens[j].text == "(" and tokens[j + 1].text == ")":
                edits.append(Edit(file, tokens[j].start, tokens[j + 1].end, literal))
                j += 2
                continue
            depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(tokens[j].text, 0)
            j += 1
        return Proposal(self.spec, (self.spec.id, file, tokens[start].start), edits,
                        note=f"{len(edits)} elements of {tokens[start - 1].text}")
