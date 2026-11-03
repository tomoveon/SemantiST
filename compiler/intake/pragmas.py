"""Inventory and classification of ``{...}`` pragmas.

The pinned RuSTy lexer skips every pragma (``parse_pragma`` in src/lexer.rs),
so a pragma that changes layout or behaviour in the vendor tool is silently
lost. Each pragma is classified:

* ``ignorable`` -- documentation, IDE, monitoring, tooling, visibility or
  compile-time-only checks; no run-time effect;
* ``layout`` -- changes memory layout (``pack_mode``);
* ``semantic`` -- changes behaviour or initialization (``call_after_init``,
  ``instance-path``, ``to_string``, MATIEC embedded C code, ...);
* ``test_framework`` -- marks unit tests (SIMATIC AX ``{Test}``), relevant
  only for running the library's own tests;
* ``unclassified`` -- not seen before; treated like ``semantic``.

A unit that contains a ``layout``, ``semantic`` or ``unclassified`` pragma is
reported as unsupported.
"""

from __future__ import annotations

import re

from compiler.parser.st import LosslessToken

IGNORABLE_ATTRIBUTES = {
    "addproperty", "hide", "tcrpcenable", "generatedusingterminal", "qualified_only", "clr", "strict", "wpf",
    "obsolete", "reflection", "monitoring", "conditionalshow", "conditionalshow_all_locals", "no-analysis",
    "tcgenerated", "const_non_replaced", "no_assign", "no_explicit_call", "signature_flag", "register_in_runtime",
    "linkalways", "call_after_online_change_slot", "qlik_load", "qlik_ref_value_field", "qlik_entity",
    "qlik_value_field", "qlik_ignore", "displaymode", "symbol", "enable_dynamic_creation",
    "no_virtual_actions", "tcnocontextmenu", "tcdisplaytypeguid", "tcdisplayscale", "tclinkto",
}
LAYOUT_ATTRIBUTES = {"pack_mode", "align"}
SEMANTIC_ATTRIBUTES = {
    "call_after_init": "method called automatically after initialization",
    "instance-path": "string variable initialized with the instance path",
    "noinit": "variable excluded from implicit initialization",
    "to_string": "enum values converted to their names by TO_STRING",
    "no_check": "disables implicit checks for the POU",
    "check_div_int64": "enables implicit division checks",
    "init_on_onlchange": "re-initialization on online change",
}
IGNORABLE_WORDS = {
    "region", "endregion", "s7_optimized_access", "s7_read_back", "s7_dynamic", "s7_setpoint", "s7_m_c",
    "instructionname", "externalaccessible", "warning", "info", "error", "id", "customgroupreference",
    "customgroupdefinition", "flow", "noflow", "s7.extern", "#ix-set", "#ix-attr", "#ix-prop", "#ix-generic",
    "library", "axcode", "pdd", "systemvariable", "event_task", "event_task_info", "messageguid",
}
TEST_WORDS = {"test", "testfixture", "testsetup", "testteardown", "fixturesetup", "fixtureteardown"}
LAYOUT_WORDS = {"pack_mode"}

_ATTRIBUTE = re.compile(r"\{\s*attribute\s+'?([\w.\-]+)", re.I)
_WORD = re.compile(r"\{\s*(#?[\w.\-]+)")


def classify(text: str) -> tuple[str, str, str]:
    """(class, key, reason) of one pragma's text."""
    match = _ATTRIBUTE.match(text)
    if match:
        key = match.group(1).lower()
        if key in IGNORABLE_ATTRIBUTES:
            return "ignorable", "attribute " + key, "tooling or compile-time attribute"
        if key in LAYOUT_ATTRIBUTES:
            return "layout", "attribute " + key, "changes memory layout"
        if key in SEMANTIC_ATTRIBUTES:
            return "semantic", "attribute " + key, SEMANTIC_ATTRIBUTES[key]
        return "unclassified", "attribute " + key, "attribute not in the classification table"
    body = text[1:].strip()
    if body.upper().startswith("__SET_VAR") or body.lower().startswith(("extern", "if ", "{")) or "data__->" in body:
        return "semantic", "embedded C", "MATIEC/OpenPLC embedded C code; RuSTy drops it"
    match = _WORD.match(text)
    key = match.group(1).lower().rstrip("(") if match else body[:20].lower()
    if key in TEST_WORDS or key.startswith("test("):
        return "test_framework", key, "unit-test framework marker"
    if key in IGNORABLE_WORDS or key.split("(")[0] in IGNORABLE_WORDS:
        return "ignorable", key, "tooling, documentation or engineering metadata"
    if key in LAYOUT_WORDS:
        return "layout", key, "changes memory layout"
    return "unclassified", key, "pragma not in the classification table"


def inventory(tokens: list[LosslessToken], units) -> list[dict]:
    """Every pragma with its class and enclosing unit."""
    result = []
    for token in tokens:
        if token.kind != "pragma":
            continue
        kind, key, reason = classify(token.text)
        unit = next((u for u in units if u.start <= token.start < u.end), None)
        # A pragma directly in front of a unit belongs to that unit.
        if unit is None:
            unit = next((u for u in units if u.start >= token.end), None)
        result.append({"line": token.line, "class": kind, "key": key, "reason": reason,
                       "unit": unit.qualified_name if unit else None, "text": token.text[:120]})
    return result
