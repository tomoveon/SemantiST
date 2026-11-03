import json
import tempfile
import unittest
from pathlib import Path

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from compiler.harness.contract import abi_json, state_layout_json, build_contract
from compiler.harness.state_table import StateTableError, append_state_table


def abi():
    return TargetAbi(
        "FUNCTION_BLOCK",
        "FB",
        [Param("X", scalar_spec("INT"), "VAR_INPUT", offset=8, index=1)],
        state_fields=[Param("X", scalar_spec("INT"), "VAR_INPUT", offset=8, index=1)],
        instance_size=16,
        instance_align=8,
    )


class ContractTests(unittest.TestCase):
    def test_abi_has_frame_layout(self):
        document = abi_json(abi())
        self.assertEqual(document["schema_version"], "semantist.abi/1.0.0")
        self.assertEqual(document["frame"]["size"], 2)
        self.assertEqual(document["frame"]["fields"][0]["name"], "X")

    def test_state_layout_contains_harness_region(self):
        with tempfile.TemporaryDirectory() as directory:
            state_ir = Path(directory) / "state.ll"
            state_ir.write_text(append_state_table('@TOTAL = internal global i32 0, align 4\n')[0],
                                encoding="utf-8")
            layout = state_layout_json(abi(), state_ir)
        names = [region["name"] for region in layout["regions"]]
        self.assertIn("TOTAL", names)
        self.assertEqual(names[-1], "semantist_harness_state")
        self.assertGreater(layout["total_size"], 0)

    def test_state_layout_matches_native_table_after_global_optimization(self):
        source = ('@REMOVED = internal global i64 0, align 8\n'
                  '@KEPT = internal global i32 0, align 4\n')
        optimized = '@KEPT = internal global i32 0, align 4\n'
        with tempfile.TemporaryDirectory() as directory:
            state_ir = Path(directory) / "state.ll"
            state_ir.write_text(append_state_table(source, optimized)[0], encoding="utf-8")
            layout = state_layout_json(abi(), state_ir)
        self.assertEqual([region["name"] for region in layout["regions"]],
                         ["KEPT", "semantist_harness_state"])
        self.assertEqual(layout["harness_region_index"], 1)
        self.assertEqual(layout["total_size"], 4 + layout["regions"][1]["size"])

    def test_zero_native_regions_do_not_import_discarded_globals(self):
        source = '@DISCARDED = internal global ptr null, align 8\n'
        with tempfile.TemporaryDirectory() as directory:
            state_ir = Path(directory) / "state.ll"
            state_ir.write_text(append_state_table(source, "")[0], encoding="utf-8")
            layout = state_layout_json(abi(), state_ir)
        self.assertEqual([region["name"] for region in layout["regions"]],
                         ["semantist_harness_state"])
        self.assertEqual(layout["harness_region_index"], 0)
        self.assertEqual(layout["total_size"], layout["regions"][0]["size"])

    def test_missing_native_state_table_is_not_guessed_from_globals(self):
        with tempfile.TemporaryDirectory() as directory:
            state_ir = Path(directory) / "state.ll"
            state_ir.write_text('@TOTAL = internal global i32 0, align 4\n', encoding="utf-8")
            with self.assertRaises(StateTableError):
                state_layout_json(abi(), state_ir)

    def test_compatibility_fidelity_and_selected_stubs_enter_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "target.st"
            source.write_text("FUNCTION FB : INT\nEND_FUNCTION\n", encoding="utf-8")
            manifest = root / "compatibility.json"
            manifest.write_text(json.dumps({"semantic_fidelity": "semantic",
                                            "support_status": "semantic",
                                            "stub_used": ["vendor_time.st"]}), encoding="utf-8")
            abi_path = root / "abi.json"
            layout_path = root / "state-layout.json"
            binary = root / "fuzz_target"
            for path in (abi_path, layout_path, binary):
                path.write_bytes(b"test")
            contract = build_contract(st_file=source, function="FB", abi=abi(),
                                      abi_path=abi_path, state_layout_path=layout_path,
                                      target_binary=binary, compatibility_manifest=manifest,
                                      symbolic="unsupported", symbolic_reason="KLEE unavailable")
        self.assertEqual(contract["semantic_fidelity"], "approximate")
        self.assertEqual(contract["support_status"], "native_only")
        self.assertEqual(contract["stub_used"], ["vendor_time.st"])
        self.assertEqual(contract["execution_status"]["native"], "ready")


if __name__ == "__main__":
    unittest.main()
