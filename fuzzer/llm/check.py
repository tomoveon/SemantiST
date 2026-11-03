"""semantist-llm-check: verify the configured endpoint without exposing the key."""

from __future__ import annotations

import argparse
import json
import sys

from fuzzer.llm.client import LlmClient, LlmError
from fuzzer.llm.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="path to the .env file")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    config = load_config(args.env_file)
    print(json.dumps({"config": config.masked()}, indent=2))
    if not config.available:
        print("LLM not configured", file=sys.stderr)
        return 2
    client = LlmClient(config, timeout_s=args.timeout, max_calls=1, retries=1)
    try:
        response = client.complete(
            [
                {"role": "system", "content": "Reply with one JSON object."},
                {"role": "user", "content": 'Return {"ok": true}.'},
            ]
        )
    except LlmError as error:
        print(f"LLM check failed: {error}", file=sys.stderr)
        return 1
    body = response.content.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {"raw": body[:200]}
    print(json.dumps({"ok": True, "model": response.model, "usage": response.usage, "reply": parsed}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
