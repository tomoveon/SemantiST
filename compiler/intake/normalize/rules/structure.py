"""Structure rules for object-oriented POUs exported by CODESYS-family tools.

The extractors keep methods, properties and actions inside their function
block, after its body (that is how TwinCAT and PLCopen XML store them). The
pinned RuSTy parses a function block as::

    FUNCTION_BLOCK name [EXTENDS b] [IMPLEMENTS i, ...]
        var-blocks*  (METHOD ... END_METHOD | PROPERTY_GET/SET ... END_PROPERTY)*  body
    END_FUNCTION_BLOCK

and actions as ``ACTION fb.name ... END_ACTION`` outside the block. These rules
move members and rewrite property accessors; the code inside is unchanged.
"""

from __future__ import annotations

from ...diagnostics import Located, SourceIndex
from ...structure import Unit, canonical_keyword
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec

VAR_BLOCKS = {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR_EXTERNAL", "VAR_STAT", "VAR_INST",
              "VARINPUT", "VAROUTPUT", "VARINOUT", "VARTEMP"}
MEMBER_KEYWORDS = {"METHOD", "PROPERTY", "ACTION", "PROPERTY_GET", "PROPERTY_SET", "PROPERTYGET", "PROPERTYSET"}
OWNER_KINDS = {"FUNCTION_BLOCK", "PROGRAM", "CLASS"}


def _word(token) -> str:
    return canonical_keyword(token.text) if token.is_word else token.text


