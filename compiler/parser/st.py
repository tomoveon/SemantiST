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


# ---------------------------------------------------------------------------
# Lossless tokenization
#
# ``tokenize_lossless`` covers every character of the input: comments, pragmas,
# whitespace and unrecognized characters are tokens too, so joining the token
# texts reproduces the source exactly. Token boundaries follow the pinned RuSTy
# lexer (``src/lexer/tokens.rs`` and ``src/lexer.rs`` at be1de6f), so a rule
# that matches a token sequence sees the same tokens as the compiler: longest
# match first, keyword spellings beat identifiers of equal length, block
# comments nest, and a pragma ends at the first ``}``.

RUSTY_KEYWORDS = frozenset(
    """
    PROGRAM CLASS END_CLASS ENDCLASS EXTENDS IMPLEMENTS INTERFACE END_INTERFACE
    ENDINTERFACE VAR_INPUT VARINPUT VAR_OUTPUT VAROUTPUT VAR VAR_CONFIG ABSTRACT
    FINAL METHOD END_METHOD ENDMETHOD SUPER THIS PROPERTY_GET PROPERTYGET
    PROPERTY_SET PROPERTYSET END_PROPERTY ENDPROPERTY CONSTANT RETAIN NON_RETAIN
    NONRETAIN VAR_TEMP VARTEMP PUBLIC PRIVATE INTERNAL PROTECTED OVERRIDE
    VAR_GLOBAL VARGLOBAL VAR_IN_OUT VARINOUT VAR_EXTERNAL END_VAR ENDVAR
    END_PROGRAM ENDPROGRAM FUNCTION END_FUNCTION ENDFUNCTION FUNCTION_BLOCK
    FUNCTIONBLOCK END_FUNCTION_BLOCK ENDFUNCTIONBLOCK TYPE STRUCT END_TYPE ENDTYPE
    END_STRUCT ENDSTRUCT ACTIONS ACTION END_ACTION ENDACTION END_ACTIONS
    ENDACTIONS IF THEN ELSIF ELSE END_IF ENDIF FOR TO BY DO END_FOR ENDFOR WHILE
    END_WHILE ENDWHILE REPEAT UNTIL END_REPEAT ENDREPEAT CASE RETURN EXIT
    CONTINUE POINTER __FPOINTER REF_TO REFTO ARRAY STRING WSTRING OF AT END_CASE
    ENDCASE MOD AND_THEN AND OR_ELSE OR XOR NOT NULL TRUE FALSE
    """.split()
)
# PROPERTY_GET and PROPERTY_SET are soft keywords usable as identifiers.
RUSTY_SOFT_KEYWORDS = frozenset({"PROPERTY_GET", "PROPERTYGET", "PROPERTY_SET", "PROPERTYSET"})

_TRIVIA = frozenset({"whitespace", "comment", "pragma"})
_PROPERTY_TOKENS = ("{external}", "{ref}", "{constant}", "{sized}", "@EXTERNAL", "@CONSTANT")
_SYMBOLS = ("...", ":=", "=>", "..", "**", "<>", "<=", ">=", ":", ";", "(", ")", "[", "]",
            ",", ".", "+", "-", "*", "/", "=", "<", ">", "&", "^")
_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*")
_TYPED_LITERAL_RES = (
    re.compile(r"(?:LDATE|DATE|LD|D)#\d+-\d+-\d+", re.I),
    re.compile(r"(?:DATE_AND_TIME|DT|LDATE_AND_TIME|LDT)#\d+-\d+-\d+-\d+:\d+(?::\d+(?:\.\d+)?)?", re.I),
    re.compile(r"(?:TIME_OF_DAY|LTIME_OF_DAY|TOD|LTOD)#\d+:\d+(?::\d+(?:\.\d+)?)?", re.I),
    re.compile(r"(?:LTIME|LT|TIME|T)#-?(?:\d+(?:\.\d+)?(?:d|h|ms|m|s|us|ns))+", re.I),
)
_NUMBER_RES = (
    re.compile(r"16#[0-9a-fA-F]+(?:_[0-9a-fA-F]+)*"),
    re.compile(r"8#[0-7]+(?:_[0-7]+)*"),
    re.compile(r"2#[0-1]+(?:_[0-1]+)*"),
    re.compile(r"[0-9]+(?:_[0-9]+)*(?:[eE][+-]?[0-9]+)?"),
)
_STRING_RES = {"'": re.compile(r"'(?:\$.|[^$'])*'"), '"': re.compile(r'"(?:\$.|[^$"])*"')}
_DIRECT_RE = re.compile(r"%[IiQqMmGg][BbDdWwLlXx*]|%[BbDdWwLlXx]")
_WHITESPACE_RE = re.compile(r"[ \t\n\f\r]+")


