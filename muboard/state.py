"""The single mub snapshot; Mu owns conversation journals."""

import fcntl
import json
import os
from pathlib import Path
import subprocess


def project_root(directory):
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=directory,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "mub requires a Git worktree")
    return Path(result.stdout.strip()).resolve()


def fresh_state():
    return dict(version=3, next_session=1, next_message=1, next_event=1,
                sessions=[], messages=[], inflight=[], events=[], owner=None,
                scheduler=dict(session=None, error=None, reason=""),
                models=dict(scheduler=None, worker=None))


def read_state(root):
    path = Path(root) / ".mu" / "mub.json"
    if not path.exists():
        return fresh_state()
    data = json.loads(path.read_text())
    if data.get("version") != 3:
        raise ValueError("This is an old task-board snapshot. Archive .mu/mub.json before starting the session scheduler; Mu journals are unchanged.")
    return data


class Store:
    def __init__(self, root):
        self.root = project_root(root)
        self.directory = self.root / ".mu"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.lock = (self.directory / "mub.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("This worktree already has a running mub") from None
        try:
            self.data = read_state(self.root)
            # Local Git metadata avoids modifying tracked project configuration.
            result = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"],
                                    cwd=self.root, capture_output=True, text=True, check=True)
            exclude = self.root / result.stdout.strip()
            previous = exclude.read_text() if exclude.exists() else ""
            patterns = ["/.mu/mub.json", "/.mu/mub.json.tmp", "/.mu/mub.lock", "/.mu/mub.sock"]
            missing = [p for p in patterns if p not in previous.splitlines()]
            if missing:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a") as stream:
                    stream.write(("\n" if previous and not previous.endswith("\n") else "")
                                 + "\n".join(missing) + "\n")
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
        self.lock.close()

    def session(self, session_id):
        for session in self.data["sessions"]:
            if session["id"] == session_id:
                return session
        raise ValueError(f"Unknown session S{session_id}")

    def new_session(self, name=None):
        key = self.data["next_session"]
        self.data["next_session"] += 1
        session = dict(id=key, name=name or f"Session {key}", session=None, hold=False,
                       gate=None, reason="", blocked=None, last=None, revision=0,
                       retry_authorized=False)
        self.data["sessions"].append(session)
        return session

    def submit(self, session_id, text):
        text = text.strip()
        if not text:
            raise ValueError("Message cannot be empty")
        session = self.session(session_id)
        # A reply is not permission to retry an interrupted turn.
        if session["hold"] or session["gate"] or session["blocked"]:
            session["revision"] += 1
        session.update(hold=False, blocked=None, retry_authorized=False)
        message = dict(id=self.data["next_message"], session_id=session_id, text=text, state="pending")
        self.data["next_message"] += 1
        self.data["messages"].append(message)
        self.event("submitted", session_id, message_id=message["id"])
        return message

    def event(self, kind, session_id=None, **detail):
        event = dict(id=self.data["next_event"], kind=kind, session_id=session_id, **detail)
        self.data["next_event"] += 1
        self.data["events"].append(event)
        return event
