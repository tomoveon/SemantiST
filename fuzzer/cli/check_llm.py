#!/usr/bin/env python3
"""Check the configured OpenAI-compatible LLM gateway."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import argparse
from pathlib import Path

from fuzzer.reporting.llm import (
    DEFAULT_OPENAI_COMPATIBLE_API_KEY,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MODEL,
)


def request_json(url: str, *, api_key: str, body: dict[str, object] | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def model_ids(models_text: str) -> list[str]:
    try:
        payload = json.loads(models_text)
    except json.JSONDecodeError:
        return []
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return []
    ids = []
    for item in raw_models:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
    return ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the configured OpenAI-compatible LLM gateway")
    parser.add_argument(
        "--json-output",
        type=Path,
        help="write a machine-readable healthcheck summary to this path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_OPENAI_COMPATIBLE_BASE_URL).rstrip("/")
    api_key = os.environ.get("LLM_API_KEY", DEFAULT_OPENAI_COMPATIBLE_API_KEY)
    model = os.environ.get("LLM_MODEL", DEFAULT_OPENAI_COMPATIBLE_MODEL)
    endpoint_base = base_url if base_url.endswith("/v1") else f"{base_url}/v1"

    models_status, models_text = request_json(f"{endpoint_base}/models", api_key=api_key)
    print(f"[check_llm] models status: {models_status}")
    print(models_text[:1000])
    ids = model_ids(models_text)
    configured_model_listed = model in ids if ids else None
    if ids:
        print(f"[check_llm] configured model: {model}")
        print(f"[check_llm] configured model listed: {configured_model_listed}")
        if model not in ids:
            print(f"[check_llm] first listed models: {', '.join(ids[:8])}")

    chat_body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": 'Return only JSON: {"ok": true}'}],
    }
    chat_status, chat_text = request_json(
        f"{endpoint_base}/chat/completions",
        api_key=api_key,
        body=chat_body,
    )
    print(f"[check_llm] chat status: {chat_status}")
    print(chat_text[:2000])
    ok = 200 <= chat_status < 300
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                {
                    "ok": ok,
                    "base_url": base_url,
                    "endpoint_base": endpoint_base,
                    "configured_model": model,
                    "configured_model_listed": configured_model_listed,
                    "listed_models": ids,
                    "models_status": models_status,
                    "models_response_preview": models_text[:2000],
                    "chat_status": chat_status,
                    "chat_response_preview": chat_text[:2000],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
