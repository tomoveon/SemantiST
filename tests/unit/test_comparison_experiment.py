import csv
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.comparison.adapters import ADAPTERS, DEFAULT_TOOL_ORDER  # noqa: E402
from experiments.comparison.adapters.aflplusplus import (  # noqa: E402
    AFLPlusPlusAdapter,
    _afl_artifact_files,
    _parse_afl_name,
)
from experiments.comparison.adapters.base import ExperimentConfig, PreprocessingResult  # noqa: E402
from experiments.comparison.adapters.icsquartz import (  # noqa: E402
    _fuzzer_log_failure,
    _launcher_core_spec,
    _latest_total_executions,
)
from experiments.comparison.adapters.semantist import SemantiSTAdapter  # noqa: E402
from experiments.baselines import build_images  # noqa: E402
from experiments.comparison.analysis import coverage as coverage_analysis  # noqa: E402
from experiments.comparison.analysis.replay import replay_seed  # noqa: E402
from experiments.comparison.lib.hashing import rng_seed, sha256_text  # noqa: E402
from experiments.comparison.lib.manifest import load_manifest  # noqa: E402
from experiments.comparison.lib.process import (  # noqa: E402
    bind_mount,
    container_cpu_bound_command,
    cpu_bound_command,
    parse_cpus,
    resolve_container_runtime,
    run_with_budget,
    unavailable_cpus,
)
from experiments.comparison.lib.schema import (  # noqa: E402
    run_result_template,
    validate_run_result,
)
from experiments.comparison.run_full_comparison import (  # noqa: E402
    make_base_result,
    parse_args,
    validate_execution_environment,
    write_schedule,
    write_support_matrix,
)


def comparison_config(artifact_root: Path, runtime: str = "podman") -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id="test",
        semantist_root=ROOT,
        workspace_root=ROOT.parent,
        artifact_root=artifact_root,
        icsquartz_root=ROOT.parent / "ICSQuartz",
        online_budget_seconds=1,
        per_execution_timeout_ms=1000,
        semantic_task_budget=1000,
        semantist_fb_max_cycles=10000,
        semantist_fb_stale_threshold=0,
        semantist_fb_state_trace=True,
        observer_poll_interval_ms=10,
        cpuset=None,
        container_runtime=runtime,
        container_runtime_status=f"{runtime} info succeeded",
        container_platform=None,
        semantist_image="semantist:0.1.0",
    )


