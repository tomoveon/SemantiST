"""Metrics events remain parseable when campaign workers write concurrently."""

import json
import multiprocessing

from fuzzer.pipeline.metrics import Metrics


def _write_events(run_dir, worker):
    metrics = Metrics(run_dir)
    for sequence in range(100):
        metrics.event("concurrent", worker=worker, sequence=sequence, payload="x" * 512)


def test_events_are_complete_across_processes(tmp_path):
    workers = [multiprocessing.Process(target=_write_events, args=(tmp_path, worker))
               for worker in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    lines = (tmp_path / "metrics" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    assert {(event["worker"], event["sequence"]) for event in map(json.loads, lines)} == {
        (worker, sequence) for worker in range(4) for sequence in range(100)
    }
