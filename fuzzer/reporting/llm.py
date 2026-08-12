"""Shared OpenAI-compatible settings for optional report presentation."""

from __future__ import annotations

import json
import os
import re
from typing import Any


DEFAULT_OPENAI_COMPATIBLE_BASE_URL = os.environ.get("SEMANTIST_LLM_BASE_URL", "")
DEFAULT_OPENAI_COMPATIBLE_API_KEY = ""
DEFAULT_OPENAI_COMPATIBLE_MODEL = os.environ.get("SEMANTIST_LLM_MODEL", "")
DEFAULT_LLM_RETRY_DELAYS = tuple(
    int(part)
    for part in os.environ.get("SEMANTIST_LLM_RETRY_DELAYS", "5,15").split(",")
    if part.strip()
)
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


def extract_json_payload(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise
