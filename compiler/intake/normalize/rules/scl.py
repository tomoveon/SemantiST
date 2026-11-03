"""Rules for Siemens SCL (TIA Portal and STEP 7 Classic source files)."""

from __future__ import annotations

import re

from compiler.parser.st import RUSTY_KEYWORDS

from ...diagnostics import Located, SourceIndex
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec
from .codesys import as_comment

SCL = frozenset({"siemens-scl"})
BLOCK_ATTRIBUTES = {"TITLE", "VERSION", "AUTHOR", "NAME", "FAMILY", "KNOW_HOW_PROTECT", "CODE_VERSION1",
                    "UNLINKED", "READ_ONLY", "NON_RETAIN", "S7_OPTIMIZED_ACCESS"}
HEADER_KINDS = {"FUNCTION", "FUNCTION_BLOCK", "ORGANIZATION_BLOCK", "DATA_BLOCK", "TYPE"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _line_bounds(text: str, offset: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return start, len(text) if end < 0 else end


def attribute_lines(index: SourceIndex, header_index: int) -> list[tuple[int, int]]:
    """Spans of the block-attribute lines following the block header at ``header_index``."""
    tokens = index.significant
    spans = []
    i = header_index + 1
    header_line = tokens[header_index].line
    while i < len(tokens) and tokens[i].line == header_line:
        i += 1
    while i < len(tokens):
        token = tokens[i]
        if token.upper not in BLOCK_ATTRIBUTES:
            break
        start, end = _line_bounds(index.text, token.start)
        if index.text[start:token.start].strip():
            break
        spans.append((token.start, end))
        while i < len(tokens) and tokens[i].start < end:
            i += 1
    return spans


def enclosing_header(index: SourceIndex, offset: int) -> int | None:
    """Index of the block header keyword the offset belongs to."""
    tokens = index.significant
    i = min(index.significant_index(offset), len(tokens) - 1)
    while i >= 0:
        if tokens[i].upper in HEADER_KINDS and tokens[i].is_word:
            return i
        if tokens[i].upper in {"END_FUNCTION", "END_FUNCTION_BLOCK", "END_TYPE", "END_DATA_BLOCK",
                               "END_ORGANIZATION_BLOCK", "VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT",
                               "VAR_TEMP", "STRUCT", "BEGIN"}:
            return None
        i -= 1
    return None


class BlockAttributes(Rule):
    spec = RuleSpec(
        id="scl-block-attributes",
        version=2,
        title="SCL block attributes (TITLE, VERSION, AUTHOR, NAME, FAMILY, ...) -> comments",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E007", "E006"}),
        premise="Lines such as VERSION : '1.0' or TITLE = ... between a block header and its declarations are "
                "S7 block properties shown in the engineering tool; they declare no variables and no code.",
        not_applicable="Attribute words used inside declarations or statements.",
        equivalence="Block properties have no run-time meaning (NON_RETAIN only matters for a warm restart, "
                    "which the execution model does not perform); each line is kept as a comment.",
        probe="scl-block-attributes",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        header = enclosing_header(index, located.offset)
        position = index.significant_index(located.offset)
        if header is None and 0 < position < len(index.significant):
            # A lone TITLE = '...' line parses as an expression; the error is
            # then reported at the declaration keyword that follows it.
            header = enclosing_header(index, index.significant[position - 1].start)
        if header is None:
            return None
        spans = attribute_lines(index, header)
        on_attribute = located.token is not None and located.token.upper in BLOCK_ATTRIBUTES
        follows = bool(spans) and index.significant_index(spans[-1][1]) == position
        if not spans or not (on_attribute or follows or any(s <= located.offset <= e + 1 for s, e in spans)):
            return None
        return Proposal(self.spec, (self.spec.id, file, spans[0][0]),
                        [Edit(file, s, e, as_comment(index.text[s:e])) for s, e in spans])


class QuotedIdentifier(Rule):
    spec = RuleSpec(
        id="scl-quoted-identifier",
        version=1,
        title='SCL "symbol" -> identifier (consistently, mangled when not a valid identifier)',
        level="L1",
        dialects=SCL,
        codes=frozenset({"E007", "E006", "E048", "E052"}),
        premise='In SCL double quotes delimit a symbol name (block, data block, UDT or tag); WSTRING '
                "literals are written WSTRING#'...'.",
        not_applicable="SIMATIC AX, where double quotes delimit WSTRING literals.",
        equivalence="Every occurrence of the quoted symbol in the project becomes the same identifier; names "
                    "that are not valid identifiers are mangled to a fresh name, so bindings are unchanged. "
                    "The mapping is recorded.",
        probe="scl-quoted-identifier",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or token.kind != "wstring":
            return None
        symbol = token.text[1:-1]
        target = symbol
        if not IDENTIFIER.match(symbol) or symbol.upper() in RUSTY_KEYWORDS:
            base = re.sub(r"[^A-Za-z0-9_]", "_", symbol) or "S"
            if not re.match(r"[A-Za-z_]", base):
                base = "S_" + base
            target = context.fresh_name(base, "__Q")
        edits = []
        for name, index in sorted(context.indices.items()):
            for other in index.significant:
                if other.kind == "wstring" and other.text[1:-1].upper() == symbol.upper():
                    edits.append(Edit(name, other.start, other.end, target))
        return Proposal(self.spec, (self.spec.id, symbol.upper()), edits, note=f'"{symbol}" -> {target}',
                        renames={'"' + symbol + '"': target})


class LocalPrefix(Rule):
    spec = RuleSpec(
        id="scl-local-prefix",
        version=1,
        title="#local -> local",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E007"}),
        premise="In TIA Portal SCL a leading # marks an identifier declared in the block itself.",
        not_applicable="# inside literals (T#, 16#) is part of the literal token and never matched.",
        equivalence="RuSTy resolves block-local names before global ones, so the unprefixed identifier "
                    "refers to the same declaration.",
        probe="scl-local-prefix",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or token.kind != "error" or token.text != "#":
            return None
        index = context.indices[file]
        unit = located.unit
        low, high = (unit.start, unit.end) if unit else (0, len(index.text))
        edits = []
        tokens = index.tokens
        for i, other in enumerate(tokens[:-1]):
            if other.start < low or other.start >= high:
                continue
            if other.kind == "error" and other.text == "#" and tokens[i + 1].kind in {"identifier", "keyword",
                                                                                       "wstring"}:
                edits.append(Edit(file, other.start, other.end, ""))
        if not edits:
            return None
        return Proposal(self.spec, (self.spec.id, file, low), edits)


class TypeColon(Rule):
    spec = RuleSpec(
        id="scl-type-colon",
        version=1,
        title="TYPE name STRUCT -> TYPE name : STRUCT",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E006", "E007"}),
        premise="STEP 7 SCL declares a UDT as TYPE name STRUCT ... END_STRUCT END_TYPE without a colon.",
        not_applicable="Other TYPE forms.",
        equivalence="Punctuation only; the same structure type is declared.",
        probe="scl-type-colon",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(located.offset)
        # Find TYPE <name> [attributes] STRUCT around the diagnostic.
        j = i
        while j > 0 and tokens[j].upper != "TYPE" and i - j < 4:
            j -= 1
        if tokens[j].upper != "TYPE" or j + 2 >= len(tokens):
            return None
        name = tokens[j + 1]
        k = j + 2
        while k < len(tokens) and tokens[k].upper not in {"STRUCT", ":"} and k - j < 40:
            k += 1
        if k >= len(tokens) or tokens[k].upper != "STRUCT":
            return None
        return Proposal(self.spec, (self.spec.id, file, name.start), [Edit(file, name.end, name.end, " :")])


class BeginSeparator(Rule):
    spec = RuleSpec(
        id="scl-begin",
        version=1,
        title="STEP 7 SCL BEGIN between declarations and statements -> comment",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E007"}),
        premise="In STEP 7 Classic SCL the keyword BEGIN only separates a block's declaration part from its "
                "statement part.",
        not_applicable="BEGIN inside DATA_BLOCK (initial values; handled by scl-data-block).",
        equivalence="A separator without run-time meaning; kept as a comment.",
        probe="scl-begin",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        unit = located.unit
        if unit is None or unit.kind not in {"FUNCTION", "FUNCTION_BLOCK", "ORGANIZATION_BLOCK"}:
            return None
        for token in index.significant[index.significant_index(unit.start):index.significant_index(unit.end)]:
            if token.upper == "BEGIN" and token.kind == "identifier":
                return Proposal(self.spec, (self.spec.id, file, token.start),
                                [Edit(file, token.start, token.end, "(* BEGIN *)")])
        return None


class Region(Rule):
    spec = RuleSpec(
        id="scl-region",
        version=1,
        title="TIA SCL REGION name ... END_REGION -> comments",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E006", "E007"}),
        premise="REGION and END_REGION enclose a program part so the TIA editor can collapse and name it "
                "(TIA Portal V21, 'Creating SCL programs': 'To summarize a program part in a region, enclose "
                "it with the keywords REGION and END_REGION. You can give each region a name').",
        not_applicable="REGION used as a variable or member name (followed by ':', ':=' or '.').",
        equivalence="A region only groups statements in the editor; the statements inside stay in place and in "
                     "order. The REGION line (keyword and name) and END_REGION are kept as comments.",
        probe="scl-region",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        unit = located.unit
        if unit is None or unit.kind not in {"FUNCTION", "FUNCTION_BLOCK", "ORGANIZATION_BLOCK", "PROGRAM"}:
            return None
        tokens = index.significant
        first, last = index.significant_index(unit.start), index.significant_index(unit.end)
        edits = []
        for k in range(first, last):
            token = tokens[k]
            if token.upper not in {"REGION", "END_REGION"} or not token.is_word:
                continue
            line_start, line_end = _line_bounds(index.text, token.start)
            following = tokens[k + 1].text if k + 1 < len(tokens) else ""
            if index.text[line_start:token.start].strip() or following in {":", ":=", "."}:
                continue
            if token.upper == "REGION":
                end = line_end
            else:
                end = tokens[k + 1].end if following == ";" and tokens[k + 1].line == token.line else token.end
            edits.append(Edit(file, token.start, end, as_comment(index.text[token.start:end].rstrip())))
        if not edits:
            return None
        return Proposal(self.spec, (self.spec.id, file, edits[0].start), edits)


TIME_LITERAL = re.compile(r"^(?:L?T|L?TIME)#.*\d(?:D|H|M|S|MS|US|NS)$", re.I)
UNIT_TAIL = re.compile(r"^(?:_\d+(?:\.\d+)?(?:D|H|M|S|MS|US|NS))+$", re.I)


class TimeLiteralUnderscore(Rule):
    spec = RuleSpec(
        id="time-literal-underscore",
        version=1,
        title="T#2s_750ms -> T#2s750ms",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E007"}),
        premise="Siemens SCL and IEC 61131-3 allow an underscore between the units of a duration literal "
                "(T#24d_20h_31m_23s_647ms in the TIA SCL reference); RuSTy ends the literal at the underscore.",
        not_applicable="An identifier that is separated from the literal by whitespace or does not consist of "
                       "underscore-separated duration units.",
        equivalence="The underscore is only a separator: the units and their values are unchanged, so the "
                    "literal denotes the same duration.",
        probe="time-literal-underscore",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        token = located.token
        if token is None or not token.is_word or not UNIT_TAIL.match(token.text):
            return None
        position = index.significant_index(token.start)
        if position == 0:
            return None
        literal = index.significant[position - 1]
        if literal.end != token.start or not TIME_LITERAL.match(literal.text):
            return None
        return Proposal(self.spec, (self.spec.id, file, token.start),
                        [Edit(file, token.start, token.end, token.text.replace("_", ""))])


class DataBlock(Rule):
    spec = RuleSpec(
        id="scl-data-block",
        version=1,
        title="DATA_BLOCK name STRUCT ... END_STRUCT BEGIN END_DATA_BLOCK -> VAR_GLOBAL name : STRUCT ... END_VAR",
        level="L2",
        dialects=SCL,
        codes=frozenset({"E007"}),
        premise="A global data block is one global variable of the block's structure (or, for an instance "
                "data block, of the function block's type); its start values are the declared ones, replaced by "
                "the actual values the BEGIN section assigns to top-level members.",
        not_applicable="BEGIN sections that assign indexed or nested members (reported unsupported), and "
                       "data blocks derived from a UDT with modified start values.",
        equivalence="The global variable has the same members and start values (BEGIN values become the "
                    "members' initializers, the assignments are kept as a comment); S7 data-block "
                    "properties are kept as comments. Access by name (DB.member) is unchanged; absolute "
                    "addressing (DBX/DBW) is not supported.",
        probe="scl-data-block",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or token.upper != "DATA_BLOCK":
            return None
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(token.start)
        if i + 1 >= len(tokens):
            return None
        name = tokens[i + 1]
        end = i + 2
        while end < len(tokens) and tokens[end].upper not in {"END_DATA_BLOCK", "DATA_BLOCK"}:
            end += 1
        if end >= len(tokens) or tokens[end].upper != "END_DATA_BLOCK":
            return None
        begin = next((k for k in range(i + 2, end) if tokens[k].upper == "BEGIN"), None)
        if begin is None:
            return None
        # BEGIN may assign actual values to top-level members: member := value;
        values: dict[str, tuple[int, int]] = {}
        k = begin + 1
        while k < end:
            if not (tokens[k].kind == "identifier" and tokens[k + 1].text == ":="):
                return None  # indexed or nested paths: not a plain start value
            j = k + 2
            while j < end and tokens[j].text != ";":
                j += 1
            values[tokens[k].upper] = (tokens[k + 2].start, tokens[j - 1].end)
            k = j + 1
        attributes = attribute_lines(index, i)
        k = i + 2
        while k < begin and any(s <= tokens[k].start < e for s, e in attributes):
            k += 1
        edits = [Edit(file, s, e, as_comment(index.text[s:e])) for s, e in attributes]
        header = Edit(file, token.start, name.end, f"VAR_GLOBAL\n{name.text} :")
        footer = [Edit(file, tokens[begin].start, tokens[begin].end, "END_VAR"),
                  Edit(file, tokens[end].start, tokens[end].end, "(* END_DATA_BLOCK *)")]
        if tokens[k].upper == "STRUCT":
            # DATA_BLOCK name STRUCT ... END_STRUCT [;] BEGIN END_DATA_BLOCK
            close = max(j for j in range(k, begin) if tokens[j].upper == "END_STRUCT")
            if tokens[close + 1].text != ";":
                edits.append(Edit(file, tokens[close].end, tokens[close].end, ";"))
        elif k + 1 == begin and tokens[k].kind in {"identifier", "wstring"}:
            # Instance data block: DATA_BLOCK name fb_type BEGIN END_DATA_BLOCK
            edits.append(Edit(file, tokens[k].end, tokens[k].end, ";"))
        elif tokens[k].upper == "VAR":
            # TIA form: DATA_BLOCK name VAR ... END_VAR BEGIN END_DATA_BLOCK
            close = max(j for j in range(k, begin) if tokens[j].upper == "END_VAR")
            header = Edit(file, token.start, name.end, f"VAR_GLOBAL\n{name.text} : STRUCT")
            edits.append(Edit(file, tokens[k].start, tokens[k].end, "(* VAR *)"))
            edits.append(Edit(file, tokens[close].start, tokens[close].end, "END_STRUCT;"))
        else:
            return None
        if values:
            # Declarations of the valued members: name : type [:= init] ;
            body = tokens[i + 2:begin]
            for pos, item in enumerate(body):
                if item.upper in values and pos + 1 < len(body) and body[pos + 1].text == ":":
                    q = pos + 2
                    while q < len(body) and body[q].text not in {";", ":="}:
                        q += 1
                    if q >= len(body):
                        return None
                    value = index.text[values[item.upper][0]:values[item.upper][1]]
                    if body[q].text == ":=":
                        r = q + 1
                        while r < len(body) and body[r].text != ";":
                            r += 1
                        edits.append(Edit(file, body[q + 1].start, body[r - 1].end, value))
                    else:
                        edits.append(Edit(file, body[q - 1].end, body[q - 1].end, " := " + value))
                    del values[item.upper]
            if values:
                return None
            footer.insert(1, Edit(file, tokens[begin + 1].start, tokens[end - 1].end,
                                  as_comment(index.text[tokens[begin + 1].start:tokens[end - 1].end])))
        edits = [header, *edits, *footer]
        return Proposal(self.spec, (self.spec.id, file, token.start), edits, note=name.text)


class OrganizationBlock(Rule):
    spec = RuleSpec(
        id="scl-organization-block",
        version=1,
        title="ORGANIZATION_BLOCK name ... END_ORGANIZATION_BLOCK -> PROGRAM name ... END_PROGRAM",
        level="L2",
        dialects=SCL,
        codes=frozenset({"E007"}),
        premise="An organization block is the cyclically (or event-) called entry point of a program; it has "
                "no parameters, and its VAR_TEMP start information is written by the operating system.",
        not_applicable="Reading the OS start information (OBx_* temporaries) is reported as unsupported "
                       "because no runtime writes it.",
        equivalence="Called once per scan, a PROGRAM executes the same statements with the same variables.",
        probe="scl-organization-block",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or token.upper != "ORGANIZATION_BLOCK":
            return None
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(token.start)
        end = next((k for k in range(i + 1, len(tokens)) if tokens[k].upper == "END_ORGANIZATION_BLOCK"), None)
        if end is None:
            return None
        return Proposal(self.spec, (self.spec.id, file, token.start),
                        [Edit(file, token.start, token.end, "PROGRAM"),
                         Edit(file, tokens[end].start, tokens[end].end, "END_PROGRAM")])


DECLARATION_ENDS = {"END_VAR", "END_CONST", "END_LABEL", "BEGIN"}


class ConstInline(Rule):
    spec = RuleSpec(
        id="scl-const-inline",
        version=2,
        title="CONST name := expr; END_CONST -> each use of name replaced by (expr)",
        level="L2",
        dialects=SCL,
        codes=frozenset({"E007", "E006"}),
        premise="S7 SCL constants are untyped: a constant takes its data type from the operation it is used in "
                "(S7-SCL V5.3 manual, 9.1.2), like a literal written at that place. They are local to the "
                "block; local and global constants can be programmed as CASE labels (TIA SCL manual, "
                "CASE).",
        not_applicable="Constants whose name is also declared as a variable of the block, or used as a member "
                       "name after '.' (reported unsupported).",
        equivalence="RuSTy also types literals by context, so the parenthesized expression at each use has the "
                    "type and value the constant had there. The CONST section is kept as a comment.",
        probe="scl-const-inline",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        unit = located.unit
        if unit is None:
            return None
        tokens = index.significant
        first, last = index.significant_index(unit.start), index.significant_index(unit.end)
        start = next((i for i in range(first, last) if tokens[i].upper == "CONST"), None)
        if start is None:
            return None
        end = next((i for i in range(start, last) if tokens[i].upper == "END_CONST"), None)
        if end is None or not (tokens[start].start <= located.offset <= tokens[end].end):
            return None
        constants: dict[str, str] = {}
        i = start + 1
        while i < end:
            if not (tokens[i].kind == "identifier" and tokens[i + 1].text == ":="):
                return None
            j = i + 2
            while j < end and tokens[j].text != ";":
                j += 1
            # The exact source text of the expression, with earlier constants substituted.
            pieces, cursor = [], tokens[i + 2].start
            for k in range(i + 2, j):
                if tokens[k].is_word and tokens[k].upper in constants:
                    pieces.append(index.text[cursor:tokens[k].start])
                    pieces.append(constants[tokens[k].upper])
                    cursor = tokens[k].end
            pieces.append(index.text[cursor:tokens[j - 1].end])
            constants[tokens[i].upper] = "(" + "".join(pieces) + ")"
            i = j + 1
        edits = [Edit(file, tokens[start].start, tokens[end].end,
                      as_comment(index.text[tokens[start].start:tokens[end].end]))]
        # In the statement part (after the last declaration section) and
        # without a LABEL section, ``name :`` can only be a CASE label.
        body = max((i for i in range(first, last) if tokens[i].upper in DECLARATION_ENDS), default=last)
        has_labels = any(tokens[i].upper == "LABEL" for i in range(first, last))
        for k in range(first, last):
            if start <= k <= end:
                continue
            token = tokens[k]
            if token.is_word and token.upper in constants:
                previous = tokens[k - 1].text
                following = tokens[k + 1].text if k + 1 < len(tokens) else ""
                if following == ":" and (k < body or has_labels):
                    return None  # also declared as a variable or label
                if previous == "." or (following in {":=", "=>"} and previous in {"(", ","}):
                    continue  # a member or formal parameter name, not the constant
                edits.append(Edit(file, token.start, token.end, constants[token.upper]))
        return Proposal(self.spec, (self.spec.id, file, tokens[start].start), edits,
                        note=", ".join(sorted(constants)))


STANDARD_FORMALS = {
    "LIMIT": ["MN", "IN", "MX"],
    "SEL": ["G", "IN0", "IN1"],
}


def _formals(function: str, count: int) -> list[str] | None:
    if function in STANDARD_FORMALS:
        return STANDARD_FORMALS[function]
    if function in {"MIN", "MAX", "CONCAT"}:
        return [f"IN{i}" for i in range(1, count + 1)]
    if function == "MUX":
        return ["K", *[f"IN{i}" for i in range(count - 1)]]
    return None


class StandardFormalArguments(Rule):
    spec = RuleSpec(
        id="standard-formal-arguments",
        version=1,
        title="MIN(IN1 := a, IN2 := b) -> MIN(a, b) for MIN/MAX/LIMIT/SEL/MUX/CONCAT",
        level="L1",
        dialects=frozenset({"siemens-scl", "codesys", "pcworx", "iec", "sysmac", "gxworks", "br"}),
        codes=frozenset({"E089", "E032"}),
        premise="The call names the standard formal parameters (IN1..INn, MN/IN/MX, G/IN0/IN1, K/IN0..) in "
                "their standard order; RuSTy declares these functions without named inputs.",
        not_applicable="Formal names in another order or other names (left unchanged and reported).",
        equivalence="With the names in standard order, positional arguments bind the same values to the same "
                    "parameters and are evaluated in the same order.",
        probe="standard-formal-arguments",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(located.offset)
        # Find the call: NAME ( ... around the diagnostic.
        while i >= 0 and not (tokens[i].is_word and i + 1 < len(tokens) and tokens[i + 1].text == "("
                              and _formals(tokens[i].upper, 2) is not None):
            i -= 1
            if i < index.significant_index(located.offset) - 3:
                return None
        if i < 0:
            return None
        depth, j, args, current = 0, i + 1, [], []
        while j < len(tokens):
            text = tokens[j].text
            if text == "(":
                depth += 1
                if depth == 1:
                    j += 1
                    continue
            elif text == ")":
                depth -= 1
                if depth == 0:
                    args.append(current)
                    break
            elif text == "," and depth == 1:
                args.append(current)
                current = []
                j += 1
                continue
            current.append(j)
            j += 1
        formals = _formals(tokens[i].upper, len(args))
        if formals is None or len(formals) != len(args):
            return None
        edits = []
        for formal, arg in zip(formals, args):
            if len(arg) < 3 or tokens[arg[0]].upper != formal or tokens[arg[1]].text != ":=":
                return None
            edits.append(Edit(file, tokens[arg[0]].start, tokens[arg[2]].start, ""))
        return Proposal(self.spec, (self.spec.id, file, tokens[i].start), edits)



LITERAL_PREFIXES = {"W#": "WORD#", "DW#": "DWORD#", "B#": "BYTE#", "L#": "DINT#"}


class TypedLiteralPrefix(Rule):
    spec = RuleSpec(
        id="scl-typed-literal-prefix",
        version=1,
        title="W#16#FFFF / DW#16#... / L#n -> WORD#16#FFFF / DWORD#... / DINT#n",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E048", "E052"}),
        premise="S7 writes typed bit-string literals with the abbreviations B#, W#, DW# and 32-bit integer "
                "literals with L#; the pinned RuSTy resolves W and DW as names (B# happens to be accepted).",
        not_applicable="C# (counter values) and S5T# (S5 time): different representations, not rewritten.",
        equivalence="The same literal with the full IEC type prefix.",
        probe="scl-typed-literal-prefix",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or token.kind != "cast_prefix" or token.upper not in LITERAL_PREFIXES:
            return None
        edits = []
        for name, index in sorted(context.indices.items()):
            for other in index.significant:
                if other.kind == "cast_prefix" and other.upper == token.upper:
                    edits.append(Edit(name, other.start, other.end, LITERAL_PREFIXES[token.upper]))
        return Proposal(self.spec, (self.spec.id, token.upper), edits)


BIT_VIEW_BASES = {"BYTE": 1, "SINT": 1, "USINT": 1, "WORD": 2, "INT": 2, "UINT": 2, "DWORD": 4, "DINT": 4,
                  "UDINT": 4}
_BOOL_VIEW = re.compile(r"cannot assign '(?P<base>\w+)' to 'ARRAY\[(?P<lo>-?\d+)\.\.(?P<hi>-?\d+)\] OF BOOL'")


def _declared_type(tokens, first: int, last: int, name: str) -> str | None:
    """Type of ``name : TYPE`` declared in ``[first, last)`` (elementary types only)."""
    for k in range(first, last - 2):
        if tokens[k].upper == name and tokens[k + 1].text == ":" and tokens[k - 1].text != ".":
            if tokens[k + 3].text in {";", ":="}:
                return tokens[k + 2].upper
            return None
    return None


class BoolArrayView(Rule):
    spec = RuleSpec(
        id="s7-bool-array-view",
        version=1,
        title="v AT x : ARRAY[lo..hi] OF BOOL (x BYTE/WORD/DWORD/INT/DINT), v[c] -> x.%Xn in S7 byte order",
        level="L2",
        dialects=SCL,
        codes=frozenset({"E037"}),
        premise="'To be able to access a declared variable with a different data type, you can define views of "
                "the variable ... using the AT keyword ... It inherits all the properties of the variable that it "
                "references; only the data type is new' (S7-SCL V5.3 manual, 8.4). S7 stores multi-byte values "
                "big endian, 'the byte with the most significant bits is saved first' (TIA SCL instructions, "
                "READ_BIG), and an ARRAY OF BOOL fills each byte from bit 0, so element i of the view is bit "
                "(n-1-i DIV 8)*8 + i MOD 8 of an n-byte variable. OSCAT's STIME relies on it: 'stuff the highest "
                "bit into the timer value' writes at_tx[7] of a DWORD view.",
        not_applicable="Views of other types (REAL, STRING, DT, structures), views longer than the variable, and "
                       "views used other than with a constant index (passed as a whole, variable index): "
                       "reported unsupported.",
        equivalence="Each element access reads or writes exactly the bit of the variable that S7 stores under it; "
                    "RuSTy's bit access x.%Xn works on the same variable, so reads see and writes change the "
                    "same storage. The view declaration is kept as a comment.",
        probe="s7-bool-array-view",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        match = _BOOL_VIEW.search(located.diagnostic.message)
        unit = located.unit
        if match is None or unit is None:
            return None
        index = context.indices[file]
        tokens = index.significant
        first, last = index.significant_index(unit.start), index.significant_index(unit.end)
        lo, hi = int(match["lo"]), int(match["hi"])
        for k in range(first + 1, last - 3):
            if not (tokens[k].upper == "AT" and tokens[k - 1].kind == "identifier"
                    and tokens[k + 1].kind == "identifier" and tokens[k + 2].text == ":"):
                continue
            start = k - 1
            end = k + 3
            while end < last and tokens[end].text != ";":
                end += 1
            if not (tokens[start].start <= located.offset <= tokens[end].end):
                continue
            view, base = tokens[start].upper, tokens[k + 1]
            size = BIT_VIEW_BASES.get(_declared_type(tokens, first, last, base.upper) or "")
            if size is None or hi - lo + 1 > size * 8:
                return None
            edits = [Edit(file, tokens[start].start, tokens[end].end,
                          as_comment(index.text[tokens[start].start:tokens[end].end]))]
            for m in range(first, last):
                if m == start or tokens[m].upper != view or not tokens[m].is_word or tokens[m - 1].text == ".":
                    continue
                if not (tokens[m + 1].text == "[" and tokens[m + 3].text == "]" and tokens[m + 2].text.isdigit()):
                    return None
                element = int(tokens[m + 2].text) - lo
                if not 0 <= element <= hi - lo:
                    return None
                bit = (size - 1 - element // 8) * 8 + element % 8
                edits.append(Edit(file, tokens[m].start, tokens[m + 3].end, f"{base.text}.%X{bit}"))
            return Proposal(self.spec, (self.spec.id, file, tokens[start].start), edits,
                            note=f"{tokens[start].text} AT {base.text} ({size} bytes)")
        return None


SHIFT_FUNCTIONS = {"SHL", "SHR", "ROL", "ROR"}


def _integer_value(text: str) -> int | None:
    digits = text.replace("_", "")
    base, _, value = digits.rpartition("#")
    try:
        return int(value, int(base)) if base else int(value)
    except ValueError:
        return None


class ShiftLiteralDword(Rule):
    spec = RuleSpec(
        id="scl-shift-literal-dword",
        version=1,
        title="SHL/SHR/ROL/ROR(IN := 16#FFFFFFFF, ...) with an untyped literal above 16#FFFF -> IN := DWORD#...",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E064"}),
        premise="'The constant is assigned the data type with the smallest value range that will accommodate the "
                "constant without any loss of value' (S7-SCL V5.3 manual, 9.1.2), and for the bit string "
                "functions 'the data type of this input parameter decides the data type of the function value', "
                "IN being BOOL, BYTE, WORD or DWORD (14.3). An untyped literal above 16#FFFF and at most "
                "16#FFFF_FFFF fits only DWORD; the pinned RuSTy cannot resolve the generic IN of a literal.",
        not_applicable="Literals up to 16#FFFF (BOOL, BYTE, WORD or DWORD by the operation they are used in: "
                       "ambiguous), typed literals, and expressions: not rewritten, the error is reported.",
        equivalence="The literal gets the only type S7-SCL can give it, written explicitly.",
        probe="scl-shift-literal-dword",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        line_start, line_end = _line_bounds(index.text, located.offset)
        edits = []
        for k in range(len(tokens) - 6):
            call = tokens[k]
            if call.start < line_start or call.start >= line_end:
                continue
            if call.upper not in SHIFT_FUNCTIONS or tokens[k + 1].text != "(" or tokens[k + 2].upper != "IN" \
                    or tokens[k + 3].text != ":=":
                continue
            m = k + 4
            wrapped = tokens[m].text == "(" and m + 2 < len(tokens) and tokens[m + 2].text == ")"
            literal = tokens[m + 1] if wrapped else tokens[m]
            following = tokens[m + 3] if wrapped else tokens[m + 1]
            # A typed literal starts with a cast prefix, not a number.
            if literal.kind != "number" or following.text not in {",", ")"}:
                continue
            value = _integer_value(literal.text)
            if value is not None and 0xFFFF < value <= 0xFFFFFFFF:
                edits.append(Edit(file, literal.start, literal.start, "DWORD#"))
        if not edits:
            return None
        return Proposal(self.spec, (self.spec.id, file, edits[0].start), edits)


_OPENERS = {"CASE": "END_CASE", "IF": "END_IF", "FOR": "END_FOR", "WHILE": "END_WHILE", "REPEAT": "END_REPEAT"}


def _innermost_block(tokens, first: int, position: int) -> str | None:
    """The control statement (CASE, IF, ...) open at ``position``."""
    stack = []
    closers = {v: k for k, v in _OPENERS.items()}
    for k in range(first, position):
        word = tokens[k].upper if tokens[k].is_word else ""
        if word in _OPENERS:
            stack.append(word)
        elif word in closers and stack and stack[-1] == closers[word]:
            stack.pop()
    return stack[-1] if stack else None


class CaseElseColon(Rule):
    spec = RuleSpec(
        id="scl-case-else-colon",
        version=1,
        title="CASE ... ELSE: statements END_CASE -> ELSE statements",
        level="L1",
        dialects=SCL,
        codes=frozenset({"E007"}),
        premise="The S7-SCL CASE syntax has 'ELSE : Statement section' and the manual's example writes 'ELSE:' "
                "(S7-SCL V5.3 manual, 12.2.4); IEC 61131-3 and the pinned RuSTy write the ELSE branch of a CASE "
                "without the colon.",
        not_applicable="A colon after an ELSE that is not the ELSE branch of the innermost open CASE: not "
                       "rewritten, the error is reported.",
        equivalence="The same ELSE branch without the separator; the colon is kept as a comment.",
        probe="scl-case-else-colon",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        k = index.significant_index(located.offset)
        unit = located.unit
        if unit is None or not 0 < k < len(tokens) or tokens[k].text != ":" or tokens[k - 1].upper != "ELSE":
            return None
        if _innermost_block(tokens, index.significant_index(unit.start), k - 1) != "CASE":
            return None
        return Proposal(self.spec, (self.spec.id, file, tokens[k].start),
                        [Edit(file, tokens[k].start, tokens[k].end, as_comment(":"))])