def header_end(tokens, i: int, end_index: int) -> int:
    """Index after ``KIND [modifiers] name [EXTENDS q] [IMPLEMENTS q, ...] [: type]``."""
    i += 1
    while i < end_index and tokens[i].upper in {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL", "ABSTRACT", "FINAL"} \
            and i + 1 < end_index and tokens[i + 1].is_word:
        i += 1
    i += 1  # the name

    def qualified(i: int) -> int:
        i += 1
        while i + 1 < end_index and tokens[i].text == "." and tokens[i + 1].is_word:
            i += 2
        return i

    if i < end_index and tokens[i].upper == "EXTENDS":
        i = qualified(i + 1) if i + 1 < end_index else i + 1
    if i < end_index and tokens[i].upper == "IMPLEMENTS":
        i = qualified(i + 1)
        while i < end_index and tokens[i].text == ",":
            i = qualified(i + 1)
    if i < end_index and tokens[i].text == ":":
        # Return type: words, dots, brackets up to the end of the line.
        line = tokens[i].line
        i += 1
        while i < end_index and tokens[i].line == line:
            i += 1
    return i


def layout(index: SourceIndex, unit: Unit) -> dict:
    """Positions of var blocks, members and body inside an owner unit."""
    tokens = index.significant
    first = index.significant_index(unit.start)
    last = index.significant_index(unit.end - 1)
    # ``last`` is the END keyword (or ';' after it); find the END keyword.
    end_keyword = last
    while end_keyword > first and _word(tokens[end_keyword]) not in {"END_FUNCTION_BLOCK", "END_PROGRAM",
                                                                       "END_CLASS"}:
        end_keyword -= 1
    i = header_end(tokens, first, end_keyword)
    members_before, members_after = [], []
    body_start = None
    member_spans = {m.start: m for m in unit.members}
    while i < end_keyword:
        word = _word(tokens[i])
        if word in VAR_BLOCKS and body_start is None:
            while i < end_keyword and _word(tokens[i]) != "END_VAR":
                i += 1
            i += 1
            continue
        if tokens[i].start in member_spans:
            member = member_spans[tokens[i].start]
            (members_after if body_start is not None else members_before).append(member)
            i = index.significant_index(member.end)
            continue
        if body_start is None:
            body_start = tokens[i].start
        i += 1
    return {
        "body_start": body_start,
        "end_keyword": tokens[end_keyword],
        "members_before": members_before,
        "members_after": members_after,
    }


def owner_of(index: SourceIndex, offset: int) -> Unit | None:
    unit = index.unit_at(offset)
    return unit if unit is not None and unit.kind in OWNER_KINDS else None


def _leading_pragmas(index: SourceIndex, start: int) -> int:
    """Start of the pragmas (and comments) directly in front of ``start``."""
    tokens = index.tokens
    position = next(i for i, t in enumerate(tokens) if t.start == start)
    i = position - 1
    begin = start
    while i >= 0 and tokens[i].kind in {"whitespace", "pragma"}:
        if tokens[i].kind == "pragma":
            begin = tokens[i].start
        i -= 1
    return begin


def _line_start(text: str, offset: int) -> int:
    """Start of the line of ``offset`` if only blanks precede it on that line."""
    start = text.rfind("\n", 0, offset) + 1
    return start if not text[start:offset].strip() else offset


def _line_end(text: str, offset: int) -> int:
    """Position after the newline ending the line of ``offset`` if only blanks follow."""
    end = text.find("\n", offset)
    if end < 0:
        return len(text) if not text[offset:].strip() else offset
    return end + 1 if not text[offset:end].strip() else offset


def _moved_span(index: SourceIndex, start: int, end: int) -> tuple[int, int]:
    return _line_start(index.text, _leading_pragmas(index, start)), _line_end(index.text, end)


class MemberReorder(Rule):
    spec = RuleSpec(
        id="member-reorder",
        version=2,
        title="methods/properties after a function block body -> before the body; actions -> ACTION fb.name "
              "after the block",
        level="L2",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007", "E006"}),
        premise="The export stores members of a function block after its body; in the source dialect a "
                "member is a separate callable that belongs to the block, independent of its position.",
        not_applicable="Members of FUNCTION or INTERFACE units (methods of interfaces are prototypes and are "
                       "already accepted where they are).",
        equivalence="Only the position of complete METHOD/PROPERTY/ACTION declarations changes; the body, "
                     "variables and members are the same text. An action is qualified with its block name, "
                     "which is how RuSTy attaches it to the block (ACTION fb.name).",
        probe="member-reorder",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or not token.is_word or token.upper not in MEMBER_KEYWORDS:
            return None
        index = context.indices[file]
        unit = owner_of(index, token.start)
        if unit is None:
            return None
        shape = layout(index, unit)
        trailing = shape["members_after"]
        if not any(member.start == token.start for member in trailing):
            return None
        tokens = index.significant
        # Step 1: qualify every unqualified trailing action (ACTION name -> ACTION fb.name).
        names = []
        for member in trailing:
            if member.kind != "ACTION":
                continue
            name_index = index.significant_index(member.start) + 1
            if not (name_index + 1 < len(tokens) and tokens[name_index + 1].text == "."):
                names.append(tokens[name_index])
        if names:
            return Proposal(self.spec, (self.spec.id, file, unit.start, "qualify"),
                            [Edit(file, name.start, name.end, unit.name + "." + name.text) for name in names])
        # Step 2: move the first run of trailing members of one kind: methods and
        # properties in front of the body, qualified actions behind the block.
        actions = trailing[0].kind == "ACTION"
        group = [trailing[0]]
        for other in trailing[1:]:
            if (other.kind == "ACTION") != actions:
                break
            group.append(other)
        start, end = _moved_span(index, group[0].start, group[-1].end)
        if actions:
            destination = _line_end(index.text, unit.end)
        else:
            destination = _line_start(index.text, _leading_pragmas(index, shape["body_start"]))
        return Proposal(self.spec, (self.spec.id, file, group[0].start, "move"),
                        [Edit(file, start, end, move_to=destination)])


class PropertyAccessors(Rule):
    spec = RuleSpec(
        id="property-accessors",
        version=1,
        title="PROPERTY name : T  GET ... END_GET  SET ... END_SET  END_PROPERTY -> PROPERTY_GET/PROPERTY_SET",
        level="L1",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007", "E006"}),
        premise="A CODESYS property is a pair of accessor methods; inside GET the property name is the "
                "return value, inside SET it holds the assigned value.",
        not_applicable="Properties without GET or SET blocks, or with declarations between the property header "
                       "and its first accessor (reported as unsupported).",
        equivalence="RuSTy's PROPERTY_GET/PROPERTY_SET declare the same accessors with the same meaning of "
                    "the property name; the accessors' variables and statements are unchanged. Access "
                    "modifiers are kept as comments (visibility has no run-time meaning).",
        probe="property-accessors",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        token = located.token
        if token is None:
            return None
        i = index.significant_index(token.start)
        # The diagnostic can point at the PROPERTY word or just after its header.
        while i > 0 and tokens[i].upper != "PROPERTY" and tokens[i].line >= token.line - 1 and \
                i > index.significant_index(token.start) - 8:
            i -= 1
        if tokens[i].upper != "PROPERTY":
            return None
        start = i
        j = i + 1
        modifiers = []
        while j < len(tokens) and tokens[j].upper in {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL", "ABSTRACT",
                                                       "FINAL", "OVERRIDE"} and tokens[j + 1].is_word:
            modifiers.append(tokens[j])
            j += 1
        name = tokens[j]
        if not name.is_word or j + 1 >= len(tokens) or tokens[j + 1].text != ":":
            return None
        k = j + 2
        type_start = k
        while k < len(tokens) and tokens[k].upper not in {"GET", "SET", "END_PROPERTY", "ENDPROPERTY"} and \
                _word(tokens[k]) not in VAR_BLOCKS:
            k += 1
        if k >= len(tokens) or tokens[k].upper not in {"GET", "SET"}:
            return None
        type_text = index.text[tokens[type_start].start:tokens[k - 1].end]
        edits = [Edit(file, tokens[start].start, tokens[k - 1].end,
                      "(* " + index.text[tokens[start].start:tokens[k - 1].end].replace("*)", "* )") + " *)")]
        accessors = 0
        inside = None
        while k < len(tokens):
            word = tokens[k].upper
            if inside is None and word in {"GET", "SET"}:
                accessors += 1
                inside = "END_" + word
                keyword = "PROPERTY_GET" if word == "GET" else "PROPERTY_SET"
                edits.append(Edit(file, tokens[k].start, tokens[k].end, f"{keyword} {name.text} : {type_text}"))
            elif inside is not None and word == inside:
                inside = None
                edits.append(Edit(file, tokens[k].start, tokens[k].end, "END_PROPERTY"))
            elif inside is None and word in {"END_PROPERTY", "ENDPROPERTY"}:
                edits.append(Edit(file, tokens[k].start, tokens[k].end, "(* " + tokens[k].text + " *)"))
                break
            elif inside is None:
                # Only accessors may follow the property header.
                return None
            k += 1
        if accessors == 0 or k >= len(tokens):
            return None
        return Proposal(self.spec, (self.spec.id, file, tokens[start].start), edits,
                        note=f"property {name.text}" + (" (modifiers " + " ".join(m.text for m in modifiers) + ")"
                                                         if modifiers else ""))


class VarSectionReorder(Rule):
    spec = RuleSpec(
        id="ax-var-section-reorder",
        version=1,
        title="VAR sections between the methods of a CLASS/FUNCTION_BLOCK -> before the first method",
        level="L2",
        dialects=frozenset({"simatic-ax"}),
        codes=frozenset({"E007"}),
        premise="SIMATIC AX allows variable sections anywhere between the members of a class or function "
                "block; every section declares members of the same instance, whatever its position.",
        not_applicable="Sections inside a METHOD (they belong to the method); sections after a function "
                       "block body.",
        equivalence="Only the position of complete VAR ... END_VAR sections changes; each moved section is "
                    "inserted directly before the first member, after the sections moved earlier, so the "
                    "order of all variable declarations of the instance is unchanged.",
        probe="ax-var-section-reorder",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or not token.is_word or _word(token) not in VAR_BLOCKS:
            return None
        index = context.indices[file]
        unit = owner_of(index, token.start)
        if unit is None or not unit.members:
            return None
        first = min(unit.members, key=lambda m: m.start)
        if token.start < first.start or any(m.start <= token.start < m.end for m in unit.members):
            return None
        tokens = index.significant
        i = index.significant_index(token.start)
        while i < len(tokens) and tokens[i].start < unit.end and _word(tokens[i]) != "END_VAR":
            i += 1
        if i >= len(tokens) or _word(tokens[i]) != "END_VAR":
            return None
        start, end = _moved_span(index, token.start, tokens[i].end)
        destination = _line_start(index.text, _leading_pragmas(index, first.start))
        return Proposal(self.spec, (self.spec.id, file, token.start),
                        [Edit(file, start, end, move_to=destination)])
