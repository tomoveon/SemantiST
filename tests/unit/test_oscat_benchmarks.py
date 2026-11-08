"""The default benchmark index covers the requested OSCAT targets."""

from collections import Counter
from pathlib import Path

from compiler.scripts.genfunction import extract_pou
from fuzzer.cli.batch import _benchmark_targets


def test_requested_oscat_targets_are_extracted_and_scheduled() -> None:
    targets = _benchmark_targets()
    assert len(targets) == 100
    assert len({target["id"] for target in targets}) == 100
    assert Counter(target["suite"] for target in targets) == {
        "oscat_basic": 80,
        "oscat_network": 10,
        "oscat_building": 10,
    }
    assert {
        "INC2", "_BUFFER_CLEAR", "_BUFFER_INIT", "_STRING_TO_BUFFER",
        "WEEKDAY_TO_STRING", "REAL_TO_STRF", "INC", "DIR_TO_DEG",
        "_BUFFER_INSERT", "SET_DATE", "MONTH_END", "FSTRING_TO_MONTH",
        "DEG_TO_DIR", "SCHEDULER_2", "BUFFER_SEARCH", "BUFFER_COMP",
        "DELAY", "SH_2", "SEQUENCE_64", "HOLIDAY", "NET_VAR_BUFFER",
        "IRTRANS_DECODE", "XML_READER", "SMTP_CLIENT", "DLOG_STORE_MYSQL",
        "TIMER_P4", "FILTER_MAV_DW",
    } <= {target["function"] for target in targets}
    for target in targets:
        path = Path(target["st_file"])
        assert path.is_file()
        name, kind, _body = extract_pou(path.read_text(encoding="utf-8"), target["function"])
        assert (name, kind) == (target["function"], target["manifest"]["kind"])
