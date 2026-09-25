"""Mu owns durable conversation history; mub only buffers live output."""

import json
from pathlib import Path
import subprocess


def journal_path(root, session):
    return Path(root) / ".mu" / "sessions" / f"{session}.jsonl"


def delivery(root, session, offset, clean):
    """Read only the appended part of a Mu journal, never infer delivery from exit alone."""
    with journal_path(root, session).open("rb") as stream:
        stream.seek(offset)
        events = [json.loads(line) for line in stream if line.strip()]
    queued = {e["prompt_id"] for e in events if e["type"] == "prompt_queued"}
    materialized = {e["prompt_id"] for e in events if e["type"] == "prompt_materialized"}
    if not queued:
        return "undelivered"
    return "complete" if clean and queued <= materialized else "interrupted"


def replay(root, session, mu="mu", *, full=False):
    result = subprocess.run([mu, "transcript", "-s", session, "-o", "full" if full else "concise"],
                            cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Cannot replay Mu session")
    return result.stdout
