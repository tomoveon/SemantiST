"""L2 rules for CODESYS storage classes that RuSTy has no section for.

Both move a declaration block to the storage RuSTy offers for the same
lifetime and sharing, under fresh names, and rename the references in the
block's scope. No other code can name the fresh variables, so widening their
visibility changes no binding.
"""

from __future__ import annotations

import re

from ...diagnostics import Located, SourceIndex
from ...structure import Unit
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec
from .codesys import as_comment

UNSUPPORTED_MODIFIERS = {"RETAIN", "PERSISTENT", "NON_RETAIN"}
VAR_BLOCKS = {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "VAR_STAT", "VAR_INST", "VAR_EXTERNAL"}


class _Block:
    """``VAR_STAT``/``VAR_INST`` [CONSTANT] declarations ``END_VAR`` [;]."""

    def __init__(self, index: SourceIndex, header: int, end: int, constant: bool, names: list[str]):
        tokens = index.significant
        self.header, self.end, self.constant, self.names = header, end, constant, names
        self.start = tokens[header].start
        self.stop = tokens[end + 1].end if end + 1 < len(tokens) and tokens[end + 1].text == ";" else tokens[end].end
        self.body_start = tokens[header + 2].start if constant else tokens[header + 1].start
        self.body_end = tokens[end].start


def _block_at(index: SourceIndex, word: str, offset: int, first: int, last: int) -> _Block | None | bool:
    """The ``word`` block of ``[first, last)`` that contains ``offset``; False
    when it exists but is outside what the rule supports."""
    tokens = index.significant
    for i in range(first, last):
        if tokens[i].upper != word or not tokens[i].is_word:
            continue
        end = next((j for j in range(i + 1, last) if tokens[j].upper == "END_VAR"), None)
        if end is None:
            return False
        stop = tokens[end + 1].end if end + 1 < len(tokens) and tokens[end + 1].text == ";" else tokens[end].end
        if not (tokens[i].start <= offset <= stop):
            continue
        k = i + 1
        constant = tokens[k].upper == "CONSTANT"
        if constant:
            k += 1
        if tokens[k].upper in UNSUPPORTED_MODIFIERS:
            return False
        names: list[str] = []
        expect_name = True
        depth = 0
        while k < end:
            token = tokens[k]
            if token.text in {"(", "["}:
                depth += 1
            elif token.text in {")", "]"}:
                depth -= 1
            elif depth == 0 and token.text == ";":
                expect_name = True
            elif expect_name:
                if token.kind != "identifier":
                    return False
                if tokens[k + 1].upper == "AT":
                    return False
                names.append(token.text)
                if tokens[k + 1].text == ",":
                    k += 2
                    continue
                if tokens[k + 1].text != ":":
                    return False
                expect_name = False
            k += 1
        if not names or depth != 0:
            return False
        return _Block(index, i, end, constant, names)
    return None


def _rename_in(index: SourceIndex, file: str, lo: int, hi: int, block: _Block, fresh: dict[str, str]) -> list[Edit] | None:
    """Renames of the block's names in token range ``[lo, hi)``, outside the
    block itself; None where an occurrence is not a plain reference."""
    tokens = index.significant
    edits = []
    for k in range(lo, hi):
        if block.header <= k <= block.end:
            continue
        token = tokens[k]
        if not token.is_word or token.upper not in fresh:
            continue
        previous = tokens[k - 1].text
        following = tokens[k + 1].text if k + 1 < len(tokens) else ""
        if following == ":":
            return None  # declared again (shadowing) or a label
        if previous == ".":
            if k >= 2 and tokens[k - 2].text == "^":
                return None  # THIS^.name: not resolved by this rule
            continue  # a member of another variable
        if following in {":=", "=>"} and previous in {"(", ","}:
            continue  # a formal parameter name of a call
        edits.append(Edit(file, token.start, token.end, fresh[token.upper]))
    return edits


def _declarations(index: SourceIndex, block: _Block, fresh: dict[str, str]) -> str:
    """The block's declarations with its names replaced by the fresh ones."""
    pieces, cursor = [], block.body_start
    for token in index.significant[block.header + 1:block.end]:
        if token.is_word and token.upper in fresh and token.start >= block.body_start:
            pieces.append(index.text[cursor:token.start])
            pieces.append(fresh[token.upper])
            cursor = token.end
    pieces.append(index.text[cursor:block.body_end])
    return "".join(pieces).rstrip()


