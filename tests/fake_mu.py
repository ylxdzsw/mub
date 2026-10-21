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
import xml.etree.ElementTree as ET

ROOT = Path.cwd()
STORE = ROOT / ".mu" / "fake"
STORE.mkdir(parents=True, exist_ok=True)
(STORE / ".gitignore").write_text("*\n")
JOURNALS = ROOT / ".mu" / "sessions"
JOURNALS.mkdir(exist_ok=True)
(JOURNALS / ".gitignore").write_text("*\n")
STOPPED = False
SOFT_STOPPED = False


def interrupt(signum, frame):
    global STOPPED
    STOPPED = True


def soft_interrupt(signum, frame):
    global SOFT_STOPPED
    SOFT_STOPPED = True


signal.signal(signal.SIGINT, interrupt)
signal.signal(signal.SIGTERM, interrupt)


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def recover_incomplete(journal, turn_id):
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    queued = set()
    compacting = False
    for event in events:
        if event["type"] == "prompt_queued":
            queued.add(event["prompt_id"])
        elif event["type"] == "prompt_materialized":
            queued.discard(event["prompt_id"])
        elif event["type"] == "compaction_started":
            compacting = True
        elif event["type"] == "compaction_applied":
            compacting = False
    with journal.open("a") as stream:
        for prompt_id in queued:
            stream.write(json.dumps(dict(type="prompt_materialized", prompt_id=prompt_id,
                                         turn_id=turn_id)) + "\n")
        if compacting:
            stream.write(json.dumps(dict(type="compaction_applied", seq=len(events) + 1)) + "\n")


