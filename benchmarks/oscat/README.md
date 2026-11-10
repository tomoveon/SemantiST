# OSCAT benchmark suite

The default `semantist-batch --suite benchmarks` index contains 100 OSCAT POUs:
80 Basic, 10 Network, and 10 Building.

The Basic selection contains 40 `FUNCTION` and 40 `FUNCTION_BLOCK` POUs and
covers pointer/CRC operations, clocks, retained state, shared records, and
array-backed events. Network covers IP parsing, bitwise transforms, and
stateful date calculations; Building covers time, control, shared counters,
rolling data, and floating-point calculations. POU kind is not used as a
substitute for cross-frame effect analysis during execution.

Network and Building targets keep extracted source in `target.st` and, when
needed, a namespace-normalized version in `normalized.st`. The extractor checks
the source and normalized forms for targets configured for that check.

`manifest.json` names each source export, the extracted target, and the Basic
compatibility manifest where applicable. `prepare_targets.py` uses the POU
extractor and preserves each selected POU body. When the original exports
are available under `oscat/oscat/`, check extraction with:

```bash
python3 -m benchmarks.oscat.prepare_targets --check
```

The following 600-second batch is a historical single-tool example, not the
formal 100-target main experiment. Run the complete SemantiST batch with a ten-minute total hybrid-search
budget per target (plus a separate build allowance):

```bash
./benchmarks/oscat/run_full.sh artifacts/batch/oscat-100-600s
```

The second argument overrides the per-target hybrid budget in seconds, for
example `./benchmarks/oscat/run_full.sh artifacts/batch/oscat-trial 60`.
`SEMANTIST_BENCH_TIME_PER_TARGET` is an equivalent environment override.
The third argument or `SEMANTIST_BENCH_BUILD_OVERHEAD` changes the separate
build allowance (600 seconds by default, to permit cold Cargo builds).
Use a new output directory when changing the roster or time budget; `--resume`
is intended for the same manifest and settings.

The container commands work with Podman or Docker. `--init` reaps sanitizer
helper processes from crashing testcases. Rebuild the image after changing the
workspace, or bind-mount the current workspace to use an existing image:

```bash
ENGINE=podman # use ENGINE=docker for Docker
"$ENGINE" build -t semantist:oscat .
mkdir -p artifacts
"$ENGINE" run --rm --init \
  --mount type=bind,src="$(pwd)",dst=/work/SemantiST \
  -w /work/SemantiST \
  semantist:oscat \
  ./benchmarks/oscat/run_full.sh artifacts/batch/oscat-100-600s
```

The script uses `semantist-batch`, which invokes SemantiST for each target
in manifest order. `--full-hybrid-duration` applies the per-target budget to
startup symbolic queries, native fuzzing, online KLEE queries, and their
feedback. The native engine runs until the shared deadline, with its own exit
headroom. Build time has a separate 600-second allowance. Outputs appear
under the chosen output directory at `<id>/buggy/`; the batch updates
`batch-summary.json` and `batch-summary.md` after every target. Run the same
command again to resume completed entries. Use a new output directory for a
fresh run. Set `SEMANTIST_BENCH_LLM=1` to enable SemantiST's optional LLM
planner; the default campaign includes native fuzzing and the symbolic phase.

The Basic targets compile against the normalized OSCAT Basic project. Network
and Building use the suite's normalized exports and typed compatibility stubs.
The 100-target roster is a selection, not a completed validation result.
For the formal `overall` experiment, see [the experiment runner](../experiments/README.md).
It has 1,500 trials (100 targets, three tools, five seeds each), with 300 seconds
of online wall time per trial; the full deduplicated comparison matrix has
3,500 jobs, and 600 seconds belongs only to the separate budget comparison.
All three tools are built and all SemantiST plans frozen before any search;
then SemantiST, AFL++ and ICSQuartz run as separate commands in one timestamped
`artifacts/experiments/` root on the `/home` disk. They use physical CPU pools
0-9, 10-19 and 20-29 respectively, two cores and 8 GiB per trial. The online
clock includes initialization and SemantiST online KLEE. Failed trials require
inspection and `--retry-failed`; changed code requires a new image and root.
Network and Building currently use approximate compatibility models; some
targets are native-only for symbolic search. Report source/model
fidelity, native support, symbolic support, and probe coverage per target.
Confirmed findings are reproducible under the normalized source, stubs, and
harness, not automatically under the original vendor runtime.

The source exports under `oscat/oscat/` and extracted benchmark targets are
third-party OSCAT material. The repository's MIT license applies to SemantiST
code, not these third-party source files. See the
[OSCAT archive's license notes](https://github.com/eclipse-oscat/oscat-libs-archive#licenses)
and [OSCAT](https://www.oscat.de/) for the original terms and attribution.
