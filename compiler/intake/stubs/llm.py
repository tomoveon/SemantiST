"""Cached LLM calls for stub generation (offline only, never during intake).

The backend is the Claude Code CLI in print mode (``claude -p``) with its own
system prompt replaced, all tools disabled and a JSON schema for the answer.
Every request is keyed by the SHA-256 of model, effort, system prompt, prompt
and schema; the answer and its cost are stored under that key, so a second
run replays the cache byte for byte and makes no call. With ``offline=True``
a missing key is an error instead of a call.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
from pathlib import Path

MODEL = "claude-opus-5-5"
EFFORT = "medium"


class BudgetExceeded(RuntimeError):
    pass


class ClaudeCLI:
    def __init__(self, cache: Path, model: str = MODEL, effort: str = EFFORT, offline: bool = False,
                 budget_usd: float = 40.0, timeout: int = 1800):
        self.cache = cache
        self.model, self.effort, self.offline = model, effort, offline
        self.budget_usd, self.timeout = budget_usd, timeout
        self.spent_usd = 0.0
        self.calls = 0
        self.cache_hits = 0
        self._lock = threading.Lock()
        cache.mkdir(parents=True, exist_ok=True)

    def key(self, system: str, prompt: str, schema: dict) -> str:
        request = json.dumps({"model": self.model, "effort": self.effort, "system": system, "prompt": prompt,
                              "schema": schema}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(request.encode("utf-8")).hexdigest()

    def ask(self, system: str, prompt: str, schema: dict) -> tuple[dict, str]:
        """The structured answer and its cache key."""
        key = self.key(system, prompt, schema)
        path = self.cache / f"{key}.json"
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                answer = cached["answer"]
                if not isinstance(answer, dict):
                    raise ValueError("cached answer is not an object")
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                if self.offline:
                    raise KeyError(f"invalid cached answer for request {key} (offline)") from error
            else:
                self.cache_hits += 1
                return answer, key
        if self.offline:
            raise KeyError(f"no cached answer for request {key} (offline)")
        if self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(f"LLM budget of {self.budget_usd} USD reached")
        command = ["claude", "-p", "--no-session-persistence", "--tools", "", "--strict-mcp-config",
                   "--model", self.model, "--effort", self.effort, "--output-format", "json",
                   "--system-prompt", system, "--json-schema", json.dumps(schema)]
        with tempfile.TemporaryDirectory(prefix="semantist-stub-llm-") as cwd:
            process = subprocess.run(command, input=prompt, text=True, capture_output=True, cwd=cwd,
                                     timeout=self.timeout)
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            raise RuntimeError(
                f"claude -p exited with status {process.returncode}: {detail[-500:]}"
            )
        try:
            reply = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"claude -p returned no JSON: {process.stdout[-500:]} {process.stderr[-500:]}") \
                from error
        with self._lock:
            self.calls += 1
            self.spent_usd += float(reply.get("total_cost_usd") or 0)
        if reply.get("is_error") or not isinstance(reply.get("structured_output"), dict):
            raise RuntimeError(f"claude -p failed: {str(reply.get('result'))[:500]}")
        answer = reply["structured_output"]
        path.write_text(json.dumps({"key": key, "model": self.model, "effort": self.effort, "system": system,
                                    "prompt": prompt, "schema": schema, "answer": answer,
                                    "cost_usd": reply.get("total_cost_usd"), "usage": reply.get("usage"),
                                    "session_id": reply.get("session_id")},
                                   ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return answer, key