def delay(seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until and not STOPPED:
        time.sleep(0.01)


def _scalar(text):
    text = (text or "").strip()
    if not text:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _xml_value(element):
    children = list(element)
    if not children:
        return _scalar(element.text)
    result = {}
    for child in children:
        value = _xml_value(child)
        if child.tag in result:
            if not isinstance(result[child.tag], list):
                result[child.tag] = [result[child.tag]]
            result[child.tag].append(value)
        else:
            result[child.tag] = value
    return result


def _items(value, child):
    if value is None:
        return []
    if isinstance(value, dict) and child in value:
        value = value[child]
    return value if isinstance(value, list) else [value]


def parse_snapshot(prompt):
    document = prompt.split("SNAPSHOT:\n", 1)[1].strip()
    root = ET.fromstring(document)
    snapshot = _xml_value(root)
    sessions = _items(snapshot.get("sessions"), "session")
    messages = []
    for session in sessions:
        nested = _items(session.pop("messages", None), "message")
        for message in nested:
            message.setdefault("session_id", session["id"])
            messages.append(message)
    snapshot["sessions"] = sessions
    snapshot["messages"] = messages
    snapshot["trigger_events"] = _items(snapshot.get("trigger_events"), "event")
    return snapshot


def _action_kind(action):
    return action["action"]


def scheduler_command(action, snapshot):
    kind = _action_kind(action)
    session_id = action.get("session_id")
    if kind == "run":
        return ["run", "--trap", action["trap"], f"S{session_id}"]
    if kind == "rename":
        return ["rename", f"S{session_id}", action["name"]]
    if kind == "mark-failed":
        return ["mark-failed", f"S{session_id}", action["reason"]]
    if kind == "note":
        if action.get("wait_reason") is None and action.get("note") is None:
            return ["note", f"S{session_id}", "--clear"]
        return ["note", f"S{session_id}", action.get("note", action.get("wait_reason"))]
    raise ValueError(f"Unknown scheduler fixture action: {kind}")


def invoke_scheduler_command(action, snapshot):
    args = scheduler_command(action, snapshot)
    result = subprocess.run([sys.executable, "-m", "muboard", "-C", str(ROOT), *args],
                            cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True)
    output = result.stdout.strip() or result.stderr.strip()
    return result.returncode, output


def schedule(snapshot, config):
    if "scheduler_actions_once" in config:
        marker = STORE / "scheduler-actions-used"
        if marker.exists():
            return []
        marker.write_text("used\n")
        return config["scheduler_actions_once"]
    if "scheduler_actions" in config:
        return config["scheduler_actions"]
    actions = []
    sessions = snapshot["sessions"]
    writer = any(s.get("current_run") and s["current_run"].get("workspace_access") == "readwrite"
                 for s in sessions)
    owner = snapshot["workspace"].get("owner_session_id")
    dirty = not snapshot["workspace"]["clean"]
    order = config.get("order", [])
    sessions = sorted(sessions, key=lambda s: order.index(s["id"]) if s["id"] in order else len(order) + s["id"])

    def writable(key):
        return not writer and (not dirty or owner == key)

    def retry_trap(session):
        if session.get("continuation_state") == "trapped":
            return "destructive"
        return "reversible" if (session.get("last_run") or {}).get("workspace_access", "readonly") == "readonly" else "destructive"

    for session in sessions:
        key = session["id"]
        if session["held"]:
            continue
        steering = session.get("pending_redirect")
        if steering:
            if (steering["phase"] == "recovery" and session["user_continue_authorized"]):
                trap = retry_trap(session)
                if trap == "reversible" or writable(key):
                    actions.append(dict(action="run", session_id=key, trap=trap))
                    writer |= trap != "reversible"
            continue
        if session.get("current_run"):
            continue
        if session.get("continuation_state") == "trapped" or session["user_continue_authorized"]:
            trap = retry_trap(session)
            if trap == "reversible" or writable(key):
                actions.append(dict(action="run", session_id=key, trap=trap))
                writer |= trap != "reversible"
            continue
        if session.get("continuation_state"):
            continue
        message = next((m for m in snapshot["messages"] if m["session_id"] == key), None)
        if not message or message["delivery_state"] != "pending":
            continue
        if message["text"].startswith("depends "):
            prerequisite = int(message["text"].split()[1])
            source = next(s for s in sessions if s["id"] == prerequisite)
            if not source.get("last_run") or source.get("continuation_state") or source.get("current_run"):
                actions.append(dict(action="note", session_id=key, note=f"Waiting for S{prerequisite}"))
                continue
        text = message["text"]
        trap = "destructive" if text.startswith("write") else "reversible"
        if trap != "reversible" and not writable(key) and not (dirty and owner and owner != key):
            continue
        actions.append(dict(action="run", session_id=key, trap=trap))
        writer |= trap != "reversible"
    return actions


def main():
    args = sys.argv[1:]
    if args[0] == "cat":
        print("[stdin]\n")
        print(sys.stdin.read(), end="")
        return 0
    if args[0] == "new":
        key = "fake-" + uuid.uuid4().hex[:12]
        save(STORE / f"{key}.json", dict(clean=True, active=dict(busy=False), transcript="", invocations=[], new_args=args))
        (JOURNALS / f"{key}.jsonl").write_text(json.dumps(dict(type="meta", session_id=key)) + "\n")
        print(key)
        return 0
    key = args[args.index("-s") + 1] if "-s" in args else None
    path = STORE / f"{key}.json" if key else None
    data = json.loads(path.read_text()) if key else None
    if args[0] == "status":
        if data:
            busy = data["active"]["busy"] and Path(f"/proc/{data['active'].get('pid', 0)}").exists()
            model = args[args.index("--model") + 1] if "--model" in args else "fake/model"
            print(json.dumps(dict(clean=data["clean"], active=dict(busy=busy), model=dict(canonical=model),
                                  context_tokens=420, context_window=1000, context_usage_source="estimated")))
        else:
            print(json.dumps(dict(project_root=str(ROOT), model=dict(canonical="fake/model"), available_models=dict(providers=[dict(models=[
                dict(id="fake/model", supported_efforts=["low", "high"]), dict(id="fake/other", supported_efforts=[])
            ])]))))
        return 0
    if args[0] == "transcript":
        print(data["transcript"], end="")
        return 0
    config_path = STORE / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if config.get("soft_interrupt", True):
        signal.signal(signal.SIGQUIT, soft_interrupt)
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
    data["invocations"].append(dict(args=args, prompt=prompt, role=role, tty=sys.stdout.isatty()))
    if not retry:
        data["prompt"] = prompt
        data["turn_id"] = f"t{len(data['invocations'])}"
        model = args[args.index("-m") + 1] if "-m" in args else "fake/model"
        data["transcript"] += f"\n{model} ~42% {ROOT}\nmu> " + prompt + "\n"
        with (JOURNALS / f"{key}.jsonl").open("a") as journal:
            prompt_id = f"p{len(data['invocations'])}"
            journal.write(json.dumps(dict(type="prompt_queued", prompt_id=prompt_id, cwd=str(ROOT),
                                          prompt=dict(kind="text", text=prompt))) + "\n")
            journal.write(json.dumps(dict(type="prompt_materialized", prompt_id=prompt_id, turn_id=data["turn_id"])) + "\n")
    exchange_id = f"e{len(data['invocations'])}"
    journal_path = JOURNALS / f"{key}.jsonl"
    if retry:
        recover_incomplete(journal_path, data["turn_id"])
    with (JOURNALS / f"{key}.jsonl").open("a") as journal:
        journal.write(json.dumps(dict(type="provider_requested", turn_id=data["turn_id"], exchange_id=exchange_id)) + "\n")
    save(path, data)
    if role == "worker":
        print("LIVE: worker started", flush=True)

    def finish(code, text, clean):
        data.update(clean=clean, active=dict(busy=False))
        data["transcript"] += "\nassistant: " + text + "\n"
        save(path, data)
        with (JOURNALS / f"{key}.jsonl").open("a") as journal:
            journal.write(json.dumps(dict(type="provider_completed", exchange_id=exchange_id,
                usage=dict(input_tokens=100, cache_read_input_tokens=60, output_tokens=20, reasoning_output_tokens=5),
                projection=dict(kind="assistant", items=[dict(type="text", text=text)] if clean else []))) + "\n")
        print(text, flush=True)
        return code

    if role == "scheduler":
        snapshot = parse_snapshot(prompt)
        save(STORE / ("snapshot-" + uuid.uuid4().hex + ".json"), snapshot)
        if diagnostic := config.get("scheduler_stderr"):
            print(diagnostic, file=sys.stderr, flush=True)
        delay(config.get("scheduler_delay", 0.02))
        if STOPPED:
            return finish(130, "scheduler interrupted", False)
        if config.get("scheduler_fail"):
            return finish(1, "fake provider failure", False)
        actions = schedule(snapshot, config)
        results = []
        for action in actions:
            try:
                code, output = invoke_scheduler_command(action, snapshot)
            except (OSError, ValueError, StopIteration) as error:
                code, output = 1, str(error)
            results.append(dict(action=action, code=code, output=output))
        data["scheduler_commands"] = results
        delay(config.get("after_action_delay", 0))
        if config.get("scheduler_fail_after_actions"):
            return finish(1, "Scheduler failed after admitting work", False)
        return finish(0, config.get("scheduler_stdout", "Scheduling pass complete."), True)

    task = data["prompt"]
    own_file = ROOT / f"work-{key}.txt"
    if task.startswith("<system-request>\n"):
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
        while not STOPPED and not SOFT_STOPPED:
            time.sleep(0.01)
    if STOPPED:
        return finish(130, "worker interrupted", False)
    if SOFT_STOPPED and not config.get("natural_completion"):
        return finish(0, "Stopped at the next safe boundary", False)
    return finish(0, "Handled: " + task, True)


if __name__ == "__main__":
    raise SystemExit(main())
