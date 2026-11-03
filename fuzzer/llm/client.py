"""OpenAI-compatible chat completions client with cache, retries and budget."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fuzzer.llm.config import LlmConfig


class LlmError(RuntimeError):
    pass


@dataclass
class LlmResponse:
    content: str
    usage: dict[str, Any]
    cached: bool
    model: str


class LlmClient:
    def __init__(
        self,
        config: LlmConfig,
        *,
        cache_dirs: list[Path] | None = None,
        offline: bool = False,
        max_calls: int = 40,
        timeout_s: float = 90.0,
        retries: int = 2,
    ):
        self.config = config
        self.cache_dirs = [Path(path) for path in (cache_dirs or [])]
        self.offline = offline
        self.max_calls = max_calls
        self.timeout_s = timeout_s
        self.retries = retries
        self.calls = 0
        self._in_flight = 0
        self._state_lock = threading.Lock()
        self.tokens = 0
        self.cache_hits = 0

    def _cache_path(self, digest: str, index: int) -> Path:
        return self.cache_dirs[index] / f"{digest}.json"

    def _read_cache(self, digest: str) -> LlmResponse | None:
        for index in range(len(self.cache_dirs)):
            path = self._cache_path(digest, index)
            if path.exists():
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    content = record["content"]
                except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                    # A truncated cache entry must not abort an otherwise valid
                    # campaign. Leave it in place for inspection and try the
                    # next cache tier or the network.
                    continue
                self.cache_hits += 1
                return LlmResponse(content, record.get("usage", {}), True, record.get("model", self.config.model))
        return None

    def _write_cache(self, digest: str, content: str, usage: dict[str, Any]) -> None:
        if not self.cache_dirs:
            return
        record = {"content": content, "usage": usage, "model": self.config.model}
        serialized = json.dumps(record, indent=2, sort_keys=True) + "\n"
        for index in range(len(self.cache_dirs)):
            path = self._cache_path(digest, index)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(serialized, encoding="utf-8")
                temporary.replace(path)
            except OSError:
                # A read-only global cache must not make a successful request
                # fail when the run-local cache is writable.
                continue

    def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.0,
                 deadline: float | None = None) -> LlmResponse:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        payload = json.dumps(body, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cached = self._read_cache(digest)
        if cached is not None:
            return cached
        if self.offline:
            raise LlmError("offline and no cached response")
        if not self.config.available:
            raise LlmError("LLM is not configured")
        with self._state_lock:
            if self.calls + self._in_flight >= self.max_calls:
                raise LlmError("LLM call budget exhausted")
            self._in_flight += 1
        base = self.config.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        url = base + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        last_error: Exception | None = None
        try:
            for attempt in range(self.retries + 1):
                remaining = (deadline - time.monotonic()) if deadline is not None else self.timeout_s
                if remaining <= 0:
                    raise LlmError("LLM deadline expired")
                data = dict(body)
                request = urllib.request.Request(
                    url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST"
                )
                try:
                    with urllib.request.urlopen(request, timeout=min(self.timeout_s, remaining)) as response:
                        raw = response.read().decode("utf-8")
                    parsed = json.loads(raw)
                    content = parsed["choices"][0]["message"]["content"]
                    usage = parsed.get("usage", {})
                    with self._state_lock:
                        self.calls += 1
                        self.tokens += int(usage.get("total_tokens", 0) or 0)
                    self._write_cache(digest, content, usage)
                    return LlmResponse(content, usage, False, self.config.model)
                except urllib.error.HTTPError as error:
                    last_error = error
                    if error.code in (400, 404, 422) and "response_format" in data:
                        data.pop("response_format", None)
                        body = data
                        continue
                    backoff = min(2 ** attempt, 5)
                    if deadline is not None:
                        backoff = min(backoff, max(0.0, deadline - time.monotonic()))
                    time.sleep(backoff)
                except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
                    last_error = error
                    backoff = min(2 ** attempt, 5)
                    if deadline is not None:
                        backoff = min(backoff, max(0.0, deadline - time.monotonic()))
                    time.sleep(backoff)
            raise LlmError(f"LLM request failed: {last_error}")
        finally:
            with self._state_lock:
                self._in_flight = max(0, self._in_flight - 1)
