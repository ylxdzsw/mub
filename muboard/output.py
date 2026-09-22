"""Replay agent history from Mu; old boards retain their archived run logs."""

from pathlib import Path
import subprocess


def replay(root, run, mu="mu"):
    if run.get("log_path") and Path(run["log_path"]).exists():
        return Path(run["log_path"]).read_text(errors="replace")
    try:
        result = subprocess.run([mu, "transcript", "-s", run["session"], "-o", "full"],
                                cwd=root, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Mu transcript replay timed out") from None
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Cannot replay Mu session")
    return result.stdout
