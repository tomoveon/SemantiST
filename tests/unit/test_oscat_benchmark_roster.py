import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]

from benchmarks.oscat.prepare_targets import normalize_cross_library_source


def test_oscat_roster_keeps_required_targets_and_excludes_other_families() -> None:
    manifest = json.loads((ROOT / "benchmarks/oscat/manifest.json").read_text(encoding="utf-8"))
    targets = manifest["targets"]
    ids = {target["id"] for target in targets}
    assert len(targets) == len(ids) == manifest["target_count"] == 100
    assert {suite: sum(target["suite"] == suite for target in targets)
            for suite in manifest["suite_counts"]} == manifest["suite_counts"]
    assert {target["suite"] for target in targets} == set(manifest["suite_counts"])

    additional_required = {
        "oscat_basic__buffer_clear",
        "oscat_basic__buffer_init",
        "oscat_basic_deg_to_dir",
        "oscat_basic_dir_to_deg",
        "oscat_basic_fstring_to_month",
        "oscat_basic_holiday",
        "oscat_basic_month_end",
        "oscat_basic_sequence_64",
        "oscat_basic_set_date",
        "oscat_basic_sh_2",
        "oscat_basic_buffer_search",
        "oscat_network_net_var_buffer",
        "oscat_network_irtrans_decode",
        "oscat_network_xml_reader",
        "oscat_network_smtp_client",
        "oscat_network_dlog_store_mysql",
        "oscat_building_timer_p4",
    }
    assert additional_required <= ids
    assert "oscat_basic_filter_mav_dw" in ids

    for target in targets:
        assert (ROOT / target["source_file"]).is_file()
        assert (ROOT / target["compatibility_manifest"]).is_file()
        assert target["st_file"] == f"benchmarks/oscat/targets/{target['id']}/target.st"
        if target.get("normalized_file"):
            assert (ROOT / target["normalized_file"]).is_file()


def test_cross_library_normalization_keeps_comments_and_strings() -> None:
    source = (
        "(* OSCAT_BASIC.CODE *)\n"
        "VAR_INPUT CONSTANT\n"
        "x : STRING := 'OSCAT_BASIC.CODE';\n"
        "END_VAR\n"
        "y := OSCAT_BASIC.CODE(x, 1);\n"
    )
    normalized = normalize_cross_library_source(source)
    assert "(* OSCAT_BASIC.CODE *)" in normalized
    assert "'OSCAT_BASIC.CODE'" in normalized
    assert "VAR_INPUT (* CONSTANT *)" in normalized
    assert "y := CODE(x, 1);" in normalized
