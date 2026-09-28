"""Mu owns durable conversation history; mub only buffers live output."""

import json
from collections import deque
from pathlib import Path
import re
import subprocess
import unicodedata


def journal_path(root, session):
    return Path(root) / ".mu" / "sessions" / f"{session}.jsonl"


def journal_events(root, session, offset=0, before=None):
    """Read a fixed journal prefix, never a concurrently appended partial event."""
    with journal_path(root, session).open("rb") as stream:
        size = stream.seek(0, 2)
        end = size if before is None else before
        if not 0 <= offset <= end <= size:
            raise ValueError("Invalid Mu journal byte range")
        stream.seek(offset)
        while stream.tell() < end:
            line = stream.readline(end - stream.tell())
            if not line or not line.endswith(b"\n"):
                raise ValueError("Incomplete Mu journal event in snapshot")
            if line.strip():
                yield json.loads(line)


def excerpt(text, limit):
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n[… omitted; read the journal context before deciding …]\n" + text[-half:]


def conversation(events, *, limit=None):
    """Canonical user turns and final responses; no renderer noise or compaction prose."""
    turns = deque(maxlen=limit)
    queued, requests = {}, {}
    current = None
    count = 0
    for event in events:
        kind = event["type"]
        if kind == "prompt_queued":
            queued[event["prompt_id"]] = event["prompt"]["text"]
        elif kind == "prompt_materialized":
            current = dict(turn_id=event["turn_id"], request=queued.pop(event["prompt_id"]), response="")
            turns.append(current)
            count += 1
        elif kind == "provider_requested":
            # Synthetic compaction turns have no materialized user prompt.
            requests[event["exchange_id"]] = current if current and event["turn_id"] == current["turn_id"] else None
        elif kind == "provider_completed":
            turn = requests.pop(event["exchange_id"], None)
            projection = event.get("projection", {})
            items = projection.get("items", [])
            if turn is not None and projection.get("kind") == "assistant" and not any(i["type"] == "bash_call" for i in items):
                text = "\n".join(i["text"] for i in items if i["type"] == "text")
                if text:
                    turn["response"] = text
    return dict(omitted_turns=count - len(turns), turns=list(turns))


def scheduler_usage(events):
    totals = {}
    requests = compactions = reports = 0
    for event in events:
        requests += event["type"] == "provider_requested"
        compactions += event["type"] == "compaction_applied"
        if event["type"] == "provider_completed" and event.get("usage"):
            reports += 1
            for key, value in event["usage"].items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
    return dict(requests=requests, usage_reports=reports, compactions=compactions, **totals)


def delivery(root, session, offset, clean):
    """Read only the appended part of a Mu journal, never infer delivery from exit alone."""
    events = list(journal_events(root, session, offset))
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
