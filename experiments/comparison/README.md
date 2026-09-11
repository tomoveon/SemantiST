# Full Benchmark Comparison

This runner executes the external 90-target benchmark matrix across the current
four-tool profile:

- `semantist_full`
- `aflplusplus_full`
- `icsquartz_full`
- `structuredfuzzer_full`

`icsfuzz_full` remains available as an adapter for future opt-in runs, but the
smoke and complete profiles in this directory intentionally exclude it.

It follows `docs/experiment_standard.md` for manifest validation, deterministic
scheduling, fixed per-trial seeds, structured run records, support accounting,
and summary generation. Unsupported tool-target pairs and environment failures
still produce authoritative `run-result.json` records.

The host is the only orchestrator. It launches sibling Docker/Podman containers
for tool rows; the SemantiST container does not start child containers.

## Preflight

From the SemantiST repository root:

```bash
python3 experiments/comparison/run_full_comparison.py \
  --preflight-only \
  --artifact-root artifacts/experiments \
  --container-runtime auto \
  --cpus 1-8
```

The runner supports both Docker and Podman. `--container-runtime auto` tries an
accessible Docker engine first and then Podman, and records the selected engine
and version in `environment.json`. Use `--container-runtime docker` or
`--container-runtime podman` to require one engine. The `CONTAINER_RUNTIME`
environment variable provides the same default selection. `--container-platform`
sets an optional image platform; the old `--docker-platform` spelling remains an
alias.

Docker rows use `--cpuset-cpus`. Local rootless Podman rows are launched through
`taskset` instead, because many user sessions do not delegate the cpuset cgroup
controller; the container process still inherits the assigned single-CPU
affinity. The runner therefore requires `taskset` for Podman experiment rows.

This writes:

```text
artifacts/experiments/<experiment-id>/
├── environment.json
├── experiment-manifest.json
├── manifest.snapshot.json
├── support-matrix.csv
└── schedule.jsonl
```

## Smoke Run

Use the smoke profile before running the paper-scale budget. It selects one
target from each important layout family and runs one 20-second trial per
tool-target pair:

```bash
python3 experiments/comparison/run_smoke_comparison.py \
  --artifact-root artifacts/experiments \
  --overwrite
```

The smoke profile writes 12 scheduled rows:

```text
4 tools x 3 targets x 1 trial = 12 rows
```

It also runs common replay, common coverage, aggregation, and structural
validation. On a workstation without a usable container runtime or baseline
images, the corresponding rows end as `preprocessing_failed` or
`online_start_failed`. Those failures are structured experiment results, not
runner crashes.

## Full Run

The complete profile uses 90 targets, 5 trials, and 300 seconds per online row:

```bash
python3 experiments/comparison/run_complete_comparison.py \
  --manifest /myfuzzer/SemantiST/benchmarks/external/manifest.json \
  --artifact-root /myfuzzer/artifacts/experiments \
  --workspace-root /myfuzzer \
  --icsquartz-root /myfuzzer/ICSQuartz
```

The complete profile writes 1800 scheduled rows:

```text
4 tools x 90 targets x 5 trials = 1800 rows
```

The output directory contains `raw-runs.jsonl`, `raw-runs.csv`, and summaries
under `summaries/`.

## Initial Seeds

The runner does not distribute one unified seed corpus across tools. Each
adapter creates or delegates its own native initial inputs and writes a
`seed-manifest.json` when those inputs are host-visible:

- `semantist_full`: SemantiST harness generator creates structured seed files;
  STG semantic initial generation remains inside online time.
- `aflplusplus_full`: the AFL++ adapter supplies its own byte-level `0x00`
  seed corpus; SemantiST harness-generator output is kept out of AFL++ online
  inputs.
- `icsquartz_full`: ICSQuartz creates its native byte inputs internally at
  startup and receives only the deterministic runner RNG seed.
- `structuredfuzzer_full`: adapter supplies the native single-byte `0x00` seed
  corpus; the tool has no explicit RNG seed control.

## Validation

```bash
python3 experiments/comparison/analysis/validate_complete.py \
  --experiment-dir artifacts/experiments/<experiment-id>
```

Validation checks that every scheduled row has exactly one run record, no run is
left as `missing`, and unsupported records match the support matrix.

## Replay And Coverage

Common replay and coverage are separate best-effort passes:

```bash
python3 experiments/comparison/analysis/replay.py \
  --experiment-dir artifacts/experiments/<experiment-id>

python3 experiments/comparison/analysis/coverage.py \
  --experiment-dir artifacts/experiments/<experiment-id>
```

They do not guess lossy conversions. If a tool corpus cannot be represented as
the common stdin harness format, the replay/coverage result is marked
`conversion_unsupported` or `not-run`.
