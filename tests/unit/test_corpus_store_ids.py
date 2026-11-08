import tempfile
from pathlib import Path
from types import SimpleNamespace

from fuzzer.corpus.store import CorpusStore


def test_equal_seed_bytes_keep_distinct_numbered_files_across_store_reopen() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        layout = SimpleNamespace(
            bootstrap=root / "testcase-metadata.bootstrap.json",
            unified_seeds=root / "unified-corpus" / "seeds",
            unified_runtime=root / "unified-corpus" / "runtime",
        )
        store = CorpusStore(layout)
        first, first_new = store.add_seed(b"X,INT,0x0100\n", "Initial")
        second, second_new = store.add_seed(b"X,INT,0x0100\n", "Initial")
        assert first_new and second_new
        assert first.seed_id == "seed-00000001"
        assert second.seed_id == "seed-00000002"
        assert first.content_hash == second.content_hash
        assert store.seed_bytes(first.seed_id) == store.seed_bytes(second.seed_id)
        store.persist()
        reopened = CorpusStore(layout)
        third, _ = reopened.add_seed(b"X,INT,0x0200\n", "Initial")
        assert third.seed_id == "seed-00000003"
        (layout.unified_seeds / "seed-00000003.seed").unlink()
        reopened.persist()
        after_missing_file = CorpusStore(layout)
        fourth, _ = after_missing_file.add_seed(b"X,INT,0x0300\n", "Initial")
        assert fourth.seed_id == "seed-00000004"
