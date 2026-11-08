import json
import hashlib
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from fuzzer.llm.client import LlmClient, LlmError
from fuzzer.llm.config import LlmConfig
from fuzzer.llm.plan import PlanRejected, expand_plan, validate_plan

ABI = {
    "frame_fields": [
        {"name": "Start", "spec": {"kind": "scalar", "name": "BOOL", "size": 1, "c_type": "uint8_t"}},
        {"name": "Level", "spec": {"kind": "scalar", "name": "INT", "size": 2, "c_type": "int16_t"}},
    ],
    "shared_fields": [
        {"name": "Counter", "block_kind": "VAR_IN_OUT",
         "spec": {"kind": "scalar", "name": "UINT", "size": 2, "c_type": "uint16_t"}},
    ],
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "response_format" in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"unsupported")
            return
        reply = {"choices": [{"message": {"content": json.dumps({"ok": True})}}], "usage": {"total_tokens": 3}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())

    def log_message(self, *args):
        pass


class LlmTests(unittest.TestCase):
    def test_concurrent_requests_share_a_strict_call_budget(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                time.sleep(0.05)
                return json.dumps({
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"total_tokens": 1},
                }).encode()

        config = LlmConfig("http://unused", "test-key", "test-model", "test")
        client = LlmClient(config, max_calls=1, retries=0)
        with patch("fuzzer.llm.client.urllib.request.urlopen", return_value=Response()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(client.complete, [{"role": "user", "content": str(i)}])
                           for i in range(2)]
                results = []
                for future in futures:
                    try:
                        results.append(future.result())
                    except LlmError:
                        results.append(None)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(client.calls, 1)

    def test_json_object_fallback(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = LlmConfig(f"http://127.0.0.1:{server.server_port}", "test-key", "test-model", "test")
            client = LlmClient(config, max_calls=1)
            response = client.complete([{"role": "user", "content": "hi"}])
            self.assertTrue(json.loads(response.content)["ok"])
        finally:
            server.shutdown()

    def test_cache_writes_an_entry_and_can_be_replayed_offline(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = LlmConfig(f"http://127.0.0.1:{server.server_port}", "test-key", "test-model", "test")
            with tempfile.TemporaryDirectory() as directory:
                cache = Path(directory)
                client = LlmClient(config, cache_dirs=[cache], max_calls=1)
                client.complete([{"role": "user", "content": "cached"}])
                entries = list(cache.glob("*.json"))
                self.assertEqual(len(entries), 1)
                replay = LlmClient(config, cache_dirs=[cache], offline=True, max_calls=0)
                self.assertTrue(json.loads(replay.complete([{"role": "user", "content": "cached"}]).content)["ok"])
        finally:
            server.shutdown()

    def test_offline_cache_does_not_require_endpoint_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            config = LlmConfig(None, None, "test-model", "missing")
            online = LlmClient(
                LlmConfig("http://unused", "test-key", "test-model", "test"),
                cache_dirs=[cache],
                max_calls=0,
            )
            messages = [{"role": "user", "content": "offline-no-config"}]
            payload = json.dumps(
                {
                    "model": "test-model",
                    "messages": messages,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
                sort_keys=True,
            )
            digest = online._cache_path(hashlib.sha256(payload.encode("utf-8")).hexdigest(), 0)
            digest.parent.mkdir(parents=True, exist_ok=True)
            digest.write_text(
                json.dumps({"content": '{"ok": true}', "usage": {}, "model": "test-model"}),
                encoding="utf-8",
            )
            replay = LlmClient(config, cache_dirs=[cache], offline=True, max_calls=0)
            self.assertTrue(json.loads(replay.complete(messages).content)["ok"])

    def test_plan_validation_and_expansion(self):
        plan = {
            "plan_id": "p-test",
            "actions": [
                {"op": "set", "inputs": {"Start": 1, "Level": 700}},
                {"op": "hold", "inputs": {"Start": 1}, "frames": 3},
                {"op": "pulse", "input": "Start"},
            ]
        }
        validate_plan(plan, abi=ABI, contract_id=None)
        frames = expand_plan(plan, {}, ABI)
        self.assertEqual(frames[-1]["Start"], "1")
        self.assertGreaterEqual(len(frames), 4)

    def test_member_plan_keeps_protocol_controls_in_aggregate_parent(self):
        abi = {"frame_fields": [{
            "name": "CTRL", "spec": {
                "kind": "opaque", "name": "XML_CONTROL", "byte_size": 8,
                "members": [
                    {"name": "COMMAND", "offset": 0, "size": 2,
                     "spec": {"kind": "scalar", "name": "WORD", "size": 2}},
                    {"name": "STOP_POS", "offset": 2, "size": 2,
                     "spec": {"kind": "scalar", "name": "UINT", "size": 2}},
                ],
            },
        }], "shared_fields": []}
        plan = {"plan_id": "member", "actions": [{"op": "set", "inputs": {"CTRL.COMMAND": 1}}]}
        validate_plan(plan, abi=abi, contract_id=None)
        frames = expand_plan(plan, {}, abi)
        raw = bytes.fromhex(frames[0]["CTRL"][2:])
        self.assertEqual(raw[:4], b"\x01\x80\x03\x00")

    def test_unknown_input_is_rejected(self):
        with self.assertRaises(PlanRejected):
            validate_plan({"actions": [{"op": "set", "inputs": {"Nope": 1}}]}, abi=ABI, contract_id=None)

    def test_plan_identity_and_shape_are_required_when_bound(self):
        with self.assertRaises(PlanRejected):
            validate_plan(
                {"actions": [{"op": "set", "inputs": {"Start": 1}}]},
                abi=ABI,
                contract_id="contract",
                task_id="task",
            )
        with self.assertRaises(PlanRejected):
            validate_plan(
                {
                    "plan_id": "p-test",
                    "contract_id": "contract",
                    "task_id": "task",
                    "start_from": [],
                    "actions": [{"op": "set", "inputs": {"Start": 1}}],
                },
                abi=ABI,
                contract_id="contract",
                task_id="task",
            )

    def test_wrapper_preserves_outer_task_and_contract_provenance(self):
        plan = {
            "plan_id": "outer-plan",
            "task_id": "outer-task",
            "contract_id": "outer-contract",
            "plan": {
                "task_id": "inner-task",
                "contract_id": "inner-contract",
                "actions": [{"op": "wait", "frames": 1}],
            },
        }
        normalized = validate_plan(
            plan,
            abi=ABI,
            contract_id="current-contract",
            task_id="outer-task",
        )
        self.assertEqual(normalized["task_id"], "outer-task")
        self.assertEqual(normalized["contract_id"], "outer-contract")

    def test_contract_provenance_is_not_a_hash_match_gate(self):
        plan = {
            "plan_id": "p-provenance",
            "contract_id": "different-provenance-value",
            "task_id": "task",
            "actions": [{"op": "set", "inputs": {"Start": 1}}],
        }
        accepted = validate_plan(
            plan,
            abi=ABI,
            contract_id="current-contract-provenance",
            task_id="task",
        )
        self.assertEqual(accepted["contract_id"], "different-provenance-value")

    def test_nested_repeat_budget_matches_expanded_frames(self):
        plan = {
            "actions": [{
                "op": "repeat",
                "times": 10,
                "actions": [{"op": "repeat", "times": 10, "actions": [
                    {"op": "hold", "inputs": {"Start": 1}, "frames": 2}
                ]}],
            }]
        }
        with self.assertRaises(PlanRejected):
            validate_plan(plan, abi=ABI, contract_id=None, max_frames=100)

    def test_var_inout_is_controllable_and_unsigned_domain_is_preserved(self):
        validate_plan(
            {"plan_id": "p", "actions": [{"op": "set", "inputs": {"Counter": 65535}}]},
            abi=ABI, contract_id=None,
        )
        with self.assertRaises(PlanRejected):
            validate_plan(
                {"plan_id": "p", "actions": [{"op": "set", "inputs": {"Counter": -1}}]},
                abi=ABI, contract_id=None,
            )

    def test_array_plan_accepts_only_bounded_frame_bytes_and_fills_defaults(self):
        abi = {"frame_fields": [{
            "name": "Enable", "spec": {"kind": "scalar", "name": "BOOL", "size": 1},
        }], "shared_fields": [{
            "name": "InRecv", "block_kind": "VAR_IN_OUT",
            "spec": {"kind": "array", "name": "ARRAY OF LREAL", "byte_size": 16,
                     "count": 2, "element": {"kind": "scalar", "name": "LREAL", "size": 8}},
        }]}
        for invalid in ("0", "0x123", "0x" + "ff" * 17, [0.0, 1.0]):
            with self.subTest(value=invalid), self.assertRaises(PlanRejected):
                validate_plan({"plan_id": "array", "actions": [
                    {"op": "set", "inputs": {"InRecv": invalid}},
                ]}, abi=abi, contract_id=None)
        plan = {"plan_id": "array", "actions": [
            {"op": "set", "inputs": {"InRecv": "0x00"}},
            {"op": "wait", "frames": 1},
        ]}
        validate_plan(plan, abi=abi, contract_id=None)
        frames = expand_plan(plan, {"Enable": "1"}, abi)
        self.assertEqual(frames, [
            {"Enable": "1", "InRecv": "0x00"},
            {"Enable": "1", "InRecv": "0x00"},
        ])

    def test_evidence_references_are_bound_to_request(self):
        plan = {"plan_id": "p", "evidence_refs": ["source:a:1"],
                "actions": [{"op": "wait", "frames": 1}]}
        validate_plan(plan, abi=ABI, contract_id=None,
                      allowed_evidence_refs={"source:a:1"})
        with self.assertRaises(PlanRejected):
            validate_plan(plan, abi=ABI, contract_id=None,
                          allowed_evidence_refs={"source:b:2"})

    def test_start_from_prefix_must_be_reachable(self):
        plan = {"plan_id": "p", "start_from": {"prefix_id": "p-missing"},
                "actions": [{"op": "wait", "frames": 1}]}
        with self.assertRaises(PlanRejected):
            validate_plan(plan, abi=ABI, contract_id=None,
                          reachable_prefix_ids={"p-reachable"})
