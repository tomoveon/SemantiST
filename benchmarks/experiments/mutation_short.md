# Semantic/raw short test

`mutation_short.json` freezes six FUNCTION_BLOCK targets, seed `0`, and a
10-second smoke budget for each of `semantic-only`, `byte-only`, and
`combined`. The combined mode uses semantic ratio `0.30`, so its target split
is semantic 30% and raw byte 70%.

Create the plan without starting a search:

```bash
python3 -m fuzzer.cli.mutation_short \
  --root /tmp/semantist-mutation-short
```

Run the three modes in separate experiment roots with the existing runner.
For each target, pass `--experiment core_ablation`, `--target`,
`--tool semantist`, `--configuration no_llm`,
`--seed 0`, and `--smoke-seconds 10`; set `--mutation-mode` to the mode being
measured and use `--semantic-ratio 0.30`. The CLI rejects more than two targets
or six jobs in a smoke invocation, so run one target at a time or split the
six-target set into batches.

Record from each `runs/.../result.json` and `metrics/engine-summary.json`:
first finding, native confirmation, executions, scans, throughput, state
progress, parser rejects, timeouts, run failures, raw mutation executions,
and raw mutation entries retained in the shared corpus. Do not use baseline
crash inputs as SemantiST seeds.
