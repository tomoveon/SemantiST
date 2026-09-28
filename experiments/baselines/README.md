# Baseline environment images

This directory builds the AFL++, ICSQuartz, and ICSFuzz
environments used by the SemantiST evaluation. It does not vendor the
third-party repositories.
Each container build downloads a pinned upstream image or source archive and
verifies the recorded digest before installing it.

The current images are environment images only. They contain the compilers,
fuzzers, and runtimes needed for later target-specific builds, but they do not
run an experiment or embed a benchmark target.

## Build

Docker or Podman must be accessible. From the SemantiST repository root, build
and smoke-test all three Linux `amd64` images (automatic Docker-to-Podman
fallback is the default):

```bash
python3 experiments/baselines/build_images.py
```

Require one runtime when reproducibility scripts should not fall back:

```bash
python3 experiments/baselines/build_images.py --container-runtime docker
python3 experiments/baselines/build_images.py --container-runtime podman
```

`CONTAINER_RUNTIME=podman` provides the same default without repeating the CLI
option.

On Windows PowerShell, use `python` if that is the Python 3 launcher:

```powershell
python experiments/baselines/build_images.py
```

Build only one or two environments:

```bash
python3 experiments/baselines/build_images.py aflplusplus
python3 experiments/baselines/build_images.py icsquartz icsfuzz
```

Re-run only the image smoke checks:

```bash
python3 experiments/baselines/build_images.py --skip-build
```

Successful completion creates:

```text
semantist-aflplusplus-env:4.21c
semantist-icsquartz-env:8021bd4
semantist-icsfuzz-env:8021bd4
```

All source revisions, base-image digests, archive checksums, and image tags are
recorded in `baselines.lock.json`.

## Scope and limitations

The AFL++ environment wraps the official AFL++ v4.21c image used by the
ICSQuartz-era artifact. The ICSQuartz environment builds the authors' pinned
source with its pinned LibAFL revision and LLVM 18. The ICSFuzz environment
reproduces the ICSQuartz containerized ICSFuzz baseline and installs CODESYS
Control for Linux SL 3.5.16.10.

The ICSFuzz environment is not yet a runnable fuzzing campaign: a compiled
CODESYS application and its target-specific `harness.env` must be added later.
Running it also requires an isolated Linux host, CODESYS address calibration,
`SYS_PTRACE`, and the ASLR policy documented by ICSQuartz. Building the
environment does not alter the host ASLR setting.

See `THIRD_PARTY.md` before redistributing any baseline image, especially the
ICSQuartz/ICSFuzz image or the proprietary CODESYS runtime.
