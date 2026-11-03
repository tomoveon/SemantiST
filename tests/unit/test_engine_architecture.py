"""R2/R3 architecture guard: the fuzz loop is Rust and Python search is gone."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_python_search_loop_is_deleted():
    assert not (ROOT / "fuzzer" / "v3" / "search.py").exists()


def test_rust_engine_components_exist():
    engine = ROOT / "fuzzer" / "engine" / "src"
    for name in ("v3.rs", "v3_exec.rs", "v3_observer.rs", "v3_stage.rs"):
        assert (engine / name).is_file(), name
    text = (engine / "v3_stage.rs").read_text(encoding="utf-8")
    assert "pub type GoalStage" in text
    text = (engine / "v3_exec.rs").read_text(encoding="utf-8")
    assert "PrefixCachingExecutor" in text
