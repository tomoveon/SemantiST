"""Campaign metrics: events.jsonl and summary.json (spec section 10.3)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class Metrics:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.metrics_dir = self.run_dir / "metrics"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.metrics_dir / "events.jsonl"
        self.started = time.monotonic()
        self.counts: dict[str, int] = {}
        self.counters: dict[str, float] = {
            "executions": 0,
            "scans": 0,
            "snapshot_hits": 0,
            "snapshot_saved_scans": 0,
            "snapshot_attempts": 0,
            "snapshot_misses": 0,
            "candidates": 0,
            "confirmed": 0,
            "solver_queries": 0,
            "llm_calls": 0,
            "llm_tokens": 0,
        }
        self._lock = threading.RLock()

    def event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            record = {"t": round(time.monotonic() - self.started, 3), "kind": kind, **fields}
            line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
            with self.events_path.open("ab", buffering=0) as out:
                if out.write(line) != len(line):
                    raise OSError("short metrics event write")
            self.counts[kind] = self.counts.get(kind, 0) + 1

    def add(self, name: str, value: float) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + value

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "definition": "one execution means one complete testcase from initialization",
                "events": dict(sorted(self.counts.items())),
                **{key: self.counters.get(key, 0) for key in self.counters},
            }

    def write_summary(self) -> Path:
        path = self.metrics_dir / "summary.json"
        path.write_text(json.dumps(self.summary(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
