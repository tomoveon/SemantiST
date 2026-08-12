#!/usr/bin/env python3
"""Validate the curated OSCAT semantic suite and its generated STG model."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def load_extractor(root: Path):
    path = root / "compiler" / "scripts" / "genfunction.py"
    spec = importlib.util.spec_from_file_location("semantist_genfunction", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.extract_pou


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"[oscat_semantic] {message}")


def node_category(node: dict) -> str:
    kind = node["kind"]
    return kind["category"] if isinstance(kind, dict) else kind


def predicate_kinds(pou: dict) -> set[str]:
    return {
        node["kind"]["kind"]
        for node in pou["graph"]["nodes"]
        if isinstance(node["kind"], dict) and node["kind"].get("category") == "predicate"
    }


def validate_expected(target: dict, pou: dict) -> None:
    expected = target.get("expects", {})
    lowered_node_mappings = [
        mapping
        for mapping in pou["mappings"]["nodes"]
        if mapping.get("source") and mapping["lowered_ast_ids"]
    ]
    lowered_expression_mappings = [
        mapping
        for mapping in pou["mappings"]["expressions"]
        if mapping.get("source") and mapping["lowered_ast_ids"]
    ]
    require(lowered_node_mappings, f"{target['id']} has no lowered-AST node mapping")
    require(lowered_expression_mappings, f"{target['id']} has no lowered-AST expression mapping")

    predicates = predicate_kinds(pou)
    outcomes = {
        node["outcome"]["role"]
        for node in pou["graph"]["nodes"]
        if node.get("outcome")
    }
    expressions = {expression["kind"]["kind"] for expression in pou["environment"]["expressions"]}
    hazards = {
        node["hazard"]["kind"]
        for node in pou["graph"]["nodes"]
        if node.get("hazard")
    }
    roles = {symbol["role"] for symbol in pou["environment"]["symbols"]}

    for value in expected.get("predicates", []):
        require(value in predicates, f"{target['id']} is missing predicate {value}")
    for value in expected.get("outcomes", []):
        require(value in outcomes, f"{target['id']} is missing outcome {value}")
    for value in expected.get("expressions", []):
        require(value in expressions, f"{target['id']} is missing expression {value}")
    for value in expected.get("hazards", []):
        require(value in hazards, f"{target['id']} is missing hazard {value}")
    for value in expected.get("variable_roles", []):
        require(value in roles, f"{target['id']} is missing variable role {value}")

    operations = [
        operation
        for edge in pou["graph"]["edges"]
        for operation in (edge.get("transfer") or {}).get("operations", [])
    ]
    if expected.get("return"):
        return_nodes = {
            node["id"]
            for node in pou["graph"]["nodes"]
            if (node.get("outcome") or {}).get("role") == "return"
        }
        return_transfers = [
            edge
            for edge in pou["graph"]["edges"]
            if any(
                operation["kind"] == "return"
                for operation in (edge.get("transfer") or {}).get("operations", [])
            )
        ]
        boundary = pou["graph"].get("cycle_exit_id") or pou["graph"]["exit_id"]
        require(return_nodes, f"{target['id']} has no return semantic node")
        require(return_transfers, f"{target['id']} has no return transfer")
        require(
            all(edge["to"] in return_nodes for edge in return_transfers)
            and all(
                any(
                    edge["from"] == return_node and edge["to"] == boundary
                    for edge in pou["graph"]["edges"]
                )
                for return_node in return_nodes
            ),
            f"{target['id']} return does not terminate at the invocation boundary",
        )
    if expected.get("loop_control"):
        require(any(node_category(node) == "loop_control" for node in pou["graph"]["nodes"]), f"{target['id']} has no loop-control node")
    if expected.get("calls"):
        require(bool(pou["environment"]["calls"]), f"{target['id']} has no call summary")
        require(
            pou["statistics"]["conservative_summary_count"] + pou["statistics"]["opaque_summary_count"] > 0,
            f"{target['id']} call precision is not reflected by transfer summaries",
        )
    if expected.get("retain"):
        require(any(symbol["retain"] for symbol in pou["environment"]["symbols"]), f"{target['id']} has no retained symbol")
    if expected.get("constant_symbols"):
        require(any(symbol["constant"] for symbol in pou["environment"]["symbols"]), f"{target['id']} has no constant symbol")

    state = pou["environment"].get("persistent_state")
    if expected.get("temporal"):
        require(state is not None, f"{target['id']} has no persistent-state schema")
        require(
            any(edge["kind"] == "temporal" for edge in pou["graph"]["edges"]),
            f"{target['id']} has no temporal edge",
        )
        require(
            not set(state["symbols"]) & set(state["cycle_inputs"]),
            f"{target['id']} carries ordinary cycle inputs as persistent state",
        )
    if expected.get("configuration_inputs"):
        require(state and state["configuration_symbols"], f"{target['id']} has no configuration inputs")
        symbols = {
            symbol["qualified_name"]: symbol for symbol in pou["environment"]["symbols"]
        }
        require(
            all(
                symbols[name]["configuration_source"]
                in {"iec_constant", "var_input_constant_annotation"}
                for name in state["configuration_symbols"]
            ),
            f"{target['id']} configuration inputs have no semantic provenance",
        )
        require(
            not set(state["configuration_symbols"])
            & (set(state["symbols"]) | set(state["cycle_inputs"])),
            f"{target['id']} configuration inputs overlap carried state or cycle inputs",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    model = json.loads(args.model.read_text(encoding="utf-8"))
    expected_model_schema = manifest.get("model_schema", model["schema_version"])
    require(model["schema_version"] == expected_model_schema, "model schema does not match manifest")
    require(model["statistics"]["error_count"] == 0, "generated model contains validation errors")
    require(
        "rusty_typed_lowered_ast" in model["metadata"]["semantic_sources"],
        "generated model does not declare its lowered-AST semantic source",
    )

    targets = manifest["targets"]
    require(len(targets) >= 16, "semantic suite is unexpectedly small")
    require({target["kind"] for target in targets} == {"FUNCTION", "FUNCTION_BLOCK"}, "suite must contain functions and function blocks")

    extract_pou = load_extractor(root)
    oscat_source = (root / manifest["source"]).read_text(encoding="utf-8", errors="replace")
    model_by_name = {pou["pou"]["qualified_name"].upper(): pou for pou in model["pous"]}
    require(len(model_by_name) == len(targets), "generated model contains missing or unexpected POUs")

    for target in targets:
        actual_name, actual_kind, source = extract_pou(oscat_source, target["function"])
        target_source = (args.manifest.parent / target["st_file"]).read_text(encoding="utf-8")
        require(target_source == source, f"{target['id']} is not a verbatim OSCAT extraction")
        require(actual_kind == target["kind"], f"{target['id']} kind differs from OSCAT")
        pou = model_by_name.get(actual_name.upper())
        require(pou is not None, f"{target['id']} is absent from the generated model")
        require(pou["pou"]["kind"] == actual_kind.lower(), f"{target['id']} model kind is incorrect")
        validate_expected(target, pou)

    print(
        f"[oscat_semantic] validated {len(targets)} real OSCAT POUs, "
        f"{model['statistics']['node_count']} nodes, "
        f"{model['statistics']['edge_count']} edges, "
        f"{model['statistics']['target_count']} targets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