class ComparisonExperimentTests(unittest.TestCase):
    @patch("experiments.comparison.run_full_comparison.container_image_id", return_value=None)
    def test_execution_environment_rejects_missing_images(self, _image_id):
        with self.assertRaisesRegex(SystemExit, "required container image"):
            validate_execution_environment(
                "docker", "docker info succeeded", ["icsquartz_full"],
                "semantist:test", None,
            )

    @patch("experiments.comparison.run_full_comparison.subprocess.run")
    @patch("experiments.comparison.run_full_comparison.container_run_command", return_value=["probe"])
    @patch("experiments.comparison.run_full_comparison.container_image_id", return_value="sha256:test")
    def test_execution_environment_probes_current_semantist_cli(
        self, _image_id, _command, run
    ):
        run.return_value = SimpleNamespace(returncode=0, stdout="--ablation <PROFILE>", stderr="")
        validate_execution_environment(
            "docker", "docker info succeeded", ["semantist_full"],
            "semantist:test", None,
        )
        run.return_value = SimpleNamespace(returncode=0, stdout="old help", stderr="")
        with self.assertRaisesRegex(SystemExit, "current experiment CLI"):
            validate_execution_environment(
                "docker", "docker info succeeded", ["semantist_full"],
                "semantist:test", None,
            )

    def test_comparison_disables_semantist_only_state_trace_by_default(self):
        self.assertFalse(parse_args([]).semantist_fb_state_trace)
        self.assertTrue(parse_args(["--semantist-fb-state-trace"]).semantist_fb_state_trace)

    def test_semantist_ablation_is_recorded_per_trial(self):
        target = SimpleNamespace(
            target_id="target", suite="suite", kind="FUNCTION_BLOCK", function="FB",
            st_file_sha256="source", fixture_hashes={"harness.c": "harness"},
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            config = replace(
                comparison_config(Path(raw_tmp)),
                semantist_ablation="no_multicycle_guidance",
            )
            adapter = SemantiSTAdapter(config)
            result = make_base_result(
                {"run_key": "exp|semantist_full|suite|target|trial_01",
                 "tool": "semantist_full", "trial_id": 1, "rng_seed": 123},
                target, adapter, "supported",
            )
        self.assertEqual(result["configuration_id"], "no_multicycle_guidance")
        self.assertEqual(result["execution_model"], "stateful_semantic_sequence")

    def test_afl_artifacts_and_counters_survive_windows_colon_encoding(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            encoded = root / "id\uf03a000000,sig\uf03a06,time\uf03a260,execs\uf03a10,op\uf03ahavoc"
            encoded.write_bytes(b"crash")
            (root / "README.txt").write_text("metadata", encoding="utf-8")
            self.assertEqual(_afl_artifact_files(root), [encoded])
            self.assertEqual(_parse_afl_name(encoded.name), (0.26, 10))

    def test_icsquartz_progress_reads_active_client_counter(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            stats = Path(raw_tmp) / "fuzzer_stats.json"
            stats.write_text(
                json.dumps(
                    {
                        "executions": 2,
                        "client_stats": [
                            {"enabled": False, "executions": 0},
                            {
                                "enabled": True,
                                "executions": 37,
                                "user_monitor": {
                                    "executions_": {"value": {"Number": 100}}
                                },
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(_latest_total_executions(stats), 100)

    def test_icsquartz_uses_visible_core_ordinal_inside_singleton_cpuset(self):
        self.assertEqual(_launcher_core_spec([7]), "0")
        with self.assertRaisesRegex(ValueError, "exactly one CPU"):
            _launcher_core_spec([7, 8])

    def test_icsquartz_fatal_client_log_is_not_a_clean_early_exit(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            log = Path(raw_tmp) / "fuzzer_log"
            log.write_text(
                "Fuzzer-respawner: Storing state in crashed fuzzer instance did not work\n",
                encoding="utf-8",
            )
            self.assertIsNotNone(_fuzzer_log_failure(log))

    def test_parse_cpus_supports_ranges_and_commas(self):
        self.assertEqual(parse_cpus("1-3,5,3"), [1, 2, 3, 5])

    def test_cpu_bound_command_uses_one_logical_cpu(self):
        with patch("experiments.comparison.lib.process.shutil.which", return_value="taskset"):
            command = cpu_bound_command(["afl-fuzz", "--", "target"], 7)
        self.assertEqual(Path(command[0]).name, "taskset")
        self.assertEqual(command[1:3], ["--cpu-list", "7"])
        self.assertEqual(command[3:], ["afl-fuzz", "--", "target"])

    @patch("experiments.comparison.lib.process.container_runtime_accessible")
    def test_container_runtime_auto_falls_back_to_podman(self, accessible):
        accessible.side_effect = [
            (False, "docker daemon unavailable"),
            (True, "podman info succeeded"),
        ]
        runtime, status = resolve_container_runtime("auto")
        self.assertEqual(runtime, "podman")
        self.assertEqual(status, "podman info succeeded")
        self.assertEqual(accessible.call_args_list[0].args, ("docker",))
        self.assertEqual(accessible.call_args_list[1].args, ("podman",))

    @patch("experiments.comparison.lib.process.container_runtime_accessible")
    def test_explicit_container_runtime_does_not_fall_back(self, accessible):
        accessible.return_value = (False, "docker daemon unavailable")
        runtime, status = resolve_container_runtime("docker")
        self.assertIsNone(runtime)
        self.assertIn("docker daemon unavailable", status)
        accessible.assert_called_once_with("docker")

    def test_bind_mount_adds_selinux_relabel_only_for_podman(self):
        source = Path("/tmp/input")
        self.assertEqual(
            bind_mount("docker", source, "/input", readonly=True),
            f"{source}:/input:ro",
        )
        self.assertEqual(
            bind_mount("podman", source, "/input", readonly=True),
            f"{source}:/input:ro,Z",
        )
        self.assertEqual(bind_mount("podman", source, "/input"), f"{source}:/input:Z")

    def test_semantist_container_command_mounts_source_readonly_and_artifacts_writable(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            adapter = SemantiSTAdapter(comparison_config(tmp_path))
            command = adapter._container_command({"PYTHONPATH": str(ROOT)}, ["true"])
            self.assertEqual(command[:2], ["podman", "run"])
            self.assertIn("semantist:0.1.0", command)
            self.assertIn(f"{ROOT}:/repo:ro,Z", command)
            self.assertIn(f"{tmp_path}:/artifacts:Z", command)

    def test_container_cpu_binding_supports_docker_and_rootless_podman(self):
        docker = container_cpu_bound_command(["docker", "run", "image"], "docker", 3)
        self.assertEqual(
            docker,
            ["docker", "run", "--cpuset-cpus", "3", "image"],
        )
        with patch("experiments.comparison.lib.process.shutil.which", return_value="taskset"):
            podman = container_cpu_bound_command(["podman", "run", "image"], "podman", 3)
        self.assertEqual(Path(podman[0]).name, "taskset")
        self.assertEqual(podman[1:5], ["--cpu-list", "3", "podman", "run"])

    @patch("experiments.comparison.adapters.aflplusplus.run_with_budget")
    def test_aflplusplus_online_command_runs_inside_baseline_container(self, run_with_budget_mock):
        run_with_budget_mock.return_value = {
            "started": True,
            "t0_monotonic_ns": 1,
            "elapsed_seconds": 1.0,
            "timed_out": True,
            "returncode": None,
            "events": [],
            "cleanup_confirmed": True,
            "infrastructure_failure": False,
            "collection_complete": True,
            "collection_mode": "post_run",
        }
        target = SimpleNamespace(target_id="target", suite="suite", kind="FUNCTION", function="TARGET")
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            run_dir = tmp_path / "test" / "runs" / "aflplusplus_full" / "suite" / "target" / "trial_01"
            (run_dir / "inputs").mkdir(parents=True)
            (run_dir / "tool-artifacts").mkdir(parents=True)
            (run_dir / "tool-artifacts" / "fuzz_target").write_bytes(b"")
            adapter = AFLPlusPlusAdapter(comparison_config(tmp_path))
            with patch("experiments.comparison.lib.process.shutil.which", return_value="taskset"):
                result = adapter.run(target, run_dir, PreprocessingResult("completed", tmp_path, 0, 0), 123, 3)
            command = json.loads((run_dir / "command.json").read_text(encoding="utf-8"))["command"]
        self.assertEqual(result["run_status"], "budget_completed")
        self.assertIn("podman", command)
        self.assertIn("semantist-aflplusplus-env:4.21c", command)
        self.assertIn("afl-fuzz", command)
        self.assertIn("-s", command)
        self.assertIn("123", command)

    @patch.object(build_images, "run")
    def test_baseline_commands_use_selected_container_runtime(self, run):
        config = {
            "dockerfile": "aflplusplus/Dockerfile",
            "tag": "baseline:test",
            "build_args": {"VERSION": "1"},
        }
        build_images.build("podman", "aflplusplus", config, "linux/amd64", pull=False)
        build_images.smoke("podman", "aflplusplus", config, "linux/amd64")
        self.assertEqual(len(run.call_args_list), 3)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0], "podman")

    def test_unavailable_cpus_respects_current_affinity(self):
        if not hasattr(os, "sched_getaffinity"):
            self.skipTest("sched_getaffinity is unavailable")

        allowed = sorted(os.sched_getaffinity(0))
        self.assertEqual(unavailable_cpus([allowed[0]]), [])
        self.assertEqual(unavailable_cpus([max(allowed) + 1]), [max(allowed) + 1])

    def test_budget_runner_collects_exit_findings_without_inventing_discovery_time(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            finding_path = tmp_path / "finding"

            def scan_findings():
                if not finding_path.is_file():
                    return []
                return [{"candidate_id": "finding-1", "observed_monotonic_ns": None}]

            result = run_with_budget(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(finding_path)!r}).write_text('found')"
                    ),
                ],
                cwd=tmp_path,
                env=None,
                stdout_path=tmp_path / "stdout.log",
                stderr_path=tmp_path / "stderr.log",
                budget_seconds=2,
                poll_interval_ms=10,
                scan_findings=scan_findings,
            )
            self.assertTrue(result["started"])
            self.assertEqual(len(result["events"]), 1)
            self.assertIsNone(result["events"][0]["observed_monotonic_ns"])
            self.assertIsNone(result["events"][0]["observed_elapsed_seconds"])
            self.assertIsNone(result["events"][0]["observed_after_budget"])
            self.assertEqual(result["events"][0]["time_source"], "unmeasured_post_run")
            self.assertEqual(result["collection_mode"], "post_run")
            self.assertTrue(result["collection_complete"])
            self.assertTrue(result["cleanup_confirmed"])

    def test_run_result_validation_uses_common_schema_and_consistency_rules(self):
        target = SimpleNamespace(
            target_id="target",
            suite="suite",
            kind="FUNCTION",
            function="TARGET",
        )
        result = run_result_template(
            experiment_id="experiment",
            run_key="experiment|semantist_full|suite|target|trial_01",
            tool="semantist_full",
            target=target,
            trial_id=1,
            rng_seed_requested=1,
            rng_seed_applied=1,
            rng_seed_status="applied",
            online_budget_seconds=1,
            per_execution_timeout_ms=1000,
            execution_timeout_model="per_process",
            support_status="supported",
            observer_poll_interval_ms=5,
        )
        result["run_status"] = "budget_completed"
        validate_run_result(result)

        invalid_tool = dict(result, tool="not-a-tool")
        with self.assertRaisesRegex(ValueError, "invalid tool"):
            validate_run_result(invalid_tool)

        inconsistent_finding = dict(result, finding_found=True)
        with self.assertRaisesRegex(ValueError, "finding_found"):
            validate_run_result(inconsistent_finding)

    def test_make_base_result_records_tool_owned_initial_seed_provider(self):
        target = SimpleNamespace(
            target_id="target",
            suite="suite",
            kind="FUNCTION",
            function="TARGET",
            st_file_sha256="test-source-hash",
            fixture_hashes={"harness.c": "test-harness-hash"},
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            adapter = ADAPTERS["aflplusplus_full"](comparison_config(Path(raw_tmp)))
            result = make_base_result(
                {
                    "run_key": "exp|aflplusplus_full|suite|target|trial_01",
                    "tool": "aflplusplus_full",
                    "trial_id": 1,
                    "rng_seed": 123,
                },
                target,
                adapter,
                "supported",
            )
        self.assertEqual(result["rng_seed_status"], "applied")
        self.assertEqual(result["initial_seed_provider"], "aflplusplus_adapter")
        self.assertEqual(result["initial_seed_policy"], "icsquartz_aflplusplus_echo_1_seed")

    def test_replay_seed_feeds_testcase_on_stdin(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            target = tmp_path / "target.py"
            target.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "data = sys.stdin.buffer.read()\n"
                "if data == b'crash': print('ERROR: AddressSanitizer: heap-buffer-overflow', file=sys.stderr)\n"
                "sys.exit(1 if data == b'crash' else 0)\n",
                encoding="utf-8",
            )
            target.chmod(0o755)
            seed = tmp_path / "seed"
            seed.write_bytes(b"crash")
            outcome = replay_seed(tmp_path, target, seed, 2.0)
        self.assertTrue(outcome["confirmed"])
        self.assertEqual(outcome["returncode"], 1)

    @patch("experiments.comparison.analysis.replay.subprocess.run")
    def test_container_replay_budget_excludes_container_startup(self, run_mock):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            target = tmp_path / "fuzz_target"
            seed = tmp_path / "seed"
            target.write_bytes(b"")
            seed.write_bytes(b"X,INT,1\n")
            run_mock.return_value = SimpleNamespace(returncode=124, stdout=b"", stderr=b"")
            outcome = replay_seed(tmp_path, target, seed, 2.0, container_runtime="podman", semantist_image="img")
            run_mock.return_value = SimpleNamespace(returncode=134, stdout=b"", stderr=b"panicked at x.rs:1:1")
            crashed = replay_seed(tmp_path, target, seed, 2.0, container_runtime="podman", semantist_image="img")
        command = run_mock.call_args.args[0]
        self.assertEqual(command[-3:], ["timeout", "2", "/replay/fuzz_target"])
        self.assertGreater(run_mock.call_args.kwargs["timeout"], 2.0)
        self.assertEqual((outcome["status"], outcome["reason"]), ("unstable", "timeout"))
        self.assertEqual((crashed["status"], crashed["fault_class"]), ("confirmed", "toolchain_panic"))

    @patch("experiments.comparison.analysis.coverage.subprocess.run")
    def test_coverage_showmap_feeds_testcase_on_stdin(self, run_mock):
        run_mock.return_value = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            target = tmp_path / "target"
            testcase = tmp_path / "case"
            trace = tmp_path / "trace"
            target.write_bytes(b"")
            testcase.write_bytes(b"input")
            coverage_analysis.run_showmap(
                tmp_path,
                target,
                testcase,
                trace,
                2.0,
                afl_showmap="/usr/bin/afl-showmap",
            )
        command = run_mock.call_args.args[0]
        self.assertEqual(run_mock.call_args.kwargs["input"], b"input")
        self.assertNotIn(str(testcase), command)

    def test_manifest_loads_authoritative_90_target_set(self):
        manifest = load_manifest(ROOT / "benchmarks" / "external" / "manifest.json", ROOT)
        self.assertEqual(
            manifest.sha256,
            "26165651552907599c50d8580e2d7dc3f1a19ac6030211fd257f9a23694abf47",
        )
        self.assertEqual(len(manifest.targets), 90)
        self.assertEqual(sum(target.suite == "oscat_basic" for target in manifest.targets), 61)
        self.assertTrue({"_STRING_TO_BUFFER", "INC", "INC2", "REAL_TO_STRF", "WEEKDAY_TO_STRING"}
                        <= {target.function for target in manifest.targets})
        self.assertEqual(sum(target.suite == "icsquartz_icsfuzz" for target in manifest.targets), 17)
        self.assertEqual(sum(target.suite == "icsquartz_scan_cycle" for target in manifest.targets), 12)

    def test_schedule_uses_documented_run_key_seed_and_sort_key(self):
        manifest = load_manifest(ROOT / "benchmarks" / "external" / "manifest.json", ROOT)
        target = manifest.targets[0]
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            rows = write_schedule(tmp_path, "exp", ["semantist_full"], [target], 1)
            run_key = f"exp|semantist_full|{target.suite}|{target.target_id}|trial_01"
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_key"], run_key)
            self.assertEqual(
                rows[0]["rng_seed"],
                rng_seed(f"exp|{target.suite}|{target.target_id}|trial_01"),
            )
            self.assertEqual(
                rows[0]["sort_key"],
                sha256_text(f"exp|schedule|semantist_full|{target.suite}|{target.target_id}|1"),
            )
            self.assertEqual(
                json.loads((tmp_path / "schedule.jsonl").read_text(encoding="utf-8"))["run_key"],
                run_key,
            )

    def test_schedule_pairs_rng_seed_across_tools_and_experiment_directories(self):
        manifest = load_manifest(ROOT / "benchmarks" / "external" / "manifest.json", ROOT)
        target = manifest.targets[0]
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            (tmp_path / "first").mkdir()
            (tmp_path / "second").mkdir()
            first = write_schedule(
                tmp_path / "first",
                "sem-run",
                ["semantist_full", "aflplusplus_full"],
                [target],
                1,
                seed_namespace="campaign",
            )
            second = write_schedule(
                tmp_path / "second",
                "ics-run",
                ["icsquartz_full"],
                [target],
                1,
                seed_namespace="campaign",
            )
        self.assertEqual({row["rng_seed"] for row in [*first, *second]}, {first[0]["rng_seed"]})

    def test_support_matrix_counts_for_full_adapter_set(self):
        manifest = load_manifest(ROOT / "benchmarks" / "external" / "manifest.json", ROOT)
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            config = comparison_config(tmp_path)
            tools = list(DEFAULT_TOOL_ORDER)
            adapters = {tool: ADAPTERS[tool](config) for tool in tools}
            write_support_matrix(tmp_path, tools, manifest.targets, adapters)
            with (tmp_path / "support-matrix.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(DEFAULT_TOOL_ORDER, (
            "semantist_full",
            "aflplusplus_full",
            "icsquartz_full",
        ))
        self.assertIn("icsfuzz_full", ADAPTERS)
        self.assertEqual(len(rows), len(DEFAULT_TOOL_ORDER) * 90)
        counts = {}
        for row in rows:
            key = (row["tool"], row["support_status"])
            counts[key] = counts.get(key, 0) + 1
        self.assertEqual(counts[("semantist_full", "supported")], 90)
        self.assertEqual(counts[("aflplusplus_full", "supported")], 90)
        self.assertEqual(counts[("icsquartz_full", "supported")], 90)


if __name__ == "__main__":
    unittest.main()
