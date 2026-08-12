"""ST type models and Structured Text signature/declaration parsing."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


DEFAULT_STRING_LENGTH = int(os.environ.get("SEMANTIST_STRING_LENGTH", "250"))
DEFAULT_POINTER_BYTES = int(os.environ.get("SEMANTIST_POINTER_BYTES", "4096"))
MAX_ARRAY_ELEMENTS = int(os.environ.get("SEMANTIST_MAX_ARRAY_ELEMENTS", "65536"))

SCALAR_TYPES = {
    "BOOL": ("uint8_t", 1, "read_u8"),
    "SINT": ("int8_t", 1, "read_i8"),
    "USINT": ("uint8_t", 1, "read_u8"),
    "BYTE": ("uint8_t", 1, "read_u8"),
    "CHAR": ("uint8_t", 1, "read_u8"),
    "INT": ("int16_t", 2, "read_i16_le"),
    "UINT": ("uint16_t", 2, "read_u16_le"),
    "WORD": ("uint16_t", 2, "read_u16_le"),
    "DINT": ("int32_t", 4, "read_i32_le"),
    "UDINT": ("uint32_t", 4, "read_u32_le"),
    "DWORD": ("uint32_t", 4, "read_u32_le"),
    "LINT": ("int64_t", 8, "read_i64_le"),
    "ULINT": ("uint64_t", 8, "read_u64_le"),
    "LWORD": ("uint64_t", 8, "read_u64_le"),
    "REAL": ("float", 4, "read_f32_le"),
    "LREAL": ("double", 8, "read_f64_le"),
    "TIME": ("int64_t", 8, "read_i64_le"),
    "LTIME": ("int64_t", 8, "read_i64_le"),
    "DATE": ("int64_t", 8, "read_i64_le"),
    "LDATE": ("int64_t", 8, "read_i64_le"),
    "DATE_AND_TIME": ("int64_t", 8, "read_i64_le"),
    "DT": ("int64_t", 8, "read_i64_le"),
    "LDATE_AND_TIME": ("int64_t", 8, "read_i64_le"),
    "LDT": ("int64_t", 8, "read_i64_le"),
    "TIME_OF_DAY": ("int64_t", 8, "read_i64_le"),
    "TOD": ("int64_t", 8, "read_i64_le"),
    "LTIME_OF_DAY": ("int64_t", 8, "read_i64_le"),
    "LTOD": ("int64_t", 8, "read_i64_le"),
    "WCHAR": ("uint16_t", 2, "read_u16_le"),
}

VAR_BLOCK_STARTS = {
    "VAR",
    "VAR_INPUT",
    "VAR_IN_OUT",
    "VAR_OUTPUT",
    "VAR_TEMP",
}
VAR_BLOCK_MODIFIERS = {
    "CONSTANT",
    "RETAIN",
    "PERSISTENT",
    "NON_RETAIN",
}


@dataclass
class TypeSpec:
    kind: str
    name: str
    c_type: str | None = None
    size: int = 0
    reader: str | None = None
    length: int | None = None
    element: "TypeSpec | None" = None
    count: int | None = None

    def fuzz_size(self) -> int:
        if self.kind == "scalar":
            return self.size
        if self.kind == "string":
            return 1 + self.length
        if self.kind == "array":
            return self.count * self.element.fuzz_size()
        if self.kind == "pointer":
            return self.element.fuzz_size() if self.element else DEFAULT_POINTER_BYTES
        return self.size

    def c_arg_type(self) -> str:
        if self.kind == "scalar":
            return self.c_type
        return "void *"

    def flat_c_type(self) -> str:
        if self.kind == "scalar":
            return self.c_type
        if self.kind == "string":
            return "uint8_t"
        if self.kind == "array":
            return self.element.flat_c_type()
        if self.kind == "pointer":
            return self.element.flat_c_type() if self.element else "uint8_t"
        return "uint8_t"

    def flat_count(self) -> int:
        if self.kind == "scalar":
            return 1
        if self.kind == "string":
            return self.length + 1
        if self.kind == "array":
            return self.count * self.element.flat_count()
        if self.kind == "pointer":
            return self.element.flat_count() if self.element else DEFAULT_POINTER_BYTES
        return self.size


@dataclass
class Param:
    name: str
    spec: TypeSpec
    offset: int
    block_kind: str | None = None
    constant: bool = False


@dataclass
class StTarget:
    kind: str
    name: str
    ret_spec: TypeSpec | None
    params: list[Param]
    total_size: int
    outputs: list[Param] | None = None
    state_fields: list[Param] | None = None
    ordinary_inputs: list[Param] | None = None
    constant_inputs: list[Param] | None = None
    inout_fields: list[Param] | None = None
    persistent_fields: list[Param] | None = None
    temp_fields: list[Param] | None = None
    constant_fields: list[Param] | None = None
    snapshot_fields: list[Param] | None = None


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


@dataclass
class StVarDecl:
    names: list[str]
    spec: TypeSpec
    address: str | None = None
    constant_value: int | None = None


@dataclass
class StVarBlock:
    kind: str
    declarations: list[StVarDecl]


@dataclass
class StFunction:
    name: str
    ret_spec: TypeSpec
    var_blocks: list[StVarBlock]


@dataclass
class StFunctionBlock:
    name: str
    var_blocks: list[StVarBlock]


@dataclass
class StSourceAst:
    functions: list[StFunction]
    function_blocks: list[StFunctionBlock]

    def function(self, target: str) -> StFunction:
        for function in self.functions:
            if function.name.upper() == target.upper():
                return function
        known = ", ".join(function.name for function in self.functions) or "<none>"
        raise ValueError(f"function {target!r} not found in ST source; known functions: {known}")

    def function_block(self, target: str) -> StFunctionBlock:
        for block in self.function_blocks:
            if block.name.upper() == target.upper():
                return block
        known = ", ".join(block.name for block in self.function_blocks) or "<none>"
        raise ValueError(
            f"function block {target!r} not found in ST source; known function blocks: {known}"
        )


class StParseError(ValueError):
    pass


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


def normalize_type(raw: str) -> str:
    return " ".join(raw.strip().split()).upper()


def eval_bound(expr: str, fallback: int) -> int:
    expr = expr.strip().replace("_", "")
    if expr:
        sign = expr[0] if expr[0] in "+-" else ""
        digits = expr[1:] if sign else expr
        if digits.isdigit():
            return int(expr)
    return fallback


def tokens_to_text(tokens: list[StToken]) -> str:
    if not tokens:
        return ""
    out = ""
    tight_left = {"[", "(", "^", "#"}
    tight_right = {"]", ")", ",", ";", ":", "..", "#"}
    for token in tokens:
        text = token.text
        if not out or text in tight_right or out[-1] in tight_left:
            out += text
        else:
            out += " " + text
    return out


def _integer_literal_value(text: str) -> int | None:
    raw = text.replace("_", "")
    if raw.startswith(("+", "-")):
        sign = -1 if raw[0] == "-" else 1
        raw = raw[1:]
    else:
        sign = 1
    if raw.isdigit():
        return sign * int(raw)
    return None


def _skip_initializer_value(tokens: list[StToken], pos: int) -> tuple[int | None, int]:
    if pos < len(tokens) and tokens[pos].text == ":=":
        pos += 1
    sign = 1
    if pos < len(tokens) and tokens[pos].text in ("+", "-"):
        sign = -1 if tokens[pos].text == "-" else 1
        pos += 1
    value = None
    if pos < len(tokens) and tokens[pos].kind == "integer":
        parsed = _integer_literal_value(tokens[pos].text)
        value = None if parsed is None else sign * parsed
        pos += 1
    depth = 0
    while pos < len(tokens):
        token = tokens[pos]
        if token.text in ("[", "("):
            depth += 1
        elif token.text in ("]", ")"):
            depth = max(0, depth - 1)
        elif token.text == ";" and depth == 0:
            pos += 1
            break
        pos += 1
    return value, pos


def integer_constants(src: str) -> dict[str, int]:
    """Collect simple integer constants used by array bounds."""
    tokens = tokenize_st(src)
    constants: dict[str, int] = {}
    pos = 0
    while pos < len(tokens) and tokens[pos].kind != "eof":
        if tokens[pos].upper not in VAR_BLOCK_STARTS:
            pos += 1
            continue

        pos += 1
        modifiers: list[str] = []
        while tokens[pos].upper in VAR_BLOCK_MODIFIERS:
            modifiers.append(tokens[pos].upper)
            pos += 1
        block_is_constant = "CONSTANT" in modifiers
        body_start = pos
        while tokens[pos].kind != "eof" and tokens[pos].upper != "END_VAR":
            pos += 1
        body = tokens[body_start:pos]
        if tokens[pos].upper == "END_VAR":
            pos += 1
        if not block_is_constant:
            continue

        decl_pos = 0
        while decl_pos < len(body):
            names: list[str] = []
            while decl_pos < len(body) and body[decl_pos].kind == "identifier":
                names.append(body[decl_pos].text)
                decl_pos += 1
                if decl_pos < len(body) and body[decl_pos].text == ",":
                    decl_pos += 1
                    continue
                break
            if not names or decl_pos >= len(body) or body[decl_pos].text != ":":
                decl_pos += 1
                continue
            decl_pos += 1
            while decl_pos < len(body) and body[decl_pos].text not in (":=", ";"):
                decl_pos += 1
            value = None
            if decl_pos < len(body) and body[decl_pos].text == ":=":
                value, decl_pos = _skip_initializer_value(body, decl_pos)
            else:
                while decl_pos < len(body) and body[decl_pos].text != ";":
                    decl_pos += 1
                if decl_pos < len(body):
                    decl_pos += 1
            if value is not None:
                for name in names:
                    constants[name.upper()] = value
    return constants


class TokenStream:
    def __init__(self, tokens: list[StToken], source_name: str = "<st>"):
        self.tokens = tokens
        self.pos = 0
        self.source_name = source_name

    def peek(self) -> StToken:
        return self.tokens[self.pos]

    def next(self) -> StToken:
        token = self.peek()
        if token.kind != "eof":
            self.pos += 1
        return token

    def match_upper(self, upper: str) -> bool:
        if self.peek().upper == upper:
            self.next()
            return True
        return False

    def match_text(self, text: str) -> bool:
        if self.peek().text == text:
            self.next()
            return True
        return False

    def expect_upper(self, upper: str) -> StToken:
        if self.peek().upper != upper:
            self.error(f"expected {upper}, got {self.peek().text!r}")
        return self.next()

    def expect_text(self, text: str) -> StToken:
        if self.peek().text != text:
            self.error(f"expected {text}, got {self.peek().text!r}")
        return self.next()

    def expect_identifier(self) -> StToken:
        token = self.peek()
        if token.kind != "identifier":
            self.error(f"expected identifier, got {token.text!r}")
        return self.next()

    def nearby(self, radius: int = 5) -> str:
        start = max(0, self.pos - radius)
        end = min(len(self.tokens), self.pos + radius + 1)
        text = tokens_to_text([token for token in self.tokens[start:end] if token.kind != "eof"])
        return text or "<eof>"

    def error(self, message: str) -> None:
        token = self.peek()
        raise StParseError(
            f"{message} at {self.source_name}:{token.line}:{token.column}; near {self.nearby()!r}"
        )


class TypeParser:
    """Parses an ST type expression into the fuzzer's compact TypeSpec model."""

    def __init__(
        self,
        raw: str | None = None,
        tokens: list[StToken] | None = None,
        constants: dict[str, int] | None = None,
        source_name: str = "<type>",
    ):
        self.stream = TokenStream(tokens if tokens is not None else tokenize_st(raw or ""), source_name)
        self.constants = constants or {}

    def parse_bound(self, fallback: int) -> int:
        sign = 1
        if self.stream.peek().text in ("+", "-"):
            sign = -1 if self.stream.next().text == "-" else 1
        token = self.stream.peek()
        if token.kind == "integer":
            self.stream.next()
            value = _integer_literal_value(token.text)
            return fallback if value is None else sign * value
        if token.kind == "identifier":
            self.stream.next()
            value = self.constants.get(token.upper)
            return fallback if value is None else sign * value
        while self.stream.peek().kind != "eof" and self.stream.peek().text not in (
            ",",
            "]",
            ")",
            "..",
        ):
            self.stream.next()
        return fallback

    def parse_type(self) -> TypeSpec:
        if self.stream.match_upper("POINTER"):
            self.stream.expect_upper("TO")
            element = self.parse_type()
            return TypeSpec("pointer", f"POINTER TO {element.name}", element=element)

        if self.stream.match_upper("REF_TO"):
            element = self.parse_type()
            return TypeSpec("pointer", f"REF_TO {element.name}", element=element)

        if self.stream.match_upper("ARRAY"):
            self.stream.expect_text("[")
            count = 1
            saw_bound = False
            while self.stream.peek().kind != "eof" and self.stream.peek().text != "]":
                saw_bound = True
                lo = self.parse_bound(0)
                if self.stream.match_text(".."):
                    hi = self.parse_bound(lo)
                else:
                    hi = lo
                count *= max(0, hi - lo + 1)
                if not self.stream.match_text(","):
                    break
            self.stream.expect_text("]")
            self.stream.expect_upper("OF")
            element = self.parse_type()
            count = max(1, min(count if saw_bound else 1, MAX_ARRAY_ELEMENTS))
            return TypeSpec("array", f"ARRAY OF {element.name}", element=element, count=count)

        token = self.stream.next()
        ty = token.upper
        if ty in ("STRING", "WSTRING"):
            width = 2 if ty == "WSTRING" else 1
            length = DEFAULT_STRING_LENGTH
            if self.stream.match_text("[") or self.stream.match_text("("):
                closer = "]" if self.stream.tokens[self.stream.pos - 1].text == "[" else ")"
                length = self.parse_bound(DEFAULT_STRING_LENGTH)
                while self.stream.peek().kind != "eof" and self.stream.peek().text != closer:
                    self.stream.next()
                self.stream.expect_text(closer)
            length = max(1, length)
            c_type = "uint16_t" if width == 2 else "uint8_t"
            return TypeSpec("string", ty, c_type=c_type, length=length, size=width)

        if ty in SCALAR_TYPES:
            c_type, size, reader = SCALAR_TYPES[ty]
            return TypeSpec("scalar", ty, c_type=c_type, size=size, reader=reader)

        if token.kind == "eof":
            raise StParseError("unexpected end of type")

        return TypeSpec(
            "pointer",
            ty,
            element=TypeSpec("scalar", "BYTE", c_type="uint8_t", size=1, reader="read_u8"),
        )


