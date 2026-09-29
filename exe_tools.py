"""Thin wrappers around pip + PyInstaller."""
import platform
import subprocess
import sys
import time
from pathlib import Path

TAIL = 6000
IS_WINDOWS = platform.system() == "Windows"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired as e:
        return 124, f"TIMEOUT after {timeout}s\n{(e.stdout or '')}{(e.stderr or '')}"
    except FileNotFoundError as e:
        return 127, str(e)


def pyinstaller_available() -> bool:
    rc, _ = _run([sys.executable, "-m", "PyInstaller", "--version"])
    return rc == 0


def ensure_pyinstaller() -> tuple[bool, str]:
    if pyinstaller_available():
        return True, ""
    rc, out = _run([sys.executable, "-m", "pip", "install", "pyinstaller"])
    return rc == 0, out[-TAIL:]


def pip_install_requirements(project: Path, req_file: str = "requirements.txt"):
    """Install the project's own deps into THIS Python env, since PyInstaller only bundles
    what's importable in the interpreter that runs it."""
    req = project / req_file
    if not req.is_file():
        return True, "(no requirements.txt found, skipping)"
    rc, out = _run([sys.executable, "-m", "pip", "install", "-r", str(req)], cwd=project, timeout=1800)
    return rc == 0, out[-TAIL:]


def build(project: Path, spec_name: str):
    """Build from a .spec file the agent generated. --noconfirm overwrites dist/ silently."""
    rc, out = _run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", spec_name],
        cwd=project, timeout=1800,
    )
    return rc == 0, out[-TAIL:]


def output_path(project: Path, name: str) -> Path:
    exe = project / "dist" / f"{name}.exe"
    if exe.exists():
        return exe
    return project / "dist" / name  # non-Windows / onedir fallback


def smoke_test(exe: Path, seconds: int = 5):
    """Launch the binary. Still running after N seconds, or a clean exit, counts as a pass.
    Skipped entirely for GUI apps launched with --windowed, since those legitimately just sit there
    with no stdout — caller decides whether to call this."""
    if not exe.exists():
        return False, f"Expected output not found: {exe}"
    try:
        proc = subprocess.Popen([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        return False, f"Could not launch: {e}"
    time.sleep(seconds)
    rc = proc.poll()
    if rc is None:
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        return True, f"still running after {seconds}s (terminated for the test)\n{out[-TAIL:]}"
    out = proc.communicate()[0] or ""
    return rc == 0, f"exited with code {rc}\n{out[-TAIL:]}"


def run_instructions(exe: Path) -> str:
    if IS_WINDOWS:
        return f'Double-click "{exe}"  or run:\n    "{exe}"'
    return f'Run:\n    "{exe}"\n(Built on {platform.system()} — this is a native binary, not a Windows .exe. ' \
           f'Run this same command on a Windows machine to get a real .exe.)'
