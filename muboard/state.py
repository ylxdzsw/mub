"""Single-writer board snapshot. Mu owns agent journals."""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path


EXECUTION_FIELDS = {"gate", "turns", "revision", "mode", "created", "updated", "question",
                    "failed_runs", "retry_after", "blocked_after", "recovery_decision"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(data):
    """Import v1 without modifying its snapshot or archived execution files."""
    by_id = {t["id"]: t for t in data["tasks"]}
    ordered, seen = [], set()

    def visit(task):
        if task["id"] in seen:
            return
        seen.add(task["id"])
        for key in task.get("depends_on", []):
            visit(by_id[key])
        ordered.append(task)

    for task in sorted(data["tasks"], key=lambda t: (-t.get("priority", 0), t["id"])):
        visit(task)
    for task in ordered:
        note = task.pop("request", "")
        brief = task.pop("brief", "")
        if brief and brief != note:
            note += "\n\nLatest instructions:\n" + brief
        dependencies = task.pop("depends_on", [])
        if dependencies:
            note += "\n\nRequires successful completion of " + ", ".join(f"T{k}" for k in dependencies) + ". Reassess before dispatch."
        task.pop("priority", None)
        result = task.pop("result", "")
        if result:
            note += "\n\nLast outcome:\n" + result
        task["execution"] = {key: task.pop(key) for key in EXECUTION_FIELDS if key in task}
        if task["execution"].get("question"):
            note += "\n\nBlocked: " + task["execution"]["question"]
        if task["state"] == "needs_input":
            task["state"] = "blocked"
        task["note"] = note
    data["tasks"] = ordered
    decisions = data.pop("decisions", [])
    if decisions:
        data["messages"].append(dict(id=len(data["messages"]) + 1, role="system", task_id=None,
                                     content="Imported project decisions (retain relevant context in task notes):\n"
                                     + "\n".join(d["content"] for d in decisions), created=now()))
    data.update(version=2, dispatch=None)
    return data


def read_state(root):
    path = Path(root) / ".mu" / "mub.json"
    if not path.exists():
        path = Path(root) / ".mub" / "state.json"
    if not path.exists():
        return fresh_state()
    data = json.loads(path.read_text())
    if data.get("version") == 1:
        data = migrate(data)
    if data.get("version") != 2:
        raise ValueError("Unsupported mub snapshot version")
    # Older snapshots did not link prompt events to their conversation entries.
    linked = {e["message_id"] for e in data["events"] if "message_id" in e}
    for event in reversed(data["events"]):
        if event["handled"] or "message_id" in event or event["kind"] not in ("message", "submitted"):
            continue
        message = next((m for m in reversed(data["messages"])
                        if m["role"] == "user" and m["id"] not in linked
                        and m["task_id"] == event["task_id"] and m["created"] <= event["created"]
                        and (event["kind"] == "submitted" or m["content"] == event["text"])), None)
        if message:
            event["message_id"] = message["id"]
            linked.add(message["id"])
    return data


def fresh_state():
    return dict(version=2, tasks=[], messages=[], events=[], runs=[], dispatch=None,
                models=dict(pm=None, worker=None),
                paused=False, hold=None, workspace_block=None, error=None)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / ".mu"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.lock = (self.directory / "mub.lock").open("a+")
        self.legacy_lock = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            legacy = self.root / ".mub" / "owner.lock"
            if legacy.exists():
                self.legacy_lock = legacy.open("a+")
                fcntl.flock(self.legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.close()
            raise RuntimeError("This project already has a running mub") from None
        try:
            ignore = self.directory / ".gitignore"
            previous = ignore.read_text() if ignore.exists() else ""
            patterns = ["/mub.json", "/mub.json.tmp", "/mub.lock", "/mub.sock"]
            missing = [p for p in patterns if p not in previous.splitlines()]
            if missing:
                # A newly created ignore file is itself local; existing tracked files stay visible.
                if not ignore.exists():
                    missing.insert(0, "/.gitignore")
                ignore.write_text(previous + ("\n" if previous and not previous.endswith("\n") else "")
                                  + "\n".join(missing) + "\n")
            self.data = read_state(root)
            self.save()
        except BaseException:
            self.close()
            raise

    def save(self):
        temporary = self.directory / "mub.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.directory / "mub.json")
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def close(self):
        if self.legacy_lock:
            self.legacy_lock.close()
        self.lock.close()

    def task(self, task_id):
        for task in self.data["tasks"]:
            if task["id"] == task_id:
                return task
        raise ValueError(f"Unknown task T{task_id}")

    def add_task(self, text, title=None):
        text = text.strip()
        if not text:
            raise ValueError("Task cannot be empty")
        task = dict(id=max((t["id"] for t in self.data["tasks"]), default=0) + 1,
                    title=title or text.splitlines()[0][:80], note=text, state="inbox", session=None,
                    execution=dict(gate=None, turns=0, revision=1, mode="prompt", question="",
                                   created=now(), updated=now()))
        self.data["tasks"].append(task)
        return task

    def touch(self, task):
        task["execution"]["revision"] += 1
        task["execution"]["updated"] = now()

    def message(self, role, content, task_id=None):
        row = dict(id=len(self.data["messages"]) + 1, role=role, content=content,
                   task_id=task_id, created=now())
        self.data["messages"].append(row)
        return row

    def event(self, kind, task_id=None, text="", *, message_id=None):
        self.data["dispatch"] = None
        row = dict(id=len(self.data["events"]) + 1, kind=kind, task_id=task_id,
                   text=text, handled=False, created=now())
        if message_id is not None:
            row["message_id"] = message_id
        self.data["events"].append(row)
        return row

    def pending(self):
        return sorted((e for e in self.data["events"] if not e["handled"]),
                      key=lambda e: (not (e["kind"].startswith("worker_") or
                                          (e["kind"] == "interrupted" and e["task_id"] is not None)), e["id"]))


def update_task(task, **values):
    for key, value in values.items():
        (task["execution"] if key in EXECUTION_FIELDS else task)[key] = value
