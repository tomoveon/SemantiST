from compiler.parser.st import significant, tokenize_lossless


def kinds(src: str) -> list[tuple[str, str]]:
    return [(t.kind, t.text) for t in significant(tokenize_lossless(src))]


def test_round_trip_preserves_every_character() -> None:
    src = (
        "FUNCTION f : INT (* a (* nested *) b *)\r\n// line comment\n{attribute 'pack_mode' := '1'}\n"
        "VAR x : STRING := 'it$'s'; END_VAR\n\tf := 16#FF + 2#1010; ä # $\n(* unclosed"
    )
    tokens = tokenize_lossless(src)
    assert "".join(t.text for t in tokens) == src
    assert all(a.end == b.start for a, b in zip(tokens, tokens[1:]))


def test_comments_nest_and_pragmas_end_at_first_brace() -> None:
    tokens = tokenize_lossless("(* (* *) *) x {a 'b}' c} y")
    assert [(t.kind, t.text) for t in tokens if not t.kind == "whitespace"] == [
        ("comment", "(* (* *) *)"),
        ("identifier", "x"),
        ("pragma", "{a 'b}"),
        ("error", "'"),
        ("identifier", "c"),
        ("error", "}"),
        ("identifier", "y"),
    ]


def test_keyword_spellings_follow_rusty() -> None:
    assert kinds("ELSEIF ELSIF ENDIF END_FUNCTIONBLOCK ENDFUNCTIONBLOCK VAR_STAT VAR_INST OVERRIDE SET") == [
        ("identifier", "ELSEIF"),
        ("keyword", "ELSIF"),
        ("keyword", "ENDIF"),
        ("identifier", "END_FUNCTIONBLOCK"),
        ("keyword", "ENDFUNCTIONBLOCK"),
        ("identifier", "VAR_STAT"),
        ("identifier", "VAR_INST"),
        ("keyword", "OVERRIDE"),
        ("identifier", "SET"),
    ]


def test_literals_and_operators_use_longest_match() -> None:
    assert kinds("T#1s TIME#2h INT#5 TOD#12:30 REF= REFERENCE TO x %IX0.1 a..b ... ** <>") == [
        ("typed_literal", "T#1s"),
        ("typed_literal", "TIME#2h"),
        ("cast_prefix", "INT#"),
        ("number", "5"),
        ("typed_literal", "TOD#12:30"),
        ("symbol", "REF="),
        ("keyword", "REFERENCE TO"),
        ("identifier", "x"),
        ("direct_access", "%IX"),
        ("number", "0"),
        ("symbol", "."),
        ("number", "1"),
        ("identifier", "a"),
        ("symbol", ".."),
        ("identifier", "b"),
        ("symbol", "..."),
        ("symbol", "**"),
        ("symbol", "<>"),
    ]


def test_strings_do_not_treat_doubled_quotes_as_escapes() -> None:
    assert kinds("'it''s' \"w$\"x\"") == [("string", "'it'"), ("string", "'s'"), ("wstring", '"w$"x"')]


def test_positions_are_one_based_lines_and_columns() -> None:
    tokens = significant(tokenize_lossless("a\n  bb := 1;"))
    assert [(t.text, t.line, t.column) for t in tokens] == [("a", 1, 1), ("bb", 2, 3), (":=", 2, 6), ("1", 2, 9), (";", 2, 10)]
