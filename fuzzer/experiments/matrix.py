"""The six predeclared comparisons and their reusable search jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "benchmarks/oscat/manifest.json"
SELECTION = ROOT / "benchmarks/experiments/selection.json"
SEEDS = range(5)


@dataclass
class Job:
    target: dict
    tool: str
    configuration: str
    seed: int
    online_seconds: int
    experiments: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return f"{self.target['id']}/{self.tool}/{self.configuration}/s{self.seed}/{self.online_seconds}s"

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "target": self.target,
            "tool": self.tool,
            "configuration": self.configuration,
            "seed": self.seed,
            "online_seconds": self.online_seconds,
            "experiments": self.experiments,
        }


def load_targets(manifest: Path = MANIFEST, selection: Path = SELECTION) -> tuple[list[dict], list[dict]]:
    roster = json.loads(manifest.read_text(encoding="utf-8"))["targets"]
    selected_ids = json.loads(selection.read_text(encoding="utf-8"))["focus_targets"]
    by_id = {item["id"]: item for item in roster}
    if len(by_id) != 100 or len(selected_ids) != 20 or len(set(selected_ids)) != 20:
        raise ValueError("paper roster must contain 100 unique targets and 20 unique focus targets")
    focus = [by_id[target_id] for target_id in selected_ids]
    if any(item["kind"] != "FUNCTION_BLOCK" for item in focus):
        raise ValueError("the focus roster must contain only FUNCTION_BLOCK targets")
    counts = {suite: sum(item["suite"] == suite for item in focus)
              for suite in ("oscat_basic", "oscat_network", "oscat_building")}
    if counts != {"oscat_basic": 16, "oscat_network": 2, "oscat_building": 2}:
        raise ValueError(f"incorrect focus roster composition: {counts}")
    return roster, focus


def make_matrix(manifest: Path = MANIFEST, selection: Path = SELECTION) -> list[Job]:
    roster, focus = load_targets(manifest, selection)
    jobs: dict[str, Job] = {}

    def add(experiment: str, targets: list[dict], tool: str, configuration: str,
            online_seconds: int) -> None:
        for target in targets:
            for seed in SEEDS:
                job = Job(target, tool, configuration, seed, online_seconds)
                existing = jobs.setdefault(job.job_id, job)
                if experiment not in existing.experiments:
                    existing.experiments.append(experiment)

    for tool in ("semantist", "icsquartz", "aflpp"):
        add("overall", roster, tool, "full", 300)
    for configuration in ("full", "no_llm", "no_klee", "no_native_stg"):
        add("core_ablation", roster, "semantist", configuration, 300)
    for configuration in ("single_frame", "ordinary_multiframe", "guided_multiframe"):
        add("multicycle", focus, "semantist", configuration, 300)
    for tool in ("semantist", "icsquartz"):
        for duration in (300, 600):
            add("budget", focus, tool, "full", duration)
    return list(jobs.values())


CONFIGURATIONS = {
    "full": {"llm": True},
    "improved": {"llm": True, "no_symbolic": True},
    "no_llm": {"llm": False},
    "no_klee": {"llm": True, "no_symbolic": True},
    "no_native_stg": {"llm": True, "no_stg_guidance": True},
    "single_frame": {"llm": False, "no_symbolic": True,
                     "no_stg_guidance": True, "single_frame": True},
    "ordinary_multiframe": {"llm": False, "no_symbolic": True,
                            "no_stg_guidance": True},
    "guided_multiframe": {"llm": False, "no_symbolic": True},
}


def validate_job(job: Job) -> None:
    if job.tool not in {"semantist", "icsquartz", "aflpp"}:
        raise ValueError(f"unknown tool: {job.tool}")
    if job.configuration not in CONFIGURATIONS:
        raise ValueError(f"unknown configuration: {job.configuration}")
    if job.tool != "semantist" and job.configuration != "full":
        raise ValueError("external tools do not accept SemantiST ablations")
    if job.configuration in {"single_frame", "ordinary_multiframe", "guided_multiframe"}:
        if job.target["kind"] != "FUNCTION_BLOCK":
            raise ValueError("multicycle comparison requires a FUNCTION_BLOCK")
    if job.online_seconds <= 0 or job.seed < 0:
        raise ValueError("online duration and seed must be nonnegative")
