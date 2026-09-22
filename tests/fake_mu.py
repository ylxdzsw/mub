#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time


ROOT = Path(os.environ.get("MUB_PROJECT", Path.cwd()))
STORE = ROOT / ".mub" / "fake-mu"


def read_session(session):
    path = STORE / f"{session}.json"
    return path, json.loads(path.read_text())


def write_session(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def new_session():
    STORE.mkdir(parents=True, exist_ok=True)
    counter = STORE / "counter"
    number = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(number))
    session = f"fake-{number}"
    write_session(STORE / f"{session}.json", {
        "active": {"busy": False},
        "clean": True,
        "transcript": "",
        "invocations": [],
    })
    print(session)


def socket_request(request):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(os.environ["MUB_SOCKET"])
        client.sendall((json.dumps(request) + "\n").encode())
        return json.loads(client.makefile("rb").readline())


def finish(path, data, code, text, clean=None):
    data["active"] = {"busy": False}
    data["transcript"] = text
    if clean is not None:
        data["clean"] = clean
    write_session(path, data)
    print(text, flush=True)
    return code


def run_mu(arguments):
    session = arguments[arguments.index("-s") + 1]
    path, data = read_session(session)
    data["active"] = {"busy": True}
    data["invocations"].append(arguments)
    write_session(path, data)
    mode = os.environ.get("FAKE_MU_MODE", "ok")
    is_pm = "MUB_PM_TOKEN" in os.environ
    delay = float(os.environ.get("FAKE_MU_DELAY", "0"))
    if delay:
        time.sleep(delay)

    stopped = False

    def interrupt(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)

    try:
        if is_pm:
            if os.environ.get("FAKE_MU_PEER_CONTROL"):
                response = socket_request({"op": "ack_workspace", "_peer_pid": 1})
                (STORE / "peer-control.json").write_text(json.dumps(response))
            if os.environ.get("FAKE_MU_PM_TRAP") and "retry" not in arguments:
                return finish(path, data, 3, "Execution: trapped PM command", False)
            plan = json.loads(os.environ.get("FAKE_MU_PLAN", "{}"))
            if os.environ.get("FAKE_MU_TRAP_REVIEW"):
                state = socket_request({"op": "status"})["result"]
                plan = {"tasks": []}
                for task in state["tasks"]:
                    if task["gate"] == "approval":
                        replies = [m for m in state["messages"] if m["role"] == "user"
                                   and m["id"] > task.get("blocked_after", 0)
                                   and m["task_id"] in (None, task["id"])]
                        if task["state"] == "blocked":
                            if replies and replies[-1]["content"] == "Yes, go ahead with that retry.":
                                plan["tasks"].append(dict(id=task["id"], state="queued", recovery="approve",
                                                          reason="The user approved the explained retry scope.",
                                                          user_message_id=replies[-1]["id"]))
                        elif os.environ["FAKE_MU_TRAP_REVIEW"] == "auto":
                            plan["tasks"].append(dict(id=task["id"], state="queued", recovery="approve",
                                                      reason="Inspected the command; routine work within the user's request."))
                        else:
                            question = "This retry disables all Bash traps for one invocation. May I continue?"
                            plan["tasks"].append(dict(id=task["id"], state="blocked", question=question))
                            plan["reply"] = question
                    elif task["state"] == "review":
                        plan["tasks"].append(dict(id=task["id"], state="done", result="Verified worker output"))
            if os.environ.get("FAKE_MU_WORKFLOW"):
                tasks = socket_request({"op": "status"})["result"]["tasks"]
                plan = {"reply": "I'll arrange that work.", "tasks": [
                    {"id": task["id"], "state": "done", "result": "Greeting complete"}
                    for task in tasks if task["state"] == "review"
                ] if tasks else [{"id": "greeting", "title": "Greeting", "brief": "Implement a greeting", "state": "queued"}]}
            response = socket_request({
                "op": "plan",
                "token": os.environ["MUB_PM_TOKEN"],
                "plan": plan,
            })
            print(json.dumps(response), flush=True)
            if not response.get("ok"):
                return finish(path, data, 1, response.get("error", "plan rejected"), False)
            if os.environ.get("FAKE_MU_PM_FAIL"):
                return finish(path, data, 1, "fake PM failed after staging", False)
            return finish(path, data, 0, "fake PM submitted plan", True)

        if mode == "trap" and "retry" not in arguments:
            return finish(path, data, 3, "Execution: trapped command", False)
        if mode == "retry" and "retry" not in arguments:
            return finish(path, data, 1, "fake worker failed before resume", False)
        if mode == "stream":
            print("LIVE: implementing greeting", flush=True)
            for index in range(20):
                if stopped:
                    return finish(path, data, 130, "fake worker interrupted", False)
                time.sleep(0.1)
                print(f"LIVE: check {index + 1}", flush=True)
        if mode in ("dirty", "long"):
            if mode == "dirty":
                (ROOT / "dirty-worker.txt").write_text("provisional worker change\n")
            while not stopped:
                time.sleep(0.02)
            return finish(path, data, 130, "fake worker interrupted", False)
        return finish(path, data, 0, "fake worker completed", True)
    except KeyboardInterrupt:
        return finish(path, data, 130, "fake worker interrupted", False)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "new":
        new_session()
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "status":
        if "-s" not in sys.argv:
            result = {"project_root": str(ROOT), "clean": True}
            if "--include-models" in sys.argv:
                result["available_models"] = {"providers": [{"id": "codex", "models": [
                    {"id": "codex/gpt-5.6-luna", "supported_efforts": ["medium", "high"]},
                    {"id": "codex/other", "supported_efforts": ["medium", "high"]},
                ]}]}
            print(json.dumps(result))
            return 0
        session = sys.argv[sys.argv.index("-s") + 1]
        _, data = read_session(session)
        print(json.dumps({"active": data["active"], "clean": data["clean"]}))
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "transcript":
        session = sys.argv[sys.argv.index("-s") + 1]
        _, data = read_session(session)
        print(data["transcript"])
        return 0
    return run_mu(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
