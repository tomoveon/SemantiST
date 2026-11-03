"""Reject ASan configurations that would silently leave ST reads unchecked."""

import pytest

from compiler.instrumentation.compile import (
    address_sanitizer_enabled,
    instrument_memory_primitive_bounds,
    validate_address_sanitizer_attributes,
)


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ("-fsanitize=address,undefined -fno-omit-frame-pointer", True),
        ("-fsanitize=undefined", False),
        ("-fsanitize=address -fno-sanitize=address", False),
        ("-fsanitize=address -fno-sanitize=all", False),
        ("-fno-sanitize=all -fsanitize=address", True),
        ("-fsanitize=address -fno-sanitize=undefined", True),
        ("-fsanitize=kernel-address", False),
        (" ", False),
    ],
)
def test_address_sanitizer_flag_order(flags, expected):
    assert address_sanitizer_enabled(flags) is expected


def test_every_retained_function_requires_asan_not_only_entry():
    source = """define void @entry(ptr %p) #0 {
  call void @helper(ptr %p)
  ret void
}
define internal void @helper(ptr %p) #1 {
  store i32 42, ptr %p
  ret void
}
attributes #0 = { sanitize_address }
attributes #1 = { noinline }
"""
    with pytest.raises(SystemExit, match="@helper"):
        validate_address_sanitizer_attributes(source)
    assert validate_address_sanitizer_attributes(
        source.replace("{ noinline }", "{ noinline sanitize_address }")
    ) == 2


@pytest.mark.parametrize(
    "attributes",
    [
        '"sanitize_address"',
        "nosanitize_address",
        "sanitize_address disable_sanitizer_instrumentation",
    ],
)
def test_metadata_strings_and_disabled_instrumentation_do_not_pass(attributes):
    source = (
        "define void @entry() #0 {\n  ret void\n}\n"
        f"attributes #0 = {{ {attributes} }}\n"
        '!0 = !{i32 4, !"nosanitize_address", i32 1}\n'
    )
    with pytest.raises(SystemExit, match="incomplete ASan"):
        validate_address_sanitizer_attributes(source)


def test_only_linking_asan_runtime_does_not_pass():
    source = """declare void @__asan_init()
define void @entry() {
  call void @__asan_init()
  ret void
}
"""
    with pytest.raises(SystemExit, match="@entry"):
        validate_address_sanitizer_attributes(source)


def test_attribute_group_before_comdat_is_checked():
    source = """define void @entry() #0 comdat($entry) {
  ret void
}
attributes #0 = { sanitize_address }
"""
    assert validate_address_sanitizer_attributes(source) == 1


def test_memory_primitive_checks_struct_member_gep_at_call_site(tmp_path):
    source = tmp_path / "target.ll"
    prepared = tmp_path / "prepared.ll"
    source.write_text(
        """
%FB = type { i8, double }
define ptr @entry(ptr %base, i32 %count) {
entry:
  %member = getelementptr inbounds %FB, ptr %base, i32 0, i32 1
  %result = call ptr @MemSet(ptr %member, i32 0, i32 %count)
  ret ptr %result
}
""",
        encoding="ascii",
    )

    assert instrument_memory_primitive_bounds(source, prepared)
    text = prepared.read_text(encoding="ascii")
    assert "@__SEMANTIST_MEMSET_CHECKED(ptr %member, i64 8, i32 0, i32 %count)" in text
    assert "declare ptr @__SEMANTIST_MEMSET_CHECKED(ptr, i64, i32, i32)" in text
