"""Subprocess and CPU helpers for comparison experiments."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable


def parse_cpus(text: str) -> list[int]:
    cpus: list[int] = []
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid CPU range: {part}")
            cpus.extend(range(start, end + 1))
        else:
            cpus.append(int(part))
    unique = list(dict.fromkeys(cpus))
    if not unique:
        raise ValueError("at least one CPU is required")
    return unique


def unavailable_cpus(cpus: list[int]) -> list[int]:
    """Return requested CPUs excluded by the runner's current affinity mask."""
    if not hasattr(os, "sched_getaffinity"):
        return []
    allowed = os.sched_getaffinity(0)
    return [cpu for cpu in cpus if cpu not in allowed]


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def cpu_bound_command(command: list[str], cpu: int) -> list[str]:
    """Return a command constrained to one logical CPU via util-linux taskset."""
    if cpu < 0:
        raise ValueError(f"CPU id must be non-negative, got {cpu}")
    taskset = shutil.which("taskset")
    if taskset is None:
        raise RuntimeError("taskset was not found; per-row CPU affinity is required")
    return [taskset, "--cpu-list", str(cpu), *command]


CONTAINER_RUNTIMES = ("docker", "podman")


def container_runtime_accessible(runtime: str | None) -> tuple[bool, str]:
    """Check that a Docker-compatible container runtime can execute commands."""
    if runtime is None:
        return False, "neither Docker nor Podman is accessible"
    if runtime not in CONTAINER_RUNTIMES:
        return False, f"unsupported container runtime: {runtime}"
    if shutil.which(runtime) is None:
        return False, f"{runtime} CLI was not found"
    try:
        result = subprocess.run(
            [runtime, "info"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, f"{runtime} info timed out"
    combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    if result.returncode != 0:
        return False, combined or f"{runtime} info failed"
    return True, f"{runtime} info succeeded"


def resolve_container_runtime(requested: str) -> tuple[str | None, str]:
    """Resolve auto/docker/podman to one accessible command for the full run."""
    if requested not in {"auto", *CONTAINER_RUNTIMES}:
        raise ValueError(f"unsupported container runtime selection: {requested}")
    candidates = CONTAINER_RUNTIMES if requested == "auto" else (requested,)
    failures: list[str] = []
    for runtime in candidates:
        accessible, status = container_runtime_accessible(runtime)
        if accessible:
            return runtime, status
        failures.append(f"{runtime}: {status}")
    return None, "; ".join(failures)


def container_runtime_version(runtime: str | None) -> str | None:
    if runtime is None or shutil.which(runtime) is None:
        return None
    result = subprocess.run(
        [runtime, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or result.stderr.strip() or None


def container_image_id(runtime: str | None, image: str) -> str | None:
    if runtime is None or shutil.which(runtime) is None:
        return None
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", "--format", "{{.Id}}", image],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def bind_mount(runtime: str, source: Path, destination: str, *, readonly: bool = False) -> str:
    """Build a bind mount accepted by Docker and rootless Podman on SELinux hosts."""
    options = ["ro"] if readonly else []
    if runtime == "podman":
        options.append("Z")
    suffix = f":{','.join(options)}" if options else ""
    return f"{source}:{destination}{suffix}"


def container_run_command(
    runtime: str,
    image: str,
    *,
    mounts: list[tuple[Path, str, bool]] | None = None,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
    platform: str | None = None,
    name: str | None = None,
    detach: bool = False,
    remove: bool = True,
    interactive_stdin: bool = False,
    cap_add: list[str] | None = None,
    command: list[str] | None = None,
) -> list[str]:
    """Build a Docker/Podman run command using only portable flags."""
    if runtime not in CONTAINER_RUNTIMES:
        raise ValueError(f"unsupported container runtime: {runtime}")
    result = [runtime, "run"]
    if remove:
        result.append("--rm")
    if detach:
        result.append("-d")
    if interactive_stdin:
        result.append("-i")
    if name:
        result.extend(["--name", name])
    if platform:
        result.extend(["--platform", platform])
    for capability in cap_add or []:
        result.extend(["--cap-add", capability])
    for source, destination, readonly in mounts or []:
        result.extend(["-v", bind_mount(runtime, source, destination, readonly=readonly)])
    for key, value in sorted((env or {}).items()):
        result.extend(["-e", f"{key}={value}"])
    if workdir:
        result.extend(["-w", workdir])
    result.append(image)
    result.extend(command or [])
    return result


def container_cpu_bound_command(command: list[str], runtime: str, cpu: int) -> list[str]:
    """Constrain a container run without requiring rootless Podman cpuset delegation."""
    if command[:2] != [runtime, "run"]:
        raise ValueError("container CPU binding requires a '<runtime> run' command")
    if runtime == "podman":
        # Local Podman is daemonless, so the OCI runtime and container init
        # inherit the CLI process affinity even when cpuset cgroups are not delegated.
        return cpu_bound_command(command, cpu)
    if runtime == "docker":
        return [runtime, "run", "--cpuset-cpus", str(cpu), *command[2:]]
    raise ValueError(f"unsupported container runtime: {runtime}")


def docker_accessible() -> tuple[bool, str]:
    """Backward-compatible Docker-only probe."""
    return container_runtime_accessible("docker")


def run_capture(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int | None = None,
) -> dict[str, object]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic_ns()
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds,
                check=False,
            )
        end = time.monotonic_ns()
        return {
            "command": command,
            "returncode": completed.returncode,
            "start_monotonic_ns": start,
            "end_monotonic_ns": end,
            "elapsed_seconds": (end - start) / 1_000_000_000,
            "timed_out": False,
        }
    except FileNotFoundError as error:
        end = time.monotonic_ns()
        stderr_path.write_text(str(error) + "\n", encoding="utf-8")
        return {
            "command": command,
            "returncode": 127,
            "start_monotonic_ns": start,
            "end_monotonic_ns": end,
            "elapsed_seconds": (end - start) / 1_000_000_000,
            "timed_out": False,
            "error": str(error),
        }
    except subprocess.TimeoutExpired as error:
        end = time.monotonic_ns()
        with stderr_path.open("ab") as stderr:
            stderr.write(f"\n[runner] command timed out after {timeout_seconds}s: {error}\n".encode())
        return {
            "command": command,
            "returncode": None,
            "start_monotonic_ns": start,
            "end_monotonic_ns": end,
            "elapsed_seconds": (end - start) / 1_000_000_000,
            "timed_out": True,
            "error": str(error),
        }


def run_with_budget(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    stdout_path: Path,
    stderr_path: Path,
    budget_seconds: int,
    poll_interval_ms: int,
    scan_findings: Callable[[], list[dict[str, object]]],
) -> dict[str, object]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic_ns()
    events: list[dict[str, object]] = []
    seen: set[str] = set()
    try:
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        stderr_path.write_text(str(error) + "\n", encoding="utf-8")
        return {
            "started": False,
            "t0_monotonic_ns": t0,
            "end_monotonic_ns": time.monotonic_ns(),
            "elapsed_seconds": 0.0,
            "returncode": 127,
            "timed_out": False,
            "events": [],
            "error": str(error),
        }

    deadline = t0 + budget_seconds * 1_000_000_000
    poll_seconds = poll_interval_ms / 1000
    timed_out = False

    def observe_findings() -> None:
        for event in scan_findings():
            candidate_id = str(event.get("candidate_id"))
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            observed = dict(event)
            observed["observed_monotonic_ns"] = time.monotonic_ns()
            observed["observed_elapsed_seconds"] = (
                observed["observed_monotonic_ns"] - t0
            ) / 1_000_000_000
            events.append(observed)

    try:
        while True:
            now = time.monotonic_ns()
            observe_findings()
            if process.poll() is not None:
                break
            if now >= deadline:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                break
            time.sleep(min(poll_seconds, max((deadline - now) / 1_000_000_000, 0)))
        # Capture artifacts flushed during normal exit or signal handling.
        observe_findings()
    finally:
        stdout.close()
        stderr.close()

    end = time.monotonic_ns()
    return {
        "started": True,
        "t0_monotonic_ns": t0,
        "end_monotonic_ns": end,
        "elapsed_seconds": (end - t0) / 1_000_000_000,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "events": events,
    }


def log_tail(path: Path, lines: int = 80) -> str:
    if not path.is_file():
        return ""
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])
