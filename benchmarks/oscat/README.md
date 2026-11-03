# OSCAT benchmark suite

The default `semantist-batch --suite benchmarks` index contains 100 OSCAT POUs:
80 Basic, 10 Network, and 10 Building. It includes all 26 targets from the
previous OSCAT index, `FILTER_MAV_DW` from `seven_targets.json` (27 required
targets in total), and all 61 Basic targets from the historical mixed index.
The requested `COMP` maps to the actual Basic POU `BUFFER_COMP`.

The eight additional Basic targets balance the roster at 40 `FUNCTION` and
40 `FUNCTION_BLOCK` POUs while adding pointer/CRC, clock, retained state,
shared records, and array-backed events. The five new Network targets exercise
IP parsing, bitwise transforms, and stateful date calculations without adding
network-service stubs. The
nine new Building targets cover time, control, shared counters, rolling data,
and floating-point calculations. POU kind is not used as a substitute for
cross-frame effect analysis during execution.

New Network and Building entries keep the extracted source in `target.st` and
use `normalized.st` for the same namespace flattening already used by the
earlier cross-library entries. The extractor verifies both versions for these
new targets; older hand-normalized entries retain their existing files.

`manifest.json` names each source export, the extracted target, and the Basic
compatibility manifest where applicable. `prepare_targets.py` uses the existing
POU extractor and preserves each selected POU body. When the original exports
are available under `oscat/oscat/`, check extraction with:

```bash
python3 -m benchmarks.oscat.prepare_targets --check
```

Run the complete SemantiST batch with a ten-minute total hybrid-search
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

For the pinned toolchain, use Podman. `--init` is required so sanitizer helper
processes from crashing testcases are reaped between targets. Rebuild the image
after changing the workspace, or bind-mount the current workspace as shown
below while using the toolchain in an existing image:

```bash
podman build -t localhost/semantist:oscat .
mkdir -p artifacts
podman run --rm --init \
  --mount type=bind,src="$(pwd)",dst=/work/SemantiST \
  -w /work/SemantiST \
  localhost/semantist:oscat \
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
Network and Building currently use approximate compatibility models; some
historical targets are native-only for symbolic search. Report source/model
fidelity, native support, symbolic support, and probe coverage per target.
Confirmed findings are reproducible under the normalized source, stubs, and
harness, not automatically under the original vendor runtime.

ICSFuzz and scan-cycle targets are absent from this benchmark. The three
scan-cycle cases K2-K4 remain in the separate defect regression suite while
their LREAL, multi-frame prefix, and pointer-backing behavior is compared with
OSCAT coverage; they are not counted in the 100 targets.

The older mixed OSCAT/ICSQuartz index is retired. ICSQuartz targets retained
under `benchmarks/external/` are defect-regression fixtures, not part of the
default OSCAT run.

The source exports under `oscat/oscat/` and extracted benchmark targets are
third-party OSCAT material. The repository's MIT license applies to SemantiST
code, not these legacy library files. See the
[OSCAT archive's license notes](https://github.com/eclipse-oscat/oscat-libs-archive#licenses)
and [OSCAT](https://www.oscat.de/) for the original terms and attribution.

Historical local runs under `artifacts/` are excluded from the repository.
They predate this 100-target roster and do not validate it. A new run must
publish its own batch summary, support status, model fidelity, and separate
`UNSUPPORTED`, `TIMEOUT`, and `REPLAY_MISMATCH` outcomes before making
discovery or coverage claims.
