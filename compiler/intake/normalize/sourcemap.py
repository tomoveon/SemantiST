"""Span edits on one source text with a mapping back to the original.

:class:`EditableSource` keeps the text as a list of pieces. A copied piece is a
slice of the original; a replacement piece carries the rule that produced it
and the original span it replaces. Every edit is expressed in coordinates of
the current text, so rules can work on the token stream they see; the mapping
to the original is maintained automatically.

The exported SourceMap lists the pieces with normalized and original offsets
and lines, which is enough to translate any normalized position (a diagnostic,
a fuzzing finding) back to the original vendor text.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass


@dataclass
class Piece:
    text: str
    original_start: int
    original_end: int
    rule: str | None = None
    # Moved text equals its original span character for character.
    exact: bool = False

    @property
    def copied(self) -> bool:
        return self.rule is None

    @property
    def precise(self) -> bool:
        return self.copied or self.exact


class EditableSource:
    def __init__(self, original: str):
        self.original = original
        self.pieces: list[Piece] = [Piece(original, 0, len(original))] if original else []

    @property
    def text(self) -> str:
        return "".join(piece.text for piece in self.pieces)

    def replace(self, start: int, end: int, replacement: str, rule: str) -> None:
        """Replace ``text[start:end]`` by ``replacement`` on behalf of ``rule``."""
        if start > end:
            raise ValueError("edit span is reversed")
        insertion_origin = self.to_original(start)
        before: list[Piece] = []
        covered: list[Piece] = []
        after: list[Piece] = []
        position = 0
        for piece in self.pieces:
            piece_start, piece_end = position, position + len(piece.text)
            position = piece_end
            if piece_end <= start and not (piece_start < start < piece_end):
                before.append(piece)
            elif piece_start >= end and not (piece_start < start < piece_end):
                after.append(piece)
            else:
                if piece_start < start:
                    before.append(self._slice(piece, 0, start - piece_start))
                inner = self._slice(piece, max(start, piece_start) - piece_start, min(end, piece_end) - piece_start)
                if inner.text:
                    covered.append(inner)
                if piece_end > end:
                    after.append(self._slice(piece, end - piece_start, piece_end - piece_start))
        if covered:
            original_start = min(p.original_start for p in covered)
            original_end = max(p.original_end for p in covered)
        else:
            original_start = original_end = insertion_origin
        new = Piece(replacement, original_start, original_end, rule)
        self.pieces = [p for p in before + [new] + after if p.text or not p.copied]

    def move(self, start: int, end: int, destination: int, rule: str) -> None:
        """Move ``text[start:end]`` to ``destination`` (current coordinates,
        outside the moved span). Moved text keeps its mapping to the original;
        only the pieces' rule is set, so the SourceMap shows the move."""
        if start < destination < end:
            raise ValueError("cannot move a span into itself")
        moved: list[Piece] = []
        kept: list[Piece] = []
        position = 0
        for piece in self.pieces:
            piece_start, piece_end = position, position + len(piece.text)
            position = piece_end
            cuts = sorted({start, end, destination} - {piece_start, piece_end})
            bounds = [piece_start, *[c for c in cuts if piece_start < c < piece_end], piece_end]
            for low, high in zip(bounds, bounds[1:]):
                part = self._slice(piece, low - piece_start, high - piece_start)
                (moved if start <= low and high <= end and start < end else kept).append((low, part))
        moved_pieces = [Piece(p.text, p.original_start, p.original_end, rule, exact=p.precise) for _, p in moved]
        result: list[Piece] = []
        inserted = False
        for low, piece in kept:
            if not inserted and low >= destination:
                result.extend(moved_pieces)
                inserted = True
            result.append(piece)
        if not inserted:
            result.extend(moved_pieces)
        self.pieces = [p for p in result if p.text]

    def _slice(self, piece: Piece, begin: int, finish: int) -> Piece:
        if piece.precise:
            return Piece(piece.text[begin:finish], piece.original_start + begin, piece.original_start + finish,
                         piece.rule, piece.exact)
        # Part of an earlier replacement keeps the whole original span.
        return Piece(piece.text[begin:finish], piece.original_start, piece.original_end, piece.rule)

    def to_original(self, offset: int) -> int:
        """Original offset of a normalized offset (start of the replaced span
        for positions inside a replacement)."""
        position = 0
        for piece in self.pieces:
            if offset < position + len(piece.text) or piece is self.pieces[-1]:
                if piece.precise:
                    return piece.original_start + min(offset - position, len(piece.text))
                return piece.original_start
            position += len(piece.text)
        return len(self.original)

    def segments(self) -> list[dict]:
        """SourceMap segments with normalized and original offsets and lines."""
        normalized_lines = _LineIndex(self.text)
        original_lines = _LineIndex(self.original)
        result = []
        position = 0
        for piece in self.pieces:
            end = position + len(piece.text)
            result.append({
                "normalized": [position, end],
                "original": [piece.original_start, piece.original_end],
                "normalized_line": normalized_lines.line(position),
                "original_line": original_lines.line(piece.original_start),
                "rule": piece.rule,
                "moved": piece.exact,
            })
            position = end
        return result


class _LineIndex:
    def __init__(self, text: str):
        self.starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


def line_map(source: EditableSource) -> list[int]:
    """Original line of every normalized line (1-based, index 0 = line 1)."""
    text = source.text
    lines = _LineIndex(source.original)
    starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
    return [lines.line(source.to_original(start)) for start in starts]
