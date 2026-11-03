"""Rules for Omron Sysmac Studio (and KEYENCE KV) exports."""

from __future__ import annotations

from ...diagnostics import Located, SourceIndex
from ..base import Edit, Proposal, Rule, RuleContext, RuleSpec

PARAMETER_BLOCKS = {"VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT"}


def en_eno_parameters(context: RuleContext, function: str) -> list[str] | None:
    """Inputs and in-outs (declared spelling) after ``en``/``eno`` of ``FUNCTION
    function`` whose first parameters are the exported ``en : BOOL`` input and
    ``eno : BOOL`` output and which has no other output; None otherwise."""
    for _, index in sorted(context.indices.items()):
        tokens = index.significant
        for i, token in enumerate(tokens[:-2]):
            if token.upper != "FUNCTION" or tokens[i + 1].upper != function:
                continue
            blocks: list[tuple[str, str]] = []  # (block keyword, parameter name)
            k = i + 2
            while k < len(tokens) and tokens[k].upper not in {"END_FUNCTION", "VAR_TEMP", "VAR"}:
                if tokens[k].upper not in PARAMETER_BLOCKS:
                    k += 1
                    continue
                block = tokens[k].upper
                k += 1
                declaration_start = True
                while k < len(tokens) and tokens[k].upper != "END_VAR":
                    if declaration_start:
                        # name [, name] : type ... ;
                        while tokens[k].kind in {"identifier", "keyword"} and tokens[k + 1].text in {":", ","}:
                            blocks.append((block, tokens[k].text))
                            k += 2 if tokens[k + 1].text == "," else 1
                            if tokens[k].text == ":":
                                break
                        declaration_start = False
                    if tokens[k].text == ";":
                        declaration_start = True
                    k += 1
            if [(block, name.upper()) for block, name in blocks[:2]] != [("VAR_INPUT", "EN"), ("VAR_OUTPUT", "ENO")]:
                return None
            if any(block == "VAR_OUTPUT" for block, _ in blocks[2:]):
                return None  # further outputs: the ST call would bind them with =>
            return [name for _, name in blocks[2:]]
    return None


def _calls(index: SourceIndex, first: int, last: int):
    """Every ``name(...)`` in ``[first, last)``: (name, '(', top-level ',' list, ')') indices."""
    tokens = index.significant
    for k in range(first, last - 1):
        if tokens[k].kind != "identifier" or tokens[k + 1].text != "(" or tokens[k - 1].text == ".":
            continue
        depth, commas, j = 0, [], k + 1
        while j < last:
            depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(tokens[j].text, 0)
            if depth == 1 and tokens[j].text == ",":
                commas.append(j)
            if depth == 0:
                break
            j += 1
        if j < last:
            yield k, k + 1, commas, j


VAR_BLOCKS = {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP"}


class EnEnoCall(Rule):
    spec = RuleSpec(
        id="sysmac-en-eno-call",
        version=1,
        title="f(a, b) of a function exported with en/eno -> f(en := TRUE, eno => fresh, p1 := a, p2 := b)",
        level="L2",
        dialects=frozenset({"sysmac"}),
        codes=frozenset({"E032", "E037"}),
        premise="Sysmac functions have the execution condition EN (input, default TRUE) and ENO (output), and "
                "'when you call a FUN instruction from structured text, omit the EN input variable' (NJ/NX-series "
                "Instructions Reference Manual W502, Common Variables). The export declares them as the first "
                "parameters (en : BOOL input, eno : BOOL output), so RuSTy binds the ST call's first argument "
                "to en, and the pinned RuSTy requires every parameter of a function call.",
        not_applicable="Calls that already name parameters, calls whose argument count differs from the "
                       "function's inputs and in-outs after en/eno, and functions with outputs besides eno.",
        equivalence="The formal call passes en := TRUE, the value Sysmac uses when ST omits EN, binds each "
                    "argument to the input or in-out it binds to in Sysmac (declaration order after EN/ENO), and "
                    "receives eno in a fresh BOOL variable of the calling POU that nothing else reads.",
        probe="sysmac-en-eno-call",
    )

    def propose(self, context: RuleContext, file: str, located: Located) -> Proposal | None:
        unit = located.unit
        if unit is None:
            return None
        index = context.indices[file]
        tokens = index.significant
        first, last = index.significant_index(unit.start), index.significant_index(unit.end)
        calls, triggered = [], False
        for name, opening, commas, closing in _calls(index, first, last):
            parameters = en_eno_parameters(context, tokens[name].upper)
            if parameters is None:
                continue
            if any(m + 2 < closing and tokens[m + 1].kind in {"identifier", "keyword"}
                   and tokens[m + 2].text in {":=", "=>"} for m in [opening, *commas]):
                continue  # already a formal call
            arguments = 0 if closing == opening + 1 else len(commas) + 1
            if arguments != len(parameters):
                continue
            calls.append((opening, commas, parameters, arguments))
            triggered |= tokens[name].start <= located.offset <= tokens[closing].end
        if not triggered:
            return None
        fresh = context.fresh_name("ENO", "__SYSMAC")
        # A VAR block for the received eno, before the unit's first variable block.
        insert = next((tokens[k].start for k in range(first + 1, last)
                       if tokens[k].is_word and tokens[k].upper in VAR_BLOCKS), None)
        if insert is None:
            return None
        edits = [Edit(file, insert, insert, f"VAR {fresh} : BOOL; END_VAR\n")]
        for opening, commas, parameters, arguments in calls:
            head = f"en := TRUE, eno => {fresh}" + (f", {parameters[0]} := " if arguments else "")
            edits.append(Edit(file, tokens[opening].end, tokens[opening].end, head))
            for comma, parameter in zip(commas, parameters[1:]):
                edits.append(Edit(file, tokens[comma].end, tokens[comma].end, f" {parameter} :="))
        return Proposal(self.spec, (self.spec.id, file, unit.start), edits, note=f"{len(calls)} calls")