def parse_type(raw: str) -> TypeSpec:
    return TypeParser(raw).parse_type()


class DeclarationParser:
    """Parses VAR blocks and declarations from an existing token stream."""

    def __init__(
        self,
        stream: TokenStream,
        constants: dict[str, int],
        target_name: str | None = None,
    ):
        self.stream = stream
        self.constants = constants
        self.target_name = target_name

    def parse_var_block(self) -> StVarBlock:
        start = self.stream.peek()
        if start.upper not in VAR_BLOCK_STARTS:
            self.stream.error(f"expected VAR block start, got {start.text!r}")
        base_kind = self.stream.next().upper
        modifiers: list[str] = []
        while self.stream.peek().upper in VAR_BLOCK_MODIFIERS:
            modifiers.append(self.stream.next().upper)

        kind = self.block_kind(base_kind, modifiers)
        declarations: list[StVarDecl] = []
        while self.stream.peek().kind != "eof" and self.stream.peek().upper != "END_VAR":
            if self.stream.peek().text == ";":
                self.stream.next()
                continue
            declaration = self.parse_variable_declaration()
            if declaration is not None:
                declarations.append(declaration)
        self.stream.expect_upper("END_VAR")
        return StVarBlock(kind=kind, declarations=declarations)

    @staticmethod
    def block_kind(base_kind: str, modifiers: list[str]) -> str:
        ordered = [modifier for modifier in ("RETAIN", "PERSISTENT", "NON_RETAIN") if modifier in modifiers]
        if "CONSTANT" in modifiers:
            ordered.append("CONSTANT")
        if not ordered:
            return base_kind
        return "_".join([base_kind, *ordered])

    def parse_variable_declaration(self) -> StVarDecl | None:
        declaration_start = self.stream.pos
        names: list[str] = []
        while self.stream.peek().kind == "identifier":
            names.append(self.stream.next().text)
            if not self.stream.match_text(","):
                break

        address = None
        if self.stream.match_upper("AT"):
            address = self.stream.next().text

        if not names or not self.stream.match_text(":"):
            self.skip_to_statement_end()
            return None

        type_tokens: list[StToken] = []
        depth = 0
        while self.stream.peek().kind != "eof":
            token = self.stream.peek()
            if depth == 0 and token.text in (";", ":="):
                break
            if token.text in ("[", "("):
                depth += 1
            elif token.text in ("]", ")"):
                depth = max(0, depth - 1)
            type_tokens.append(self.stream.next())

        if not type_tokens:
            self.stream.error(f"missing type in declaration {', '.join(names)!r}")

        decl_text = tokens_to_text(self.stream.tokens[declaration_start : self.stream.pos])
        try:
            spec = TypeParser(
                tokens=[*type_tokens, StToken("eof", "", type_tokens[-1].line, type_tokens[-1].column)],
                constants=self.constants,
                source_name=f"{self.stream.source_name} declaration {decl_text!r}",
            ).parse_type()
        except StParseError as exc:
            raise StParseError(
                f"{exc}; while parsing declaration {decl_text!r}"
                + (f" in target {self.target_name!r}" if self.target_name else "")
            ) from exc

        constant_value = None
        if self.stream.match_text(":="):
            constant_value, _ = self.skip_initializer_after_assignment()
        else:
            self.stream.expect_text(";")
        return StVarDecl(names=names, spec=spec, address=address, constant_value=constant_value)

    def skip_initializer_after_assignment(self) -> tuple[int | None, int]:
        sign = 1
        if self.stream.peek().text in ("+", "-"):
            sign = -1 if self.stream.next().text == "-" else 1
        value = None
        if self.stream.peek().kind == "integer":
            parsed = _integer_literal_value(self.stream.peek().text)
            value = None if parsed is None else sign * parsed
            self.stream.next()
        self.skip_to_statement_end()
        return value, self.stream.pos

    def skip_to_statement_end(self) -> None:
        depth = 0
        while self.stream.peek().kind != "eof":
            token = self.stream.next()
            if token.text in ("[", "("):
                depth += 1
            elif token.text in ("]", ")"):
                depth = max(0, depth - 1)
            elif token.text == ";" and depth == 0:
                break


