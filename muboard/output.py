"""Mu owns durable conversation history; mub only buffers live output."""

import errno
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import termios


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


def markdown(root, text, width, mu="mu"):
    """Capture Mu's own terminal Markdown renderer, without its preview header."""
    master, slave = pty.openpty()
    try:
        # Mu reserves one terminal column as a right margin.
        termios.tcsetwinsize(slave, (24, width + 1))
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as errors:
            # cat trims stdin. Keep the last Markdown line complete so trailing
            # fences and table rows are flushed as blocks, not partial lines.
            source.write((text + "\n\n\x1b[0m").encode())
            source.seek(0)
            with subprocess.Popen([mu, "cat"], cwd=root, stdin=source, stdout=slave, stderr=errors) as process:
                os.close(slave)
                slave = None
                chunks = []
                while True:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                if process.wait():
                    errors.seek(0)
                    raise RuntimeError(errors.read().decode("utf-8", "replace").strip() or "Cannot render Mu output")
        rendered = b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")
        return rendered.split("\n\n", 1)[1].rstrip("\n")
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)
