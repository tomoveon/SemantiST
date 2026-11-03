"""Fixed representation differences, applied once per dialect family.

Unlike L1/L2 rules these are not triggered by diagnostics: the pinned RuSTy
accepts the code, but with a different meaning. The only rewrite here binds
calls of conversions that RuSTy defines with other semantics to the adapters
of the dialect's compatibility profiles: CODESYS temporal conversions in
milliseconds/seconds (``codesys-time``: TIME_TO_LWORD, ULINT_TO_TIME, ...)
and S7 bit-string conversions (``s7-conversions``: DWORD_TO_REAL, ...). Every edit is recorded like a rule
application.
"""

from __future__ import annotations

from compiler.parser.st import significant, tokenize_lossless

from .base import RuleSpec

CONVERSION_REBIND = RuleSpec(
    id="representation-conversion-rebind",
    version=1,
    title="call of a conversion RuSTy defines with other semantics -> vendor-semantics adapter",
    level="representation",
    dialects=frozenset({"codesys", "siemens-scl"}),
    codes=frozenset(),
    premise="The library is written for the vendor tool, where for example TIME_TO_LWORD(t) yields "
            "milliseconds (CODESYS) or DWORD_TO_REAL copies the bit string (S7); RuSTy's functions of the "
            "same name use nanoseconds or convert the number.",
    not_applicable="Long temporal types (LTIME, LTOD, LDATE, LDT) use nanoseconds on both sides and are not "
                   "rebound.",
    equivalence="The adapter implements the documented vendor semantics on RuSTy's representation; see the "
                "assumptions of the profile (codesys-time, s7-conversions).",
    probe="representation-conversion-rebind",
)


def rebind_edits(text: str, rebind: dict[str, str]) -> list[tuple[int, int, str]]:
    """(start, end, replacement) for every call ``NAME(`` with NAME in ``rebind``."""
    tokens = significant(tokenize_lossless(text))
    edits = []
    for token, following in zip(tokens, tokens[1:]):
        if token.is_word and following.text == "(" and token.upper in rebind:
            edits.append((token.start, token.end, rebind[token.upper]))
    return edits
