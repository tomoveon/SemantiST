# SemantiST mechanism ablations

These runs use the same target preparation, checked-in harness and seed,
AddressSanitizer oracle, CPU interface, timeout, online budget, and raw result
schema as the main comparison. The only difference is the SemantiST runtime
profile (`--ablation`). Build one clean SemantiST image containing all profiles
before starting a paper run. Function-block state trace remains off by default.
The cycle limit defaults to 256; keep `--semantist-fb-max-cycles` identical
across profiles. Each run ID is exclusive, including preflight IDs. There is
no resume, preprocessing reuse, or overwrite mode.

| Profile | Mechanism removed | Suitable question |
| --- | --- | --- |
| `full` | None | Reference configuration |
| `no_semantic_init` | STG-generated initial candidates; the checked-in seed remains | Does semantic initial generation help? |
| `no_targeted_mutation` | Target/expression-guided mutations; structured havoc remains and task scheduling stays on | Does directed mutation add value? |
| `queue_havoc` | Semantic initial generation, target-aware scheduling, and directed mutation | Combined guidance lower bound; **not** a single-factor ablation |
| `no_transition_guidance` | Transfer-read-guided mutation of earlier FB cycles | Does transfer-aware prefix mutation help? |
| `no_prefix_growth` | Bounded semantic extension of the FB prefix | Does semantic sequence deepening help? |
| `no_multicycle_guidance` | Both transfer-aware prefix mutation and semantic prefix extension | Combined multi-cycle-guidance ablation; generic cycle havoc and multi-cycle harness remain |

`no_transition_guidance`, `no_prefix_growth`, and `no_multicycle_guidance`
should be assessed on `FUNCTION_BLOCK` targets. They do not remove the
multi-cycle input format or alter the harness, so the comparison isolates
guidance rather than changing what the target executes. `queue_havoc` still
retains the common semantic observer and oracle; it is a deliberately combined
reference, not an AFL++ replacement.

Run one profile independently:

```bash
python experiments/ablations/run_ablation.py \
  --variant no_semantic_init --run-id ablate_init_01 \
  --target-id oscat_basic_inc2 --cpus 1-2
```

Run paired profiles sequentially with the same target/trial RNG seed namespace
(trials within each profile use the supplied CPU pool; add `--serial` to run
only one trial at a time):

```bash
python experiments/ablations/run_ablation_suite.py \
  --run-prefix ablate_fb_01 \
  --variants full,no_transition_guidance,no_prefix_growth,no_multicycle_guidance \
  --target-id scan_cycle_smart_oobw_1 --cpus 1-2
```

Defaults are 300 seconds and five trials, inherited from the main preset.
Use `--fuzz-time` / `--fuzz-trials` to override them; `--trials` remains valid.
The image can be selected once with `SEMANTIST_IMAGE`, as in the main runner.
Omit `--target-id` to use the main benchmark manifest. The suite produces a
fresh independent experiment directory for each profile and stops if an entry
point returns a nonzero status. Individual failed trials remain recorded for
inspection; every online entry rejects supported trials that did not complete
their budget with valid measurements and positive native execution counts.
No replay, vulnerability deduplication, significance test, or final
interpretation is performed automatically. Each directory contains the normal
`run-result.json`, `raw-runs.jsonl`, and `metrics.csv`; the profile is recorded
as `configuration_id` in each SemantiST trial and in the experiment manifest.
After a failure, preserve its directory and rerun the chosen group with a new
prefix/ID. Concurrent runner processes must use non-overlapping CPU pools.

The main `analysis/compare_tools.py` comparison accepts only the `full`
SemantiST profile. Keep ablation directories separate and analyze their
exported `metrics.csv`/`metrics.jsonl` manually by `configuration_id`. To
refresh one directory's exports manually if needed (successful online runs
already produce them):

```bash
python - artifacts/experiments/ablate_init_01 <<'PY'
import sys
from pathlib import Path
from experiments.comparison.analysis.compare_tools import load_runs, export_metrics
root = Path(sys.argv[1])
export_metrics(load_runs([root]), root)
PY
```

For a quick interface check only:

```bash
python experiments/ablations/run_ablation_smoke.py \
  --run-prefix ablation_smoke_new --semantist-image semantist:0.1.0
```

The smoke is not paper evidence. For paper claims, keep budgets, targets,
trials, image, trace setting, and seed namespace fixed, and interpret findings
manually. Initial generation runs *inside* the online budget, as in the main
experiment; removing it intentionally removes both its candidate inputs and
its startup cost. Do not rank function-block executions against single-cycle
executions as if they were the same unit.
