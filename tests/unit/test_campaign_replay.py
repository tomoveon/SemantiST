import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.toolchain.compatibility import load_project
from fuzzer.pipeline.campaign import Candidate, Campaign
from fuzzer.pipeline.probe import Observation
from fuzzer.pipeline.replay import _build_from_package, _load_contract, _replay_status, _target_from_contract, main


class ReplayContractTests(unittest.TestCase):
    def test_replay_status_uses_shared_vocab(self):
        candidate = {"oracle": "array_access"}
        self.assertEqual(_replay_status(Observation(0, False, ""), candidate, "array_access"), "confirmed")
        self.assertEqual(
            _replay_status(Observation(-6, False, "asan", crashed=True),
                           {"oracle": "crash"}, "crash"),
            "confirmed",
        )
        self.assertEqual(_replay_status(Observation(0, False, ""), candidate, "timer"), "REPLAY_MISMATCH")
        self.assertEqual(_replay_status(Observation(0, False, ""), None, "array_access"), "REPLAY_MISMATCH")
        self.assertEqual(_replay_status(Observation(0, True, ""), None, "array_access"), "TIMEOUT")
        self.assertEqual(_replay_status(Observation(7, False, ""), None, "array_access"), "FAILED")

    def test_invalid_package_is_reported_as_unsupported(self):
        temporary = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(main([temporary.name]), 2)
        finally:
            temporary.cleanup()

    def _package(self, *, finding_contract: str = "contract-1") -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        package = Path(temporary.name)
        source = package / "source"
        source.mkdir()
        target_source = source / "target.st"
        target_source.write_text("FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n", encoding="utf-8")
        contract = {
            "contract_id": "contract-1",
            "target": {"source_sha256": hashlib.sha256(target_source.read_bytes()).hexdigest()},
            "capabilities": {"target_binary_sha256": None},
        }
        (package / "execution-contract.json").write_text(json.dumps(contract), encoding="utf-8")
        (package / "finding.json").write_text(
            json.dumps({"contract_id": finding_contract}), encoding="utf-8"
        )
        return temporary, package

    def test_contract_is_available_as_replay_metadata(self):
        temporary, package = self._package()
        try:
            finding = json.loads((package / "finding.json").read_text())
            contract = _load_contract(package, finding)
            self.assertEqual(contract["contract_id"], "contract-1")
        finally:
            temporary.cleanup()

    def test_contract_and_source_hash_mismatch_do_not_block_replay(self):
        temporary, package = self._package(finding_contract="other")
        try:
            (package / "source" / "target.st").write_text("edited source", encoding="utf-8")
            finding = json.loads((package / "finding.json").read_text())
            self.assertEqual(_load_contract(package, finding)["contract_id"], "contract-1")
        finally:
            temporary.cleanup()

    def test_packaged_binary_is_selected_without_hash_gate(self):
        temporary, package = self._package()
        try:
            target = package / "binary" / "fuzz_target"
            target.parent.mkdir()
            target.write_bytes(b"different binary")
            contract = json.loads((package / "execution-contract.json").read_text())
            contract["capabilities"]["target_binary_sha256"] = hashlib.sha256(b"expected").hexdigest()
            finding = json.loads((package / "finding.json").read_text())
            self.assertEqual(_target_from_contract(package, finding, contract), target)
        finally:
            temporary.cleanup()

    def test_rebuild_recipe_preserves_profile_and_packaged_resources(self):
        temporary, package = self._package()
        try:
            (package / "minimized.txt").write_text("0.X,INT,1\n", encoding="utf-8")
            (package / "repro-manifest.json").write_text(json.dumps({
                "profile": "codesys-memory",
                "resources": [],
            }), encoding="utf-8")
            (package / "finding.json").write_text(json.dumps({"function": "F"}), encoding="utf-8")
            class Completed:
                pid = 123
                returncode = 1

                def communicate(self, timeout):
                    del timeout
                    return "", ""

            with patch("fuzzer.pipeline.replay.subprocess.Popen", return_value=Completed()) as run:
                _build_from_package(package, Path(temporary.name) / "work")
                command = run.call_args.args[0]
            self.assertIn("--profile", command)
            self.assertEqual(command[command.index("--profile") + 1], "codesys-memory")
        finally:
            temporary.cleanup()

    def test_rebuild_timeout_is_a_failed_replay(self):
        temporary, package = self._package()
        try:
            (package / "repro-manifest.json").write_text(json.dumps({"resources": []}), encoding="utf-8")
            (package / "finding.json").write_text(json.dumps({"function": "F"}), encoding="utf-8")
            import subprocess

            class TimedOut:
                pid = 123
                returncode = None

                def communicate(self, timeout=None):
                    del timeout
                    if self.returncode is None:
                        raise subprocess.TimeoutExpired(["python"], 180)
                    return "", ""

                def wait(self, timeout=None):
                    del timeout
                    return None

            with patch("fuzzer.pipeline.replay.subprocess.Popen", return_value=TimedOut()), \
                    patch("fuzzer.pipeline.replay.os.killpg"):
                self.assertIsNone(_build_from_package(package, Path(temporary.name) / "work"))
        finally:
            temporary.cleanup()

    def test_repro_manifest_copies_relative_compatibility_tree(self):
        temporary = tempfile.TemporaryDirectory()
        try:
            root = Path(temporary.name)
            intake = root / "intake"
            (intake / "normalized").mkdir(parents=True)
            (intake / "normalized" / "lib.st").write_text(
                "FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n", encoding="utf-8"
            )
            manifest = intake / "compatibility.json"
            manifest.write_text(json.dumps({
                "schema_version": "semantist.compatibility/1.0.0",
                "name": "fixture",
                "dialect": "generic",
                "profiles": [],
                "library": {"normalized_sources": ["normalized/lib.st"]},
                "native": {"sources": [], "objects": [], "libraries": []},
                "toolchain": {},
            }), encoding="utf-8")
            st_file = root / "target.st"
            st_file.write_text("FUNCTION F : INT\nF := 1;\nEND_FUNCTION\n", encoding="utf-8")
            bundle = root / "bundle"
            bundle.mkdir()
            campaign = object.__new__(Campaign)
            campaign.run_dir = root / "run"
            campaign.bundle = bundle
            campaign.target_binary = root / "missing-target"
            campaign.st_file = st_file
            campaign.function = "F"
            campaign.config = {"compatibility_manifest": manifest, "minimize": False}
            campaign._abi_like = lambda: object()
            candidate = Candidate(
                "F0001", b"0.X,INT,1\n", Observation(0, False, ""),
                {"oracle": "array_access", "runtime_id": 1, "source": None}, {}, 0.0,
            )
            campaign._write_repro(candidate, {"finding_id": "F0001", "oracle": "array_access"})
            copied = campaign.run_dir / "findings" / "confirmed" / "F0001" / "source" / "compatibility-manifest" / "compatibility.json"
            assert copied.exists()
            load_project(copied)
        finally:
            temporary.cleanup()
