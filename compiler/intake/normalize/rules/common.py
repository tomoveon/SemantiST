"""L1 rules shared by several dialect families."""

from __future__ import annotations

import re

from ...diagnostics import Located
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec

ACCESS_MODIFIERS = {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL"}


def rename_everywhere(context: RuleContext, word: str, new_name: str) -> list[Edit]:
    """Edits renaming every word token spelled ``word`` (any case) in all files.

    Comments, strings and pragmas are tokens of their own and are not touched.
    """
    edits = []
    for name, index in sorted(context.indices.items()):
        for token in index.significant:
            if token.is_word and token.upper == word:
                # Keep the case of each occurrence; identifiers are case-insensitive.
                suffix = new_name[len(word):]
                edits.append(Edit(name, token.start, token.end, token.text + suffix))
    return edits


def renamed(context: RuleContext, word: str) -> str:
    return context.fresh_name(word, "__")


class VarBlockConstant(Rule):
    spec = RuleSpec(
        id="var-block-constant",
        version=1,
        title="VAR_INPUT CONSTANT / VAR_IN_OUT CONSTANT -> VAR_INPUT / VAR_IN_OUT",
        level="L1",
        dialects=frozenset({"codesys", "pcworx", "sysmac", "iec", "br", "gxworks", "control-expert", "plcnext"}),
        codes=frozenset({"E034"}),
        premise="In the source dialect CONSTANT on an input or in-out block only forbids writing the "
                "parameter inside the POU; a program accepted by the vendor tool therefore never writes it.",
        not_applicable="VAR_IN_OUT CONSTANT called with a literal or expression argument: without CONSTANT the "
                       "pinned RuSTy rejects that call (the remaining error is reported, nothing is guessed).",
        equivalence="Removing a write prohibition does not change the behaviour of a program that does not "
                    "write; the qualifier is kept as a comment.",
        probe="var-block-constant",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(located.offset)
        if i + 1 >= len(tokens):
            return None
        block, qualifier = tokens[i], tokens[i + 1]
        if block.upper not in {"VAR_INPUT", "VAR_IN_OUT", "VARINPUT", "VARINOUT"} or qualifier.upper != "CONSTANT":
            return None
        return Proposal(self.spec, (self.spec.id, file, qualifier.start),
                        [Edit(file, qualifier.start, qualifier.end, "(* " + qualifier.text + " *)")])


class ArrayInitializerBrackets(Rule):
    spec = RuleSpec(
        id="array-initializer-brackets",
        version=1,
        title="ARRAY ... := a, b, c  ->  ARRAY ... := [a, b, c]",
        level="L1",
        dialects=frozenset({"codesys", "pcworx", "sysmac", "iec", "gxworks", "br", "control-expert", "plcnext", "siemens-scl"}),
        codes=frozenset({"E043"}),
        premise="The diagnostic marks an array variable whose initial value is a comma-separated list "
                "without brackets; in the source dialect that list initializes the elements in order.",
        not_applicable="Initializers RuSTy reports with E043 for other reasons (a scalar assigned to an "
                       "array) are not lists and are left unchanged.",
        equivalence="RuSTy's bracketed array literal initializes the same elements in the same order; "
                    "repetition factors n(v) keep their meaning inside the brackets.",
        probe="array-initializer-brackets",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        d = located.diagnostic
        start = index.offset(d.line, d.column)
        end = index.offset(d.end_line, d.end_column)
        tokens = index.significant
        i = index.significant_index(start)
        if i == 0 or tokens[i - 1].text != ":=" or tokens[i].text == "[":
            return None
        # The list must be at the top level a comma separated sequence.
        depth = 0
        commas = 0
        j = i
        while j < len(tokens) and tokens[j].start < end:
            text = tokens[j].text
            if text in "([":
                depth += 1
            elif text in ")]":
                depth -= 1
            elif text == "," and depth == 0:
                commas += 1
            j += 1
        if commas == 0 or depth != 0:
            return None
        last = tokens[j - 1]
        return Proposal(self.spec, (self.spec.id, file, start),
                        [Edit(file, tokens[i].start, tokens[i].start, "["), Edit(file, last.end, last.end, "]")])


class KeywordIdentifierRename(Rule):
    """RuSTy keywords that the source dialect treats as ordinary identifiers."""

    spec = RuleSpec(
        id="keyword-identifier-rename",
        version=2,
        title="identifier spelled like a RuSTy keyword -> consistently renamed identifier",
        level="L1",
        dialects=frozenset({"codesys", "siemens-scl", "pcworx", "sysmac", "iec", "gxworks", "br",
                            "control-expert", "plcnext", "simatic-ax"}),
        codes=frozenset({"E007"}),
        premise="The word is listed in the dialect profile as an ordinary identifier of the source dialect "
                "(for example OVERRIDE in CODESYS), and the diagnostic reports it where RuSTy expected an "
                "identifier or literal.",
        not_applicable="Dialects in which the word is a keyword (OVERRIDE in SIMATIC AX); words that collide "
                       "with a name in another library of the dependency closure are renamed with a numbered "
                       "suffix instead.",
        equivalence="Every occurrence of the identifier in the compiled project is renamed to the same fresh "
                    "name that no other symbol, standard-library or compatibility name uses, so every "
                    "reference keeps its binding. The rename is recorded for mapping results back.",
        probe="keyword-identifier-rename",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None or token.kind != "keyword":
            return None
        word = token.upper
        if word not in set(context.dialect.get("identifier_words", [])):
            return None
        new_name = renamed(context, word)
        return Proposal(self.spec, (self.spec.id, word), rename_everywhere(context, word, new_name),
                        note=f"{word} -> {new_name}", renames={word: new_name})


_DUPLICATE = re.compile(r"^(?P<name>[\w]+): (?:Duplicate symbol|Ambiguous callable symbol|Ambiguous datatype"
                        r"|Ambiguous global variable)\.", re.I)
_BUILTIN = re.compile(r"^(?P<name>[\w]+) can not be used as a name because it is a built-in datatype")


class StandardNameCollisionRename(Rule):
    spec = RuleSpec(
        id="standard-name-collision-rename",
        version=1,
        title="library POU or type named like a RuSTy standard-library symbol -> consistently renamed",
        level="L1",
        dialects=frozenset({"codesys", "siemens-scl", "pcworx", "sysmac", "iec", "gxworks", "br",
                            "control-expert", "plcnext", "simatic-ax"}),
        codes=frozenset({"E004"}),
        premise="The library itself defines the symbol (OSCAT ROUND with two parameters, MATIEC's own CTU), "
                "and the pinned RuSTy also provides a symbol of that name. In the vendor tool the library's "
                "definition is the one its own code refers to.",
        not_applicable="Duplicate definitions inside the library itself (no standard-library symbol of that "
                       "name): reported as unsupported.",
        equivalence="All occurrences in the project are renamed to one fresh name, so the library's calls keep "
                    "binding to the library's definition instead of switching to RuSTy's.",
        probe="standard-name-collision-rename",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        message = located.diagnostic.message
        match = _DUPLICATE.match(message) or _BUILTIN.match(message)
        if not match:
            return None
        word = match["name"].upper()
        if word not in context.standard_names and not _BUILTIN.match(message):
            return None
        # The diagnostic must point at the library's own definition.
        if located.token is None or located.token.upper != word:
            return None
        new_name = renamed(context, word)
        return Proposal(self.spec, (self.spec.id, word), rename_everywhere(context, word, new_name),
                        note=f"{word} -> {new_name}", renames={word: new_name})


class PouAccessModifier(Rule):
    spec = RuleSpec(
        id="pou-access-modifier",
        version=2,
        title="FUNCTION_BLOCK|TYPE PUBLIC|INTERNAL name -> FUNCTION_BLOCK|TYPE name",
        level="L1",
        dialects=frozenset({"codesys", "simatic-ax"}),
        codes=frozenset({"E006", "E007"}),
        premise="An access modifier directly after FUNCTION_BLOCK/FUNCTION/CLASS/INTERFACE/PROGRAM/TYPE controls "
                "which other libraries may refer to the POU or type; it has no run-time meaning.",
        not_applicable="Modifiers on methods and properties (RuSTy accepts those).",
        equivalence="Visibility only restricts which programs compile; the POU behaves identically. The "
                    "modifier is kept as a comment.",
        probe="pou-access-modifier",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        token = located.token
        if token is None:
            return None
        index = context.indices[file]
        tokens = index.significant
        i = index.significant_index(token.start)
        if token.upper not in ACCESS_MODIFIERS:
            # TYPE PUBLIC name : ... is reported at the type name.
            if i < 2 or tokens[i - 1].upper not in ACCESS_MODIFIERS or tokens[i - 2].upper != "TYPE":
                return None
            i -= 1
            token = tokens[i]
        if i == 0 or tokens[i - 1].upper not in {"FUNCTION_BLOCK", "FUNCTIONBLOCK", "FUNCTION", "CLASS",
                                                  "INTERFACE", "PROGRAM", "TYPE"}:
            return None
        return Proposal(self.spec, (self.spec.id, file, token.start),
                        [Edit(file, token.start, token.end, "(* " + token.text + " *)")])


class ElseIfKeyword(Rule):
    spec = RuleSpec(
        id="elseif-keyword",
        version=1,
        title="IF ... ELSEIF cond THEN ... -> IF ... ELSIF cond THEN ...",
        level="L1",
        dialects=frozenset({"codesys", "pcworx", "sysmac", "iec", "br", "gxworks", "control-expert", "plcnext"}),
        codes=frozenset({"E007"}),
        premise="ELSEIF is the else-if clause of an IF statement: 'Either ELSIF or ELSEIF can be used for the "
                "else if clause in an IF statement' (Emerson PACSystems GFK-2950N); LS XGI (instruction manual "
                "14.4.2) and B&R (Automation Studio ST reference, IF statement) describe the clause as ELSEIF. "
                "RuSTy parses the word as an identifier and reports the token after it.",
        not_applicable="ELSEIF that is not followed by a condition and THEN before the next ';', or that does "
                       "not start a clause (after ';', THEN or ELSE); ELSEIF declared as a name.",
        equivalence="ELSIF is the IEC 61131-3 spelling of the same clause; the condition and statements are "
                    "unchanged.",
        probe="elseif-keyword",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        index = context.indices[file]
        unit = located.unit
        if unit is None:
            return None
        tokens = index.significant
        first, last = index.significant_index(unit.start), index.significant_index(unit.end)
        k = index.significant_index(located.offset)
        if not (first < k < last and tokens[k - 1].upper == "ELSEIF"):
            return None
        edits = []
        for i in range(first + 1, last - 1):
            token = tokens[i]
            if token.upper != "ELSEIF" or not token.is_word:
                continue
            if tokens[i + 1].text in {":", ":=", ".", ","}:
                return None  # a declaration or use of a name ELSEIF
            if tokens[i - 1].text != ";" and tokens[i - 1].upper not in {"THEN", "ELSE"}:
                return None
            j = i + 1
            while j < last and tokens[j].text != ";" and tokens[j].upper != "THEN":
                j += 1
            if j >= last or tokens[j].upper != "THEN":
                return None
            edits.append(Edit(file, token.start, token.end, "ELSIF"))
        return Proposal(self.spec, (self.spec.id, file, unit.start), edits) if edits else None
