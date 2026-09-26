"""Mu owns durable conversation history; mub only buffers live output."""

import errno
import json
import os
from pathlib import Path
import pty
import re
import subprocess
import tempfile
import termios
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


def history_blocks(root, session, text):
    """Locate complete, journal-backed prompts in Mu's plain transcript.

    Mu still owns transcript projection (including compaction and tool output).
    Match the entire recorded prompt, not arbitrary lines beginning with mu>.
    Header metadata, especially historical context percentages, comes from Mu.
    """
    queued, prompts = {}, []
    with journal_path(root, session).open() as stream:
        for line in stream:
            event = json.loads(line)
            if event["type"] == "prompt_queued":
                queued[event["prompt_id"]] = event
            elif event["type"] == "prompt_materialized":
                prompts.append(queued[event["prompt_id"]])
            elif event["type"] == "compaction_started":
                prompts.append(event)
    patterns = {}
    for event in prompts:
        content = event["prompt"]
        prompt = content["text"] if content["kind"] == "text" else "\n".join(
            part["text"] for part in content["parts"] if part["kind"] == "text")
        cwd = event["cwd"]
        key = (cwd, prompt)
        patterns[key] = patterns.get(key, 0) + 1
    spans = []
    for (cwd, prompt), count in patterns.items():
        pattern = re.compile(r"^(?P<model>[^\n]+?)(?: (?P<context>~?\d+%))? "
                             + re.escape(cwd) + r"\nmu> " + re.escape(prompt)
                             + ("" if prompt.endswith("\n") else r"(?:\n|$)"), re.MULTILINE)
        matches = list(pattern.finditer(text))
        # A response can quote a real prompt. Leave ambiguous text untouched.
        if len(matches) != count:
            continue
        for match in matches:
            spans.append((match.start(), match.end(), dict(kind="prompt", text=prompt, cwd=cwd,
                          model=match["model"], context=match["context"])))
    blocks, end = [], 0
    for start, stop, block in sorted(spans, key=lambda span: span[0]):
        if start < end:
            continue
        if text[end:start].strip():
            blocks.append(dict(kind="markdown", text=text[end:start]))
        blocks.append(block)
        end = stop
    if text[end:].strip():
        blocks.append(dict(kind="markdown", text=text[end:]))
    return blocks


def live_prompt(text, cwd, status):
    tokens, window = status.get("context_tokens"), status.get("context_window")
    context = None
    if tokens is not None and window:
        estimated = "~" if status.get("context_usage_source") == "estimated" else ""
        context = f"{estimated}{tokens * 100 / window:.0f}%"
    return dict(kind="prompt", text=text, cwd=str(cwd),
                model=status.get("model", {}).get("canonical") or "mu", context=context)


def render_blocks(root, blocks, width, mu="mu", cache=None):
    # Retain only this view's blocks. New live text must not evict old history
    # or leave a cache full of obsolete streaming snapshots.
    cache = {} if cache is None else cache
    rendered, current = [], {}
    for block in blocks:
        if block["kind"] == "prompt":
            header = f"\x1b[94m{literal(block['model'])}\x1b[0m"
            if block.get("context"):
                header += f" \x1b[35m{literal(block['context'])}\x1b[0m"
            header += f" \x1b[36m{literal(block['cwd'])}\x1b[0m"
            rendered.append(header + "\nmu> " + literal(block["text"]))
        elif block["text"].strip():
            key = (root, literal(block["text"]), width, mu)
            current[key] = cache[key] if key in cache else markdown(*key)
            rendered.append(current[key])
    cache.clear()
    cache.update(current)
    return "".join(part + "\n" * max(0, 2 - (len(part) - len(part.rstrip("\n"))))
                   for part in rendered)


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
