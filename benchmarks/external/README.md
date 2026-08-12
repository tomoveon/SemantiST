# External ST benchmark suite

This directory contains SemantiST-runnable OSCAT and ICSQuartz benchmark targets.

The authoritative index is `manifest.json`. Each entry records upstream repository, commit, original path, SHA-256, target POU, and transformations.

Counts:
- icsquartz_icsfuzz: 17
- icsquartz_scan_cycle: 12
- oscat_basic: 61

Compatibility notes:
- ICSFuzz and scan-cycle imports use `compatibility/codesys-memory/compatibility.json` for CODESYS memory APIs.
- OSCAT Basic targets use `benchmarks/oscat_basic/source/compatibility.json`.

Reproduce:
- Regenerate this suite with `python3 benchmarks/external/import_benchmarks.py`.
- Build-check all targets with `python3 benchmarks/external/check_compile_suite.py --keep-going --timeout 420`.
- Build-check one suite with `python3 benchmarks/external/check_compile_suite.py --suite icsquartz_icsfuzz --keep-going`.
