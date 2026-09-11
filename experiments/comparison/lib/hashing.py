"""Stable hashing utilities for experiment records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_or_none(path: Path | None) -> str | None:
    if path is None:
        return None
    return sha256_file(path) if path.is_file() else None


def canonical_json_sha256(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256_text(data)


def rng_seed(run_key: str) -> int:
    digest = sha256_text(run_key)
    return (int(digest[0:16], 16) % 2_147_483_646) + 1


def target_name(function: str) -> str:
    chars = [ch.lower() if ch.isalnum() or ch in "_.-" else "_" for ch in function]
    name = "".join(chars).strip("_")
    return name or "target"