class PouExtractor:
    """Extracts FUNCTION and FUNCTION_BLOCK signatures and declaration blocks."""

    def __init__(
        self,
        src: str,
        source_name: str = "<st>",
        target_name: str | None = None,
    ):
        self.constants = integer_constants(src)
        self.stream = TokenStream(tokenize_st(src), source_name)
        self.target_name = target_name

    def parse(self) -> StSourceAst:
        functions: list[StFunction] = []
        function_blocks: list[StFunctionBlock] = []
        while self.stream.peek().kind != "eof":
            if self.stream.peek().upper == "FUNCTION_BLOCK":
                function_blocks.append(self.parse_function_block())
            elif self.stream.peek().upper == "FUNCTION":
                functions.append(self.parse_function())
            else:
                self.stream.next()
        return StSourceAst(functions, function_blocks)

    def parse_function(self) -> StFunction:
        self.stream.expect_upper("FUNCTION")
        name = self.stream.expect_identifier().text
        self.stream.expect_text(":")
        ret_spec = self.parse_type_until({"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_TEMP", "END_FUNCTION"})
        var_blocks: list[StVarBlock] = []
        while self.stream.peek().kind != "eof" and self.stream.peek().upper != "END_FUNCTION":
            if self.stream.peek().upper in VAR_BLOCK_STARTS:
                var_blocks.append(
                    DeclarationParser(self.stream, self.constants, target_name=name).parse_var_block()
                )
            else:
                self.stream.next()
        self.stream.expect_upper("END_FUNCTION")
        return StFunction(name=name, ret_spec=ret_spec, var_blocks=var_blocks)

    def parse_function_block(self) -> StFunctionBlock:
        self.stream.expect_upper("FUNCTION_BLOCK")
        name = self.stream.expect_identifier().text
        var_blocks: list[StVarBlock] = []
        while self.stream.peek().kind != "eof" and self.stream.peek().upper != "END_FUNCTION_BLOCK":
            if self.stream.peek().upper in VAR_BLOCK_STARTS:
                var_blocks.append(
                    DeclarationParser(self.stream, self.constants, target_name=name).parse_var_block()
                )
            else:
                self.stream.next()
        self.stream.expect_upper("END_FUNCTION_BLOCK")
        return StFunctionBlock(name=name, var_blocks=var_blocks)

    def parse_type_until(self, stop_words: set[str]) -> TypeSpec:
        type_tokens: list[StToken] = []
        depth = 0
        while self.stream.peek().kind != "eof":
            token = self.stream.peek()
            if depth == 0 and token.upper in stop_words:
                break
            if token.text in ("[", "("):
                depth += 1
            elif token.text in ("]", ")"):
                depth = max(0, depth - 1)
            type_tokens.append(self.stream.next())
        if not type_tokens:
            self.stream.error("missing FUNCTION return type")
        return TypeParser(
            tokens=[*type_tokens, StToken("eof", "", type_tokens[-1].line, type_tokens[-1].column)],
            constants=self.constants,
            source_name=self.stream.source_name,
        ).parse_type()


