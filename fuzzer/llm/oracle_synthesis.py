"""Infer target output checks from retrieved manuals before test planning."""

from __future__ import annotations

import json
from pathlib import Path

from compiler.harness.abi import load_target_abi
from compiler.harness.oracle import OracleRejected, validate_document
from fuzzer.llm.client import LlmClient
from fuzzer.llm.config import load_config
from fuzzer.llm.retrieval import retrieve_manual


ORACLE_SYSTEM_MESSAGE = """You infer observable functional requirements for one
IEC 61131-3 Structured Text target. Return only a JSON object with
schema_version='semantist.generated-oracle/1' and properties (zero to three).
Use the cited manual excerpts, target comments, ABI, and source. Never assume
that integer wrap alone is a defect. An output requirement needs an independent
functional rationale. If evidence is insufficient, return properties=[].

Each property has exactly id, precision ('exact' or 'estimated'), evidence_refs,
when, and must. Cite only evidence IDs supplied in the request. Exact means
explicitly documented or a necessary consequence under the stated when guard;
estimated means a plausible but incompletely specified behavior. Use short,
stable ASCII ids. The when guard states every precondition; do not declare a
test invalid merely because the observed output differs from source code.

Expressions are JSON trees: {"const": number-or-bool},
{"field": "ABI_NAME", "at": "input|output|previous_input|previous_output"},
or {"op": "and|or|not|eq|ne|lt|le|gt|ge|add|sub|mul|min|max|abs|between|finite",
"args": [...]}. and/or and arithmetic/comparisons take two args; not, abs,
finite take one; between takes value, lower, upper. Previous observations refer
to the preceding PLC scan in the same cold testcase. The monitor automatically
requires a previous scan when one is referenced. Current input means caller
input before the target executes; output means the value after its scan.
Use only scalar ABI fields shown in the request; function returns are named
__RESULT. Avoid exact equality for rounded floating-point calculations.

Example for a documented moving-average block with X, N, RST and Y: when is
the conjunction of previous RST=true, current RST=false, current N>0, and
current N=previous N; must is Y between min(previous X,current X) and
max(previous X,current X). This is only an example of the JSON expression
shape; do not copy it for unrelated targets."""


def synthesize_oracle(root: Path, target: dict, build: Path, staging: Path) -> dict:
    abi_path = build / "template" / "bundle" / "abi.json"
    abi_json = json.loads(abi_path.read_text(encoding="utf-8"))
    abi = load_target_abi(build / "template" / "stg" / "stg-model.json",
                          build / "build" / "target.ll", target["function"])
    source_path = build / "source" / "target.st"
    source = source_path.read_text(encoding="utf-8", errors="replace")
    retrieved = retrieve_manual(root, target["suite"], target["function"])
    evidence = [{"id": f"manual:{item['source']}:p{item['page']}", **item}
                for item in retrieved]
    evidence.append({"id": "source:target.st", "source": "target.st",
                     "text": source[:15000], "truncated": len(source) > 15000})
    request = {
        "function": target["function"], "suite": target["suite"],
        "abi": {key: abi_json.get(key) for key in ("target", "frame_fields", "outputs", "return")},
        "evidence": evidence,
    }
    config = load_config()
    client = LlmClient(config, cache_dirs=[staging / "llm" / "oracle-cache"], max_calls=2)
    messages = [
        {"role": "system", "content": ORACLE_SYSTEM_MESSAGE},
        {"role": "user", "content": json.dumps(request, sort_keys=True)},
    ]
    attempts = []
    document = {"schema_version": "semantist.generated-oracle/1", "properties": []}
    properties = []
    status = "rejected"
    allowed_refs = {item["id"] for item in evidence}
    for _ in range(2):
        response = client.complete(messages)
        record = {"content": response.content, "tokens": response.usage.get("total_tokens", 0),
                  "model": response.model}
        attempts.append(record)
        try:
            candidate = json.loads(response.content.strip())
            candidate_properties = validate_document(abi, candidate)
            if any(reference not in allowed_refs for spec in candidate_properties
                   for reference in spec["evidence_refs"]):
                raise OracleRejected("generated property cites evidence not supplied to the model")
        except (json.JSONDecodeError, OracleRejected) as error:
            record["rejection"] = str(error)
            messages += [{"role": "assistant", "content": response.content},
                         {"role": "user", "content":
                          f"The oracle was rejected: {error}. Return a corrected JSON object. "
                          "Use properties=[] if no supported requirement can be expressed."}]
            continue
        document, properties = candidate, candidate_properties
        status = "generated" if properties else "empty"
        break
    raw_path = staging / "llm" / "oracle-responses.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(attempts, indent=2) + "\n", encoding="utf-8")
    output = staging / "generated-oracle.json"
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (staging / "llm" / "oracle-evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return {"properties": len(properties), "calls": len(attempts), "status": status,
            "rejections": [item["rejection"] for item in attempts if "rejection" in item],
            "tokens": sum(item["tokens"] or 0 for item in attempts),
            "model": attempts[-1]["model"],
            "evidence_refs": sorted({reference for spec in properties
                                     for reference in spec["evidence_refs"]})}