@dataclass(frozen=True)
class LosslessToken:
    """One token of :func:`tokenize_lossless`.

    ``kind`` is one of ``whitespace``, ``comment``, ``pragma``, ``property``,
    ``keyword``, ``identifier``, ``cast_prefix``, ``typed_literal``,
    ``number``, ``string``, ``wstring``, ``direct_access``, ``symbol`` or
    ``error``. ``start``/``end`` are character offsets; ``line`` and
    ``column`` are 1-based.
    """

    kind: str
    text: str
    start: int
    end: int
    line: int
    column: int

    @property
    def upper(self) -> str:
        return self.text.upper()

    @property
    def is_trivia(self) -> bool:
        return self.kind in _TRIVIA

    @property
    def is_word(self) -> bool:
        return self.kind in {"keyword", "identifier"}


def _block_comment_end(src: str, start: int) -> int | None:
    """Return the end of the nested block comment at ``start`` like RuSTy."""
    opener = src[start]
    close = ")" if opener == "(" else "/"
    unclosed = 1
    prev = " "
    i = start + 2
    while i < len(src):
        c = src[i]
        if c == "*" and prev == opener:
            unclosed += 1
            prev = " "
        elif c == close and prev == "*":
            unclosed -= 1
            prev = c
        else:
            prev = c
        i += 1
        if unclosed == 0:
            return i
    return None


def _scan(src: str, i: int) -> tuple[str, int]:
    """Return the kind and end offset of the token starting at ``i``."""
    c = src[i]
    if c in " \t\n\f\r":
        return "whitespace", _WHITESPACE_RE.match(src, i).end()
    if src.startswith("(*", i) or src.startswith("/*", i):
        end = _block_comment_end(src, i)
        return ("comment", end) if end is not None else ("error", i + 2)
    if src.startswith("//", i):
        end = src.find("\n", i)
        return "comment", len(src) if end < 0 else end
    if c == "{" or c == "@":
        for token in _PROPERTY_TOKENS:
            if src.startswith(token, i):
                return "property", i + len(token)
        if c == "{":
            end = src.find("}", i + 1)
            return ("pragma", end + 1) if end >= 0 else ("error", i + 1)
        return "error", i + 1
    if c in _STRING_RES:
        match = _STRING_RES[c].match(src, i)
        if match:
            return ("string" if c == "'" else "wstring"), match.end()
        return "error", i + 1
    if c == "%":
        match = _DIRECT_RE.match(src, i)
        return ("direct_access", match.end()) if match else ("error", i + 1)
    if c.isascii() and (c.isalpha() or c == "_"):
        word = _IDENT_RE.match(src, i)
        best_kind, best_end = "identifier", word.end()
        if word.group(0).upper() in RUSTY_KEYWORDS:
            best_kind = "keyword"
        upper4 = src[i : i + 4].upper()
        if upper4 == "REF=" and best_end <= i + 4:
            best_kind, best_end = "symbol", i + 4
        if src[i : i + 12].upper() == "REFERENCE TO" and best_end <= i + 12:
            best_kind, best_end = "keyword", i + 12
        if best_end < len(src) and src[best_end] == "#":
            best_kind, best_end = "cast_prefix", best_end + 1
            for pattern in _TYPED_LITERAL_RES:
                match = pattern.match(src, i)
                if match and match.end() > best_end:
                    best_kind, best_end = "typed_literal", match.end()
        return best_kind, best_end
    if c.isascii() and c.isdigit():
        end = max(pattern.match(src, i).end() if pattern.match(src, i) else i for pattern in _NUMBER_RES)
        return "number", end
    for symbol in _SYMBOLS:
        if src.startswith(symbol, i):
            return "symbol", i + len(symbol)
    return "error", i + 1


def tokenize_lossless(src: str) -> list[LosslessToken]:
    """Tokenize ``src`` without dropping any character (see module notes)."""
    tokens: list[LosslessToken] = []
    i = 0
    line = 1
    line_start = 0
    while i < len(src):
        kind, end = _scan(src, i)
        text = src[i:end]
        tokens.append(LosslessToken(kind, text, i, end, line, i - line_start + 1))
        newlines = text.count("\n")
        if newlines:
            line += newlines
            line_start = i + text.rfind("\n") + 1
        i = end
    return tokens


def significant(tokens: list[LosslessToken]) -> list[LosslessToken]:
    """Tokens the RuSTy parser sees (no whitespace, comments or pragmas)."""
    return [token for token in tokens if not token.is_trivia]
