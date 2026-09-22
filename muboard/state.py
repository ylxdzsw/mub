"""Single-writer board snapshots. Mu owns the conversation journals."""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_state(root):
    path = Path(root) / ".mub" / "state.json"
    if not path.exists():
        return fresh_state()
    data = json.loads(path.read_text())
    if data.get("version") != 1:
        raise ValueError("Unsupported .mub state version")
    return data


def fresh_state():
    return dict(version=1, tasks=[], messages=[], events=[], runs=[], decisions=[],
                models=dict(pm=None, worker=None),
                paused=False, hold=None, workspace_block=None, error=None)


def ordered_tasks(tasks):
    """PM preference order, with every prerequisite ahead of its dependents."""
    by_id = {task["id"]: task for task in tasks}
    ordered, seen = [], set()

    def visit(task):
        if task["id"] in seen:
            return
        seen.add(task["id"])
        for dependency in task["depends_on"]:
            visit(by_id[dependency])
        ordered.append(task)

    for task in sorted(tasks, key=lambda task: (-task["priority"], task["id"])):
        visit(task)
    return ordered


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / ".mub"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        ignore = self.directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        elif ignore.read_text().strip() != "*":
            raise ValueError(".mub/.gitignore must contain '*' so all board state stays private")
        self.lock = (self.directory / "owner.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("This project already has a running mub") from None
        self.data = read_state(root)
        (self.directory / "runs").mkdir(exist_ok=True)
        self.save()

    def save(self):
        temporary = self.directory / "state.tmp"
        with temporary.open("w") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.directory / "state.json")
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def close(self):
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
                    title=title or text.splitlines()[0][:80], request=text, brief="",
                    state="inbox", priority=0, depends_on=[], question="", result="",
                    session=None, gate=None, turns=0, revision=1, mode="prompt",
                    created=now(), updated=now())
        self.data["tasks"].append(task)
        return task

    def touch(self, task):
        task["revision"] += 1
        task["updated"] = now()

    def message(self, role, content, task_id=None):
        row = dict(id=len(self.data["messages"]) + 1, task_id=task_id,
                   role=role, content=content, created=now())
        self.data["messages"].append(row)
        return row

    def event(self, kind, task_id=None, text=""):
        row = dict(id=len(self.data["events"]) + 1, kind=kind, task_id=task_id,
                   text=text, handled=False, created=now())
        self.data["events"].append(row)
        return row

    def pending(self):
        return [e for e in self.data["events"] if not e["handled"]]
