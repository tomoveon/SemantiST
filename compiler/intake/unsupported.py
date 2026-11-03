"""Constructs reported as unsupported regardless of what the compiler says.

Some constructs are outside the execution model (heap allocation conflicts with
the no-heap premise of design section 1.1; exception handling has no RuSTy
counterpart) or cannot be expressed by simple substitution (UNION, struct
inheritance, VAR_STAT/VAR_INST blocks the storage rules decline). They are
detected on the normalized token stream so the report names them even when
RuSTy happens to accept part of the code.
"""

from __future__ import annotations

from compiler.parser.st import LosslessToken

WORDS = {
    "__NEW": "heap allocation; conflicts with the no-heap execution model (design 1.1)",
    "__DELETE": "heap deallocation; conflicts with the no-heap execution model (design 1.1)",
    "UNION": "UNION overlays members in shared storage; RuSTy has no union type",
    "__TRY": "structured exception handling (__TRY/__CATCH) has no RuSTy counterpart",
    "__CATCH": "structured exception handling (__TRY/__CATCH) has no RuSTy counterpart",
    "__QUERYINTERFACE": "run-time interface query has no RuSTy counterpart",
    "__QUERYPOINTER": "run-time interface query has no RuSTy counterpart",
    "__ISVALIDREF": "reference validity check has no RuSTy counterpart",
    "__VARINFO": "variable meta information has no RuSTy counterpart",
    "CONFIGURATION": "resource/task configuration; the execution model runs single POUs (design 1.1)",
    "VAR_STAT": "static variables (CODESYS VAR_STAT) that var-stat-global does not move: RETAIN/PERSISTENT "
                "statics survive a restart, which the execution model does not have; AT declarations, "
                "redeclared names or THIS^ accesses",
    "VAR_INST": "method instance variables (CODESYS VAR_INST) that var-inst-member does not move: "
                "RETAIN/PERSISTENT, AT declarations, methods outside their function block, redeclared names "
                "or THIS^ accesses",
}


def _type_text(significant: list[LosslessToken], colon: int) -> str:
    j, parts = colon + 1, []
    while j < len(significant) and significant[j].text not in {";", ":="}:
        parts.append(significant[j].upper)
        j += 1
    return " ".join(parts)


def _same_type_alias(significant: list[LosslessToken], at: int) -> bool:
    """``v AT x : T`` where ``x`` is declared earlier with the same type ``T``:
    RuSTy accepts such an alias (a reference initialized to x)."""
    if at + 2 >= len(significant) or significant[at + 2].text != ":":
        return False
    base = significant[at + 1].upper
    view_type = _type_text(significant, at + 2)
    for k in range(at - 2, max(at - 2000, 0), -1):
        token = significant[k]
        if token.upper in {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "END_FUNCTION", "END_FUNCTION_BLOCK",
                           "END_PROGRAM"}:
            return False
        if token.upper == base and significant[k + 1].text == ":" and significant[k - 1].text != ".":
            return _type_text(significant, k + 1) == view_type
    return False


def detect(significant: list[LosslessToken], units) -> list[dict]:
    found = []
    for i, token in enumerate(significant):
        reason = None
        construct = None
        if token.is_word and token.upper in WORDS:
            construct, reason = token.upper, WORDS[token.upper]
        elif token.upper == "TYPE" and i + 2 < len(significant) and significant[i + 2].upper == "EXTENDS":
            construct = "STRUCT EXTENDS"
            reason = "struct inheritance; flattening the base members would change type compatibility"
        elif (token.upper == "AT" and 0 < i < len(significant) - 1 and significant[i - 1].kind == "identifier"
              and significant[i + 1].kind == "identifier" and not _same_type_alias(significant, i)):
            construct = "AT overlay"
            reason = "variable declared AT another variable of a different type shares its storage (S7 view); " \
                     "RuSTy only has aliases of the same type, and s7-bool-array-view handles BOOL views of " \
                     "integers accessed by constant index"
        elif (token.upper == "AT" and i + 1 < len(significant) and significant[i + 1].kind == "direct_access"
              and significant[i + 1].upper in {"%X", "%B", "%W", "%D", "%L"}):
            construct = "AT member offset"
            reason = "STRUCT member placed at an explicit bit/byte offset (SIMATIC AX layout); RuSTy has no " \
                     "member offsets, and dropping them would change the layout"
        elif token.text == "?" and i + 1 < len(significant) and significant[i + 1].text == "=" \
                and significant[i + 1].start == token.end:
            construct = "?= assignment attempt"
            reason = "SIMATIC AX assignment attempt (checked dynamic cast to an interface or reference); no " \
                     "RuSTy counterpart"
        elif token.text in {"&", "|"} and i + 1 < len(significant) and significant[i + 1].text == token.text \
                and significant[i + 1].start == token.end and (i == 0 or significant[i - 1].end != token.start
                                                                or significant[i - 1].text != token.text):
            construct = token.text * 2 + " short-circuit operator"
            reason = "SIMATIC AX short-circuit operator; RuSTy's AND_THEN/OR_ELSE short-circuit too but bind " \
                     "like AND/OR, while AX ranks && and || below OR, so the operands would have to be " \
                     "regrouped; not a simple substitution"
        if reason:
            unit = next((u for u in units if u.start <= token.start < u.end), None)
            found.append({"construct": construct, "line": token.line, "reason": reason,
                          "unit": unit.qualified_name if unit else None})
    return found
