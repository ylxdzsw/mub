#!/usr/bin/env python3
"""Local Mu stand-in: scripted scheduling, streaming, traps, and Git writes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path.cwd()
STORE = ROOT / ".mu" / "fake"
STORE.mkdir(parents=True, exist_ok=True)
(STORE / ".gitignore").write_text("*\n")
JOURNALS = ROOT / ".mu" / "sessions"
JOURNALS.mkdir(exist_ok=True)
(JOURNALS / ".gitignore").write_text("*\n")
STOPPED = False


def interrupt(signum, frame):
    global STOPPED
    STOPPED = True


signal.signal(signal.SIGINT, interrupt)
signal.signal(signal.SIGTERM, interrupt)


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def delay(seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until and not STOPPED:
        time.sleep(0.01)


def schedule(snapshot, config):
    if "plan" in config:
        return config["plan"]
    actions = []
    sessions = snapshot["sessions"]
    writer = any(s["active"] and s["active"]["mode"] == "readwrite" for s in sessions)
    owner = snapshot["workspace"]["owner"]
    dirty = not snapshot["workspace"]["clean"]
    order = config.get("order", [])
    sessions = sorted(sessions, key=lambda s: order.index(s["id"]) if s["id"] in order else len(order) + s["id"])
    for session in sessions:
        key = session["id"]
        if session["active"] or session["hold"]:
            continue
        writable = not writer and (not dirty or owner == key)
        if session["gate"] == "trapped" or session["retry_authorized"]:
            if writable:
                actions.append(dict(type="retry", session_id=key, mode="readwrite", trap="off", reason="Scripted trap approval for this turn"))
                writer = True
            continue
        if session["gate"]:
            continue
        if dirty and owner == key and writable and (session["last"] or {}).get("action") != "commit":
            actions.append(dict(type="commit", session_id=key, reason="Release checkout"))
            writer = True
            continue
        message = next((m for m in snapshot["messages"] if m["session_id"] == key), None)
        if not message or message["state"] != "pending":
            continue
        if message["text"].startswith("depends "):
            prerequisite = int(message["text"].split()[1])
            source = next(s for s in sessions if s["id"] == prerequisite)
            if not source["last"] or source["gate"] or source["active"]:
                actions.append(dict(type="label", session_id=key, status="blocked", reason=f"Waiting for S{prerequisite}"))
                continue
        mode = "readwrite" if message["text"].startswith("write") else "readonly"
        if mode == "readwrite" and not writable:
            continue
        actions.append(dict(type="dispatch", message_id=message["id"], mode=mode))
        writer |= mode == "readwrite"
    return dict(reason="Scripted scheduling decision", actions=actions)


def main():
    args = sys.argv[1:]
    if args[0] == "new":
        key = "fake-" + uuid.uuid4().hex[:12]
        save(STORE / f"{key}.json", dict(clean=True, active=dict(busy=False), transcript="", invocations=[]))
        (JOURNALS / f"{key}.jsonl").write_text(json.dumps(dict(type="meta", session_id=key)) + "\n")
        print(key)
        return 0
    key = args[args.index("-s") + 1] if "-s" in args else None
    path = STORE / f"{key}.json" if key else None
    data = json.loads(path.read_text()) if key else None
    if args[0] == "status":
        if data:
            busy = data["active"]["busy"] and Path(f"/proc/{data['active'].get('pid', 0)}").exists()
            print(json.dumps(dict(clean=data["clean"], active=dict(busy=busy))))
        else:
            print(json.dumps(dict(project_root=str(ROOT), available_models=dict(providers=[dict(models=[
                dict(id="fake/model", supported_efforts=["low", "high"]), dict(id="fake/other", supported_efforts=[])
            ])]))))
        return 0
    if args[0] == "transcript":
        print(data["transcript"], end="")
        return 0
    config_path = STORE / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    retry = args[0] == "retry"
    prompt = sys.stdin.read() if not retry else ""
    role = os.environ["MUB_ROLE"]
    if role == "worker":
        delay(config.get("before_prompt_delay", 0))
        if STOPPED or config.get("early_fail"):
            print("Stopped before accepting input" if STOPPED else "Failed before accepting input")
            return 130 if STOPPED else 1
    data["active"] = dict(busy=True, pid=os.getpid())
    data["clean"] = False
    data["invocations"].append(dict(args=args, prompt=prompt, role=role))
    if not retry:
        data["prompt"] = prompt
        data["transcript"] += "\nuser: " + prompt + "\n"
        with (JOURNALS / f"{key}.jsonl").open("a") as journal:
            prompt_id = f"p{len(data['invocations'])}"
            for kind in ("prompt_queued", "prompt_materialized"):
                journal.write(json.dumps(dict(type=kind, prompt_id=prompt_id)) + "\n")
    save(path, data)
    if role == "worker":
        print("LIVE: worker started", flush=True)

    def finish(code, text, clean):
        data.update(clean=clean, active=dict(busy=False))
        data["transcript"] += "\nassistant: " + text + "\n"
        save(path, data)
        print(text, flush=True)
        return code

    if role == "scheduler":
        snapshot = json.loads(prompt.split("SNAPSHOT:\n", 1)[1])
        save(STORE / ("snapshot-" + uuid.uuid4().hex + ".json"), snapshot)
        delay(config.get("scheduler_delay", 0.02))
        if STOPPED:
            return finish(130, "scheduler interrupted", False)
        if config.get("scheduler_fail"):
            return finish(1, "fake provider failure", False)
        plan = schedule(snapshot, config)
        delay(config.get("after_plan_delay", 0))
        return finish(0, json.dumps(plan), True)

    task = data["prompt"].split("USER MESSAGE:\n", 1)[-1].strip()
    own_file = ROOT / f"work-{key}.txt"
    if "SCHEDULER HANDOFF REQUEST" in data["prompt"]:
        if own_file.exists() and not config.get("commit_refused"):
            subprocess.run(["git", "add", str(own_file)], check=True)
            subprocess.run(["git", "-c", "user.name=Fake", "-c", "user.email=fake@example.com", "commit", "-qm", "Fake handoff"], check=True)
        return finish(0, "Committed task-owned work" if not config.get("commit_refused") else "Cannot safely commit incomplete work", True)
    if task.startswith("trap") and not retry:
        return finish(3, "# Write fixture\n$ [reversible] cat > work.txt\n< complete stdin\n⊘ trapped before execution", False)
    if task.startswith("fail"):
        return finish(1, "fake provider failure", False)
    if task.startswith("write"):
        own_file.write_text(task + "\n")
    delay(config.get("worker_delay", 0.08))
    if "hold" in task and not config.get("release_hold"):
        while not STOPPED:
            time.sleep(0.01)
    if STOPPED:
        return finish(130, "worker interrupted", False)
    return finish(0, "Handled: " + task, True)


if __name__ == "__main__":
    raise SystemExit(main())
