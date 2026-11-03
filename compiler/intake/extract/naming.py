"""Portable output naming for the whole-library extractors.

The original extraction ran on Windows. Two of its naming decisions depended on
that host: a name budget derived from the absolute output path length (to stay
below MAX_PATH) and collision checks on a case-insensitive file system. Both are
fixed here so that the same input yields the same file names on any host.
"""

from __future__ import annotations

from pathlib import Path

# Upper bound for a generated base name (without the ".st" suffix). The
# extractors already shorten longer names to 143 characters, so this bound only
# replaces the host-dependent MAX_PATH budget and never truncates by itself.
NAME_BUDGET = 200


def exists(path: Path) -> bool:
    """Return whether ``path`` collides with an existing file, ignoring case.

    A case-insensitive check reproduces the original Windows behaviour and keeps
    the output portable to case-insensitive file systems.
    """
    folder = path.parent
    if not folder.is_dir():
        return False
    wanted = path.name.casefold()
    return any(item.name.casefold() == wanted for item in folder.iterdir())
