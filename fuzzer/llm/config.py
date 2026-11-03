"""LLM configuration: environment first, then the repository .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass
class LlmConfig:
    base_url: str | None
    api_key: str | None
    model: str
    source: str

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def masked(self) -> dict[str, str | None]:
        key = self.api_key or ""
        masked = (key[:4] + "*" * max(0, len(key) - 8) + key[-4:]) if len(key) > 8 else ("*" * len(key))
        return {"base_url": self.base_url, "model": self.model, "api_key": masked, "source": self.source}


def load_config(env_file: Path | None = None) -> LlmConfig:
    keys = ("SEMANTIST_LLM_BASE_URL", "SEMANTIST_LLM_API_KEY", "SEMANTIST_LLM_MODEL")
    values = {key: os.environ.get(key) for key in keys}
    source = "environment"
    if not all(values.get(key) for key in keys[:2]):
        file_values = _read_env_file(env_file or ROOT / ".env.semantist-llm")
        for key in keys:
            if not values.get(key) and file_values.get(key):
                values[key] = file_values[key]
        source = "env-file" if file_values else "missing"
    return LlmConfig(
        values.get("SEMANTIST_LLM_BASE_URL"),
        values.get("SEMANTIST_LLM_API_KEY"),
        values.get("SEMANTIST_LLM_MODEL") or "gpt-5.6-sol",
        source,
    )