def parse_source_ast(src: str, source_name: str = "<st>", target_name: str | None = None) -> StSourceAst:
    return PouExtractor(src, source_name=source_name, target_name=target_name).parse()


def parse_declaration(decl: str):
    src = f"FUNCTION __tmp : INT\nVAR_INPUT\n{decl.rstrip(';')};\nEND_VAR\nEND_FUNCTION\n"
    function = parse_source_ast(src, source_name="<declaration>", target_name="__tmp").function("__tmp")
    declaration = function.var_blocks[0].declarations[0]
    return declaration.names, declaration.spec


def parse_function(path: str, target: str):
    parsed = parse_target(path, target)
    if parsed.kind != "FUNCTION":
        raise ValueError(f"{target!r} is a FUNCTION_BLOCK; use parse_target for mixed targets")
    return parsed.name, parsed.ret_spec, parsed.params, parsed.total_size


def _param_block_kind(block_kind: str) -> str:
    if block_kind.endswith("_CONSTANT"):
        return block_kind[: -len("_CONSTANT")]
    return block_kind


def _block_is_constant(block_kind: str) -> bool:
    return "CONSTANT" in block_kind.split("_")


def _block_params(block: StVarBlock, offset: int) -> tuple[list[Param], int]:
    params = []
    base_kind = _param_block_kind(block.kind)
    constant = _block_is_constant(block.kind)
    for declaration in block.declarations:
        for name in declaration.names:
            params.append(Param(name, declaration.spec, offset, base_kind, constant))
            offset += declaration.spec.fuzz_size()
    return params, offset


