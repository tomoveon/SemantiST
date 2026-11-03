"""The pinned RuSTy toolchain as seen by the intake: location, identity, checks.

Every rule decision is backed by what this compiler does, so its identity
(revision, applied patches, binary hash) is part of every intake record.
Diagnostics are read in RuSTy's ``clang`` error format.
"""

from __future__ import annotations

import hashlib
import os
import signal
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVISION = "be1de6f175ec1bed7928904dfb694208a24043fb"
PATCH_DIR = ROOT / "compiler" / "toolchain" / "rusty-patches"
DEFAULT_RUSTY_DIR = ROOT / "artifacts" / "rusty-semantic"


def rusty_dir() -> Path:
    return Path(os.environ.get("SEMANTIST_RUSTY_SEMANTIC_DIR", DEFAULT_RUSTY_DIR))


def plc_path() -> Path:
    value = os.environ.get("RUSTY_COMPILER")
    return Path(value) if value else rusty_dir() / "target" / "release" / "plc"


def stdlib_dir() -> Path:
    value = os.environ.get("SEMANTIST_RUSTY_STDLIB_GLOB")
    return Path(value).parent if value else rusty_dir() / "libs" / "stdlib" / "iec61131-st"


def runtime_archive() -> Path:
    value = os.environ.get("SEMANTIST_RUSTY_STDLIB_LIB")
    return Path(value) if value else rusty_dir() / "target" / "release" / "libiec61131std.a"


def stdlib_sources() -> list[Path]:
    return sorted(stdlib_dir().glob("*.st"))


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def toolchain_identity() -> dict:
    """Revision, patch set and binary hashes for TargetIdentity records."""
    return {
        "rusty_revision": REVISION,
        "patches": [{"name": p.name, "sha256": _sha256(p)} for p in sorted(PATCH_DIR.glob("*.patch"))],
        "plc_sha256": _sha256(plc_path()),
        "stdlib_sha256": hashlib.sha256(
            b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in stdlib_sources())
        ).hexdigest(),
        "runtime_archive_sha256": _sha256(runtime_archive()),
    }


@dataclass
class Diagnostic:
    severity: str
    code: str
    message: str
    path: str = ""
    line: int = 0
    column: int = 0
    end_line: int = 0
    end_column: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompileResult:
    returncode: int
    diagnostics: list[Diagnostic] = field(default_factory=list)
    output: str = ""
    panic: str | None = None

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.panic is None and not self.errors


_LOCATED = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+):\{(?P<l1>\d+):(?P<c1>\d+)-(?P<l2>\d+):(?P<c2>\d+)\}: "
    r"(?P<severity>error|warning|info|hint)\[(?P<code>E\d+)\]: (?P<message>.*)$"
)
_UNLOCATED = re.compile(r"^(?P<path>.*?):?\s*(?P<severity>error|warning|info|hint)\[(?P<code>E\d+)\]: (?P<message>.*)$")
_PANIC = re.compile(r"^thread '.*' panicked at (?P<where>[^\n]+)$", re.M)
# Code generation errors are printed as "<message> at: <path>:<line>:<col>:{...}:"
# (sometimes followed by " ."), with 0-based lines and columns. A message that
# dumps an AST node spans several lines and ends in the line with the location
# ("Expected Struct-literal, got Assignment {" ... "} at: ...").
_CODEGEN = re.compile(
    r"^(?P<message>.+?) at: (?P<path>/.+?):(?P<line>\d+):(?P<column>\d+):\{(?P<l1>\d+):(?P<c1>\d+)-(?P<l2>\d+):"
    r"(?P<c2>\d+)\}: ?\.?$"
)
_CLOSING = ("}", ")", "]")


def parse_diagnostics(output: str) -> list[Diagnostic]:
    """Parse ``--error-format clang`` output; messages may span several lines."""
    result: list[Diagnostic] = []
    pending: list[str] = []  # lines after the last diagnostic header

    def flush() -> None:
        if result and pending:
            result[-1].message += "\n" + "\n".join(pending)
        pending.clear()

    for line in output.splitlines():
        if line.startswith("Compilation aborted"):
            # RuSTy repeats the critical diagnostics after this line.
            break
        match = _LOCATED.match(line)
        if match:
            flush()
            result.append(Diagnostic(
                match["severity"], match["code"], match["message"], match["path"],
                int(match["l1"]), int(match["c1"]), int(match["l2"]), int(match["c2"]),
            ))
            continue
        match = _CODEGEN.match(line)
        if match:
            message = match["message"]
            if message.startswith(_CLOSING) and pending:
                # The end of a multi-line message: it starts after the last header.
                message = "\n".join([*pending, message])
                pending.clear()
            flush()
            result.append(Diagnostic(
                "error", "CODEGEN", message, match["path"],
                int(match["l1"]) + 1, int(match["c1"]) + 1, int(match["l2"]) + 1, int(match["c2"]) + 1,
            ))
            continue
        match = _UNLOCATED.match(line)
        if match and not line.startswith((" ", "\t")):
            flush()
            result.append(Diagnostic(match["severity"], match["code"], match["message"], match["path"]))
            continue
        if not _PANIC.match(line):
            pending.append(line)
    flush()
    return result


def run_plc(arguments: list[str], *, cwd: Path | None = None, timeout: int = 600,
            env: dict[str, str] | None = None) -> CompileResult:
    command = [str(plc_path()), *arguments]
    try:
        process = subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=env, errors="replace")
    except OSError as error:
        # Missing compiler/permission failures are intake evidence, not an
        # uncaught traceback.  The normalization and library layers already
        # turn a non-zero result without diagnostics into a structured failure.
        return CompileResult(returncode=127, output=f"compiler could not start: {error}")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        process = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        output = (error.stdout or "") + (error.stderr or "") if isinstance(error.stdout, str) else ""
        return CompileResult(returncode=-1, output=output, panic=f"timeout after {timeout}s")
    output = process.stdout + process.stderr
    panic = _PANIC.search(output)
    return CompileResult(process.returncode, parse_diagnostics(output), output,
                         panic.group("where") if panic else None)


def check(files: list[Path], *, include_stdlib: bool = True, extra: list[str] = (), timeout: int = 600,
          ir: Path | None = None, threads: int = 1) -> CompileResult:
    """Type check ``files`` (or generate IR into ``ir``) with the pinned compiler.

    Diagnostics located in the RuSTy standard library are dropped: they are
    the same for every input (implicit downcast warnings).
    """
    sources = [*(str(f) for f in files), *(str(f) for f in (stdlib_sources() if include_stdlib else []))]
    if ir is None:
        arguments = ["--check", *sources]
    else:
        arguments = ["--ir", "--single-module", "-o", str(ir), *sources]
    result = run_plc([*arguments, "--error-format", "clang", "-j", str(threads), *extra], timeout=timeout)
    stdlib = str(stdlib_dir())
    result.diagnostics = [d for d in result.diagnostics if not d.path.startswith(stdlib)]
    return result


def check_text(text: str, *, name: str = "probe.st", extra: list[str] = (), ir: bool = False,
               include_stdlib: bool = True) -> CompileResult:
    """Check one in-memory source (used by rule probes)."""
    with tempfile.TemporaryDirectory(prefix="semantist-probe-") as directory:
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        output = Path(directory) / "probe.ll" if ir else None
        return check([path], include_stdlib=include_stdlib, extra=list(extra), ir=output)
