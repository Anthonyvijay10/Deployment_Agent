"""Thin wrappers around the docker CLI."""
import re
import shutil
import subprocess
import time
from pathlib import Path

TAIL = 6000  # chars of log we feed back to the LLM


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired as e:
        return 124, f"TIMEOUT after {timeout}s\n{(e.stdout or '')}{(e.stderr or '')}"


def build(project: Path, image: str):
    rc, out = _run(["docker", "build", "-t", image, "."], cwd=project)
    return rc == 0, out[-TAIL:]


def smoke_test(image: str, env_file: str | None, seconds: int = 10):
    """Start the container. Still running after N seconds (server) or exit 0 (script) = pass."""
    name = f"{image.replace(':', '_')}_smoke"
    _run(["docker", "rm", "-f", name])
    cmd = ["docker", "run", "-d", "--name", name]
    if env_file:
        cmd += ["--env-file", env_file]
    rc, out = _run(cmd + [image])
    if rc != 0:
        return False, out[-TAIL:]
    time.sleep(seconds)
    _, state = _run(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name])
    _, logs = _run(["docker", "logs", name])
    _run(["docker", "rm", "-f", name])
    running, _, code = state.strip().partition(" ")
    ok = running == "true" or code == "0"
    return ok, f"state={state.strip()}\n{logs[-TAIL:]}"


def run_command(dockerfile_text: str, image: str, env_file: str | None) -> str:
    ports = re.findall(r"^\s*EXPOSE\s+(\d+)", dockerfile_text, re.M | re.I)
    parts = ["docker run --rm"]
    parts.append("".join(f"-p {p}:{p} " for p in ports).strip() or "-it")
    if env_file:
        parts.append(f"--env-file {env_file}")
    parts.append(image)
    return " ".join(x for x in parts if x)