def _is_persistent_function_block_field(param: Param) -> bool:
    if param.constant:
        return False
    if param.block_kind in ("VAR_INPUT", "VAR_IN_OUT", "VAR_OUTPUT", "VAR_TEMP"):
        return False
    return bool(param.block_kind and param.block_kind.startswith("VAR"))


def _is_function_block_constant_field(param: Param) -> bool:
    return bool(param.constant and param.block_kind != "VAR_INPUT")


def parse_target(path: str, target: str) -> StTarget:
    try:
        with open(path, "r", encoding="utf-8") as f:
            ast = parse_source_ast(f.read(), source_name=path, target_name=target)
    except StParseError as exc:
        raise StParseError(f"failed to parse target {target!r} in {path}: {exc}") from exc

    for function in ast.functions:
        if function.name.upper() != target.upper():
            continue
        params: list[Param] = []
        offset = 0
        for block in function.var_blocks:
            if block.kind not in ("VAR_INPUT", "VAR_INPUT_CONSTANT", "VAR_IN_OUT"):
                continue
            block_params, offset = _block_params(block, offset)
            params.extend(block_params)
        ordinary_inputs = [
            param for param in params if param.block_kind == "VAR_INPUT" and not param.constant
        ]
        constant_inputs = [
            param for param in params if param.block_kind == "VAR_INPUT" and param.constant
        ]
        inout_fields = [param for param in params if param.block_kind == "VAR_IN_OUT"]
        return StTarget(
            kind="FUNCTION",
            name=function.name,
            ret_spec=function.ret_spec,
            params=params,
            total_size=offset,
            ordinary_inputs=ordinary_inputs,
            constant_inputs=constant_inputs,
            inout_fields=inout_fields,
            persistent_fields=[],
            temp_fields=[],
            constant_fields=[],
            snapshot_fields=[],
        )

    for block in ast.function_blocks:
        if block.name.upper() != target.upper():
            continue
        params: list[Param] = []
        outputs: list[Param] = []
        state_fields: list[Param] = []
        offset = 0
        for var_block in block.var_blocks:
            block_fields, offset = _block_params(var_block, offset)
            state_fields.extend(block_fields)
            if var_block.kind in ("VAR_INPUT", "VAR_INPUT_CONSTANT", "VAR_IN_OUT"):
                params.extend(block_fields)
            elif var_block.kind == "VAR_OUTPUT":
                outputs.extend(block_fields)
        ordinary_inputs = [
            param for param in params if param.block_kind == "VAR_INPUT" and not param.constant
        ]
        constant_inputs = [
            param for param in params if param.block_kind == "VAR_INPUT" and param.constant
        ]
        inout_fields = [param for param in params if param.block_kind == "VAR_IN_OUT"]
        persistent_fields = [
            param for param in state_fields if _is_persistent_function_block_field(param)
        ]
        temp_fields = [param for param in state_fields if param.block_kind == "VAR_TEMP"]
        constant_fields = [
            param for param in state_fields if _is_function_block_constant_field(param)
        ]
        snapshot_fields = [
            param
            for param in state_fields
            if param in outputs or param in persistent_fields or param in inout_fields
        ]
        return StTarget(
            kind="FUNCTION_BLOCK",
            name=block.name,
            ret_spec=None,
            params=params,
            total_size=offset,
            outputs=outputs,
            state_fields=state_fields,
            ordinary_inputs=ordinary_inputs,
            constant_inputs=constant_inputs,
            inout_fields=inout_fields,
            persistent_fields=persistent_fields,
            temp_fields=temp_fields,
            constant_fields=constant_fields,
            snapshot_fields=snapshot_fields,
        )

    known_functions = ", ".join(function.name for function in ast.functions) or "<none>"
    known_blocks = ", ".join(block.name for block in ast.function_blocks) or "<none>"
    raise ValueError(
        f"target {target!r} not found in ST source {path}; known functions: {known_functions}; "
        f"known function blocks: {known_blocks}"
    )
