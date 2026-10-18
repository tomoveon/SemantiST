"""Structured Text lexer for declaration-text extraction.

Types, sizes and layouts are not inferred here: the harness ABI comes from the
compiler (``compiler.harness.abi``). This module only tokenizes ST so that
benchmark tooling can copy declarations verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class StToken:
    kind: str
    text: str
    line: int = 1
    column: int = 1
    index: int = 0

    @property
    def upper(self) -> str:
        return self.text.upper()


def strip_comments(src: str) -> str:
    """Remove ST comments while preserving newlines for useful locations."""
    out: list[str] = []
    i = 0
    while i < len(src):
        if src.startswith("(*", i):
            end = src.find("*)", i + 2)
            if end == -1:
                out.extend("\n" for _ in src[i:] if _ == "\n")
                break
            out.extend("\n" for _ in src[i : end + 2] if _ == "\n")
            i = end + 2
            continue
        if src.startswith("//", i):
            end = src.find("\n", i + 2)
            if end == -1:
                break
            out.append("\n")
            i = end + 1
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def normalize_var_input_constant(src: str) -> str:
    return re.sub(
        r"(?i)\bVAR_INPUT\s*\(\*\s*CONSTANT\s*\*\)",
        "VAR_INPUT CONSTANT",
        src,
    )


def is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def is_ident_continue(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


class StLexer:
    """Tokenizes the declaration-oriented ST subset without parsing it."""

    def __init__(self, src: str):
        self.src = strip_comments(normalize_var_input_constant(src))
        self.i = 0
        self.line = 1
        self.column = 1
        self.tokens: list[StToken] = []

    def current(self) -> str:
        return self.src[self.i]

    def advance(self, count: int = 1) -> str:
        text = self.src[self.i : self.i + count]
        for ch in text:
            if ch == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
        self.i += count
        return text

    def emit(self, kind: str, text: str, line: int, column: int) -> None:
        self.tokens.append(StToken(kind, text, line, column, len(self.tokens)))

    def tokenize(self) -> list[StToken]:
        while self.i < len(self.src):
            ch = self.current()
            if ch.isspace():
                self.advance()
                continue

            line, column = self.line, self.column
            if self.src.startswith(":=", self.i) or self.src.startswith("..", self.i):
                self.emit("symbol", self.advance(2), line, column)
                continue
            if self.src.startswith("=>", self.i):
                self.emit("symbol", self.advance(2), line, column)
                continue

            if ch in "[](),;:^#+-*/=<>.":
                self.emit("symbol", self.advance(), line, column)
                continue

            if ch == "%":
                start = self.i
                while self.i < len(self.src) and not self.current().isspace():
                    if self.current() in "[](),;:":
                        break
                    self.advance()
                self.emit("address", self.src[start:self.i], line, column)
                continue

            if ch == "'":
                text = self.read_single_quoted()
                self.emit("string", text, line, column)
                continue

            if ch.isdigit():
                start = self.i
                self.advance()
                while self.i < len(self.src):
                    ch = self.current()
                    if ch.isalnum() or ch == "_":
                        self.advance()
                    else:
                        break
                self.emit("integer", self.src[start:self.i], line, column)
                continue

            if is_ident_start(ch):
                start = self.i
                self.advance()
                while self.i < len(self.src) and is_ident_continue(self.current()):
                    self.advance()
                self.emit("identifier", self.src[start:self.i], line, column)
                continue

            self.emit("symbol", self.advance(), line, column)

        self.tokens.append(StToken("eof", "", self.line, self.column, len(self.tokens)))
        return self.tokens

    def read_single_quoted(self) -> str:
        start = self.i
        self.advance()
        while self.i < len(self.src):
            ch = self.current()
            self.advance()
            if ch == "'":
                if self.i < len(self.src) and self.current() == "'":
                    self.advance()
                    continue
                break
        return self.src[start:self.i]


def tokenize_st(src: str) -> list[StToken]:
    return StLexer(src).tokenize()