def _base(*parts: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", "_".join(p for p in parts if p))


def _scope(unit: Unit, offset: int):
    return next((m for m in unit.members if m.start <= offset < m.end), None)


class VarStatGlobal(Rule):
    spec = RuleSpec(
        id="var-stat-global",
        version=1,
        title="VAR_STAT [CONSTANT] decls END_VAR -> VAR_GLOBAL [CONSTANT] with fresh names, references renamed",
        level="L2",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007"}),
        premise="CODESYS static variables 'are initialized on download', 'retain their values when the "
                "application leaves the block' and can be accessed 'only from within the namespace where the "
                "variables are declared (like static variables in C)' (CODESYS 3 programming reference, "
                "Variable: VAR_STAT): one storage per declaration, shared by all calls and instances.",
        not_applicable="VAR_STAT RETAIN/PERSISTENT, AT declarations, names declared again in the scope, and "
                       "THIS^.name accesses (reported unsupported).",
        equivalence="A global variable is also initialized once and keeps its value, with one storage for the "
                    "program. Each name gets a fresh global name that no other symbol uses and every reference "
                    "in the declaring POU (or method) is renamed to it, so every reference still denotes the "
                    "same storage; the original block is kept as a comment.",
        probe="var-stat-global",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        unit = located.unit
        if unit is None or unit.kind not in {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM"}:
            return None
        index = context.indices[file]
        member = _scope(unit, located.offset)
        lo = index.significant_index(member.start if member else unit.start)
        hi = index.significant_index(member.end if member else unit.end)
        block = _block_at(index, "VAR_STAT", located.offset, lo, hi)
        if not block:
            return None
        fresh = {name.upper(): context.fresh_name(_base(unit.name, member.name if member else "", name), "__STAT")
                 for name in block.names}
        renames = _rename_in(index, file, lo, hi, block, fresh)
        if renames is None:
            return None
        section = "VAR_GLOBAL CONSTANT" if block.constant else "VAR_GLOBAL"
        declaration = f"{section}\n{_declarations(index, block, fresh)}\nEND_VAR\n"
        edits = [Edit(file, unit.start, unit.start, declaration),
                 Edit(file, block.start, block.stop, as_comment(index.text[block.start:block.stop])), *renames]
        return Proposal(self.spec, (self.spec.id, file, block.start), edits,
                        note=", ".join(f"{k} -> {v}" for k, v in fresh.items()))


class VarInstMember(Rule):
    spec = RuleSpec(
        id="var-inst-member",
        version=1,
        title="METHOD ... VAR_INST [CONSTANT] decls END_VAR -> VAR [CONSTANT] of the owning FB with fresh names",
        level="L2",
        dialects=frozenset({"codesys"}),
        codes=frozenset({"E007"}),
        premise="'CODESYS does not save a VAR_INST method variable in a method stack, but in the stack of the "
                "function block instance. This means that the VAR_INST variable functions like other "
                "variables of the function block instance, and it is not reinitialized each time the method "
                "is called' (CODESYS 3 programming reference, Variable: VAR_INST).",
        not_applicable="VAR_INST RETAIN/PERSISTENT, AT declarations, methods outside their owner, names "
                       "declared again in the method, and THIS^.name accesses (reported unsupported).",
        equivalence="A VAR variable of the function block has one storage per instance, initialized with the "
                    "instance and kept between method calls. Each name gets a fresh name that no other symbol "
                    "uses and every reference in the method is renamed to it, so no other code can reach it; "
                    "the original block is kept as a comment.",
        probe="var-inst-member",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        unit = located.unit
        if unit is None or unit.kind not in {"FUNCTION_BLOCK", "PROGRAM"}:
            return None
        member = _scope(unit, located.offset)
        if member is None or member.kind != "METHOD":
            return None
        index = context.indices[file]
        tokens = index.significant
        lo, hi = index.significant_index(member.start), index.significant_index(member.end)
        block = _block_at(index, "VAR_INST", located.offset, lo, hi)
        if not block:
            return None
        # The owner's first variable block, or its first member: the new VAR
        # block goes in the owner's declaration part either way.
        first_member = min(m.start for m in unit.members)
        owner = range(index.significant_index(unit.start) + 1, index.significant_index(first_member))
        insert = next((tokens[k].start for k in owner if tokens[k].is_word and tokens[k].upper in VAR_BLOCKS),
                      first_member)
        fresh = {name.upper(): context.fresh_name(_base(member.name, name), "__INST") for name in block.names}
        renames = _rename_in(index, file, lo, hi, block, fresh)
        if renames is None:
            return None
        section = "VAR CONSTANT" if block.constant else "VAR"
        declaration = f"{section}\n{_declarations(index, block, fresh)}\nEND_VAR\n"
        edits = [Edit(file, insert, insert, declaration),
                 Edit(file, block.start, block.stop, as_comment(index.text[block.start:block.stop])), *renames]
        return Proposal(self.spec, (self.spec.id, file, block.start), edits,
                        note=", ".join(f"{k} -> {v}" for k, v in fresh.items()))
