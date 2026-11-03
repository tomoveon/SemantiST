"""Attribute RuSTy diagnostics to source tokens and classify the construct.

RuSTy reports 1-based lines and 1-based byte columns. :class:`SourceIndex`
maps them to character offsets and to the lossless token at that position;
:func:`classify` names the construct ("keyword_as_identifier:OVERRIDE",
"unknown_word:VAR_STAT", ...) so diagnostics can be counted per construct and
rules can be selected per diagnostic.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from compiler.parser.st import LosslessToken, tokenize_lossless

from .rusty import Diagnostic
from .structure import Unit, split_units


class SourceIndex:
    """Positions, tokens and units of one source text."""

    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize_lossless(text)
        self.starts = [token.start for token in self.tokens]
        self.line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        self._units: list[Unit] | None = None
        self._significant: list[LosslessToken] | None = None
        self._significant_starts: list[int] = []

    @property
    def significant(self) -> list[LosslessToken]:
        if self._significant is None:
            self._significant = [t for t in self.tokens if not t.is_trivia]
            self._significant_starts = [t.start for t in self._significant]
        return self._significant

    def significant_index(self, offset: int) -> int:
        """Index of the first significant token starting at or after ``offset``
        (or containing it)."""
        tokens = self.significant
        index = bisect.bisect_right(self._significant_starts, offset) - 1
        if index >= 0 and tokens[index].end > offset:
            return index
        return index + 1

    @property
    def units(self) -> list[Unit]:
        if self._units is None:
            self._units = split_units(self.text, self.tokens)
        return self._units

    def offset(self, line: int, byte_column: int) -> int:
        """Character offset of a 1-based line and 1-based byte column."""
        if line < 1 or line > len(self.line_starts):
            return len(self.text)
        start = self.line_starts[line - 1]
        end = self.line_starts[line] if line < len(self.line_starts) else len(self.text)
        raw = self.text[start:end].encode("utf-8")
        prefix = raw[: max(byte_column - 1, 0)].decode("utf-8", errors="ignore")
        return start + len(prefix)

    def token_at(self, offset: int) -> LosslessToken | None:
        index = bisect.bisect_right(self.starts, offset) - 1
        if index < 0:
            return None
        token = self.tokens[index]
        # A diagnostic at trivia refers to the next significant token.
        while token.is_trivia and index + 1 < len(self.tokens):
            index += 1
            token = self.tokens[index]
        return token

    def significant_before(self, token: LosslessToken, count: int = 1) -> list[LosslessToken]:
        index = bisect.bisect_left(self.starts, token.start) - 1
        found = []
        while index >= 0 and len(found) < count:
            if not self.tokens[index].is_trivia:
                found.append(self.tokens[index])
            index -= 1
        return found

    def unit_at(self, offset: int) -> Unit | None:
        for unit in self.units:
            if unit.start <= offset < unit.end:
                return unit
        return None


@dataclass
class Located:
    diagnostic: Diagnostic
    offset: int
    token: LosslessToken | None
    construct: str
    unit: Unit | None


_EXPECTED = re.compile(r"Unexpected token: expected (?P<expected>\w+) but found (?P<found>.*)", re.S)
_UNRESOLVED = re.compile(r"Could not resolve reference to (?P<name>[\w.]+)")
_UNKNOWN_TYPE = re.compile(r"Unknown type:?\s*(?P<name>[\w.]+)")
_MISSING_BASE = re.compile(r"Base `(?P<name>[\w.]+)` does not exist")


def _message_shape(message: str) -> str:
    """A message with names and literals replaced, for message-level grouping."""
    first = message.split("\n", 1)[0]
    first = re.sub(r"'[^']*'", "'_'", first)
    first = re.sub(r"`[^`]*`", "`_`", first)
    first = re.sub(r"\b\d+\b", "N", first)
    return first[:120]


def classify(diagnostic: Diagnostic, token: LosslessToken | None, index: SourceIndex | None = None) -> str:
    message = diagnostic.message
    code = diagnostic.code
    if code == "E007":
        match = _EXPECTED.match(message)
        expected = match["expected"] if match else ""
        if token is not None:
            if token.kind == "error":
                char = token.text
                return "lexical:" + ("non_ascii" if not char.isascii() else char)
            if token.kind == "keyword" and expected in {"Identifier", "Literal", "DataTypeDefinition"}:
                return "keyword_as_identifier:" + token.upper
            if token.kind == "identifier":
                previous = index.significant_before(token, 1) if index else []
                if previous and previous[0].upper in {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_GLOBAL",
                                                      "VAR_TEMP", "VAR_EXTERNAL"} and token.upper == "CONSTANT":
                    return "var_section_qualifier:" + previous[0].upper + "_" + token.upper
                return "unexpected_word:" + token.upper if expected.startswith("Keyword") or expected in {
                    "DataTypeDefinition", "Literal", "Identifier"} else "unexpected_word:" + token.upper
            if token.kind in {"keyword", "symbol"}:
                return f"unexpected_{token.kind}:{token.upper}"
            return f"unexpected_{token.kind}"
        return "parse:" + _message_shape(message)
    if code == "E006":
        return "missing_token:" + (token.upper if token is not None and not token.is_trivia else "?")
    if code == "E048":
        return "unresolved_reference"
    if code == "E052":
        return "unknown_type"
    return code + ":" + _message_shape(message)


def referenced_name(diagnostic: Diagnostic) -> str | None:
    for pattern in (_UNRESOLVED, _UNKNOWN_TYPE, _MISSING_BASE):
        match = pattern.search(diagnostic.message)
        if match:
            return match["name"]
    return None


def locate(diagnostic: Diagnostic, index: SourceIndex) -> Located:
    offset = index.offset(diagnostic.line, diagnostic.column) if diagnostic.line else 0
    token = index.token_at(offset) if diagnostic.line else None
    return Located(diagnostic, offset, token, classify(diagnostic, token, index),
                   index.unit_at(offset) if diagnostic.line else None)
