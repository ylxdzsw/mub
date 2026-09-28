"""Mu owns durable conversation history; mub only buffers live output."""

import json
from pathlib import Path
import re
import subprocess
import unicodedata


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


def literal(value):
    """Keep terminal controls out of user content, without interpreting Markdown."""
    return "".join(
        "    " if char == "\t" else "�" if char != "\n" and unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in str(value or "")
    )


def live_prompt(text, cwd, status):
    tokens, window = status.get("context_tokens"), status.get("context_window")
    context = None
    if tokens is not None and window:
        estimated = "~" if status.get("context_usage_source") == "estimated" else ""
        context = f"{estimated}{tokens * 100 / window:.0f}%"
    return dict(kind="prompt", text=text, cwd=str(cwd),
                model=status.get("model", {}).get("canonical") or "mu", context=context)


def prompt_bytes(block, *, pending=False):
    header = f"\x1b[94m{literal(block['model'])}\x1b[0m"
    if block.get("context"):
        header += f" \x1b[35m{literal(block['context'])}\x1b[0m"
    header += f" \x1b[36m{literal(block['cwd'])}\x1b[0m"
    text = header + "\nmu> " + literal(block["text"])
    if not pending:
        text += "\n" * max(0, 2 - (len(text) - len(text.rstrip("\n"))))
    return text.replace("\n", "\r\n").encode()


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")


def plain_output(data):
    """Readable append-only logs/evidence, never a truncated terminal viewport.

    This removes display escapes; it does not try to emulate screen updates.
    Complete trap commands/stdin remain independent of screen scrollback limits.
    """
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    text = _ANSI.sub("", text).replace("\r", "\n").replace("\x07", "")
    return "".join(char if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"} else "�"
                   for char in text)
