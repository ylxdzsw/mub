"""Opt-in real-model scheduler replay; workers are recorded, never executed.

Run with the checkout's Python environment. See scheduler_replay.md.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from muboard.engine import Engine
from muboard.output import conversation, excerpt, journal_events


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def rename_keys(value, mapping):
    return {mapping.get(k, k): v for k, v in value.items()}


def normalize(raw, histories):
    snapshot = copy.deepcopy(raw)
    snapshot = rename_keys(snapshot, {"events": "trigger_events"})
    snapshot["workspace"] = rename_keys(snapshot["workspace"], {"status": "git_status", "owner": "owner_session_id"})
    snapshot["messages"] = [rename_keys(m, {"state": "delivery_state"}) for m in snapshot["messages"]]
    snapshot["trigger_events"] = [rename_keys(e, {"exit": "outcome", "supersedes": "superseded_message_id"})
                                  for e in snapshot["trigger_events"]]
    sessions = []
    for old in snapshot["sessions"]:
        s = rename_keys(old, dict(hold="held", gate="continuation_state", reason="state_detail",
            blocked="wait_reason", user_retry_authorized="user_continue_authorized", active="current_run",
            context="history", last="last_run", steering="pending_redirect"))
        s.setdefault("revision", 0)
        s.setdefault("pending_redirect", None)
        h = rename_keys(s["history"], dict(omitted_turns="omitted_turn_count", read="read_command",
                                           unchanged="unchanged_since_previous_snapshot"))
        if h.get("unchanged_since_previous_snapshot"):
            previous = copy.deepcopy(histories[s["id"]])
            previous.update({k: v for k, v in h.items() if k != "unchanged_since_previous_snapshot"})
            h = previous
        histories[s["id"]] = copy.deepcopy(h)
        s["history"] = h
        if s["last_run"]:
            s["last_run"] = rename_keys(s["last_run"], dict(exit="outcome", action="original_operation",
                mode="workspace_access", trap="command_trap", clean="completed", trap_output="trap_evidence"))
        if s["current_run"]:
            s["current_run"] = rename_keys(s["current_run"], dict(id="run_id", action="operation",
                mode="workspace_access", trap="command_trap"))
            s["current_run"].setdefault("run_id", f"historical-{s['id']}")
        if s["pending_redirect"]:
            s["pending_redirect"] = rename_keys(s["pending_redirect"], dict(run_id="interrupted_run_id",
                supersedes="superseded_message_id", mode="workspace_access"))
        sessions.append(s)
    snapshot["sessions"] = sessions
    return snapshot


def recover(source, destination):
    destination.mkdir(parents=True, exist_ok=False)
    state = json.loads((source / ".mu/mub.json").read_text())
    ids = state["scheduler"].get("previous_sessions", [])
    if state["scheduler"].get("session"):
        ids.append(state["scheduler"]["session"])
    fixtures = []
    for sid in ids:
        path = source / ".mu/sessions" / f"{sid}.jsonl"
        histories = {}
        current = None
        number = 0
        for line, text in enumerate(path.read_text().splitlines(), 1):
            event = json.loads(text)
            if event["type"] == "prompt_queued":
                prompt = event["prompt"]["text"]
                if "SNAPSHOT:\n" not in prompt:
                    continue
                number += 1
                raw = json.loads(prompt.split("SNAPSHOT:\n", 1)[1])
                current = dict(id=f"{sid}-{number:02}", source=dict(journal=str(path), line=line,
                    seq=event["seq"], at=event["at"], sha256=hashlib.sha256(path.read_bytes()).hexdigest()),
                    original_snapshot=raw, snapshot=normalize(raw, histories), original_outputs=[], evidence={})
                fixtures.append(current)
            elif current and event["type"] == "provider_completed":
                current["original_outputs"].extend(event.get("projection", {}).get("items", []))
    # Identify worker journals by the exact historical byte boundary and visible turns.
    # No newest-state substitution: only the recorded prefix is supplied to context.
    journals = []
    for path in (source / ".mu/sessions").glob("*.jsonl"):
        if path.stem in ids:
            continue
        data = path.read_bytes()
        boundaries = set()
        offset = 0
        for line in data.splitlines(keepends=True):
            offset += len(line)
            boundaries.add(offset)
        journals.append((path, data, boundaries))
    cache = {}
    for fixture in fixtures:
        for s in fixture["snapshot"]["sessions"]:
            h = s["history"]
            if not h.get("read_command"):
                continue
            end = int(re.search(r"--before (\d+)", h["read_command"])[1])
            key = (s["id"], end)
            if key not in cache:
                matches = []
                for path, data, boundaries in journals:
                    if end not in boundaries:
                        continue
                    turns = conversation(json.loads(line) for line in data[:end].splitlines())["turns"]
                    visible = h["turns"]
                    if visible and all(any(t["turn_id"] == v["turn_id"] and
                        (t["request"] == v["request"] or excerpt(t["request"], 2000) == v["request"])
                        for t in turns) for v in visible):
                        matches.append(dict(journal=str(path), before=end,
                                            sha256=hashlib.sha256(data[:end]).hexdigest()))
                cache[key] = matches[0] if len(matches) == 1 else dict(unresolved=True, candidates=len(matches), before=end)
            fixture["evidence"][str(s["id"])] = cache[key]
    known = {}
    for fixture in fixtures:
        for key, evidence in fixture["evidence"].items():
            if not evidence.get("unresolved"):
                known.setdefault(key, set()).add(evidence["journal"])
    for fixture in fixtures:
        for key, evidence in fixture["evidence"].items():
            if evidence.get("unresolved") and len(known.get(key, [])) == 1:
                path = next(iter(known[key]))
                data = Path(path).read_bytes()[:evidence["before"]]
                h = next(s["history"] for s in fixture["snapshot"]["sessions"] if str(s["id"]) == key)
                if data.endswith(b"\n") and not h["turns"] and not conversation(json.loads(l) for l in data.splitlines())["turns"]:
                    evidence.clear()
                    evidence.update(journal=path, before=len(data), sha256=hashlib.sha256(data).hexdigest())
        save(destination / (fixture["id"] + ".json"), fixture)
    save(destination / "index.json", [dict(id=f["id"], source=f["source"], sessions=len(f["snapshot"]["sessions"]),
        trapped=[s["id"] for s in f["snapshot"]["sessions"] if s["continuation_state"] == "trapped"],
        unresolved=[k for k, v in f["evidence"].items() if v.get("unresolved")]) for f in fixtures])
    print(json.dumps(dict(recovered=len(fixtures), unresolved_prefixes=sum(v.get("unresolved", False) for v in cache.values()))))


class ReplayEngine(Engine):
    """Real scheduler/IPC/admission, frozen Git facts, inert worker starts."""

    def __init__(self, root, fixture, model):
        self.fixture = copy.deepcopy(fixture)
        self.launches = []
        self.requests = []
        self.probes = 0
        self.histories = {}
        super().__init__(root, scheduler_model=model)
        snapshot = self.fixture["snapshot"]
        for item in snapshot["sessions"]:
            s = self.store.new_session(item["name"])
            s.update(id=item["id"], name_source=item["name_source"], revision=item["revision"],
                     hold=item["held"], gate=item["continuation_state"], reason=item["state_detail"],
                     blocked=item["wait_reason"], retry_authorized=item["user_continue_authorized"])
            if redirect := item.get("pending_redirect"):
                s["steering"] = rename_keys(redirect, dict(interrupted_run_id="run_id",
                    superseded_message_id="supersedes", workspace_access="mode"))
            if item.get("handoff_failure"):
                s["handoff_failed"] = item["handoff_failure"]
            if last := item["last_run"]:
                s["last"] = rename_keys(last, dict(outcome="exit", original_operation="action",
                    workspace_access="mode", command_trap="trap", completed="clean"))
                s["last"].update(journal_offset=0, summary=last.get("diagnostic", ""))
                if last.get("trap_evidence"):
                    self.traps[s["id"]] = last["trap_evidence"].replace("/root/mote", str(root))
            h = copy.deepcopy(item["history"])
            evidence = fixture.get("evidence", {}).get(str(s["id"]))
            if evidence and not evidence.get("unresolved"):
                data = Path(evidence["journal"]).read_bytes()[:evidence["before"]]
                if hashlib.sha256(data).hexdigest() != evidence["sha256"]:
                    raise ValueError("Historical evidence changed")
                s["session"] = f"ses_replay{s['id']}"
                folder = root / ".mu/sessions"
                folder.mkdir(exist_ok=True)
                (folder / f"{s['session']}.jsonl").write_bytes(data)
                h["read_command"] = f"{self.client} context S{s['id']} --before {len(data)}"
            elif h.get("read_command"):
                raise ValueError(f"Cannot replay unresolved evidence for S{s['id']}")
            self.histories[s["id"]] = h
            if run := item["current_run"]:
                self._virtual_run(s, run["operation"], run["workspace_access"], run["command_trap"], run.get("message_id"), run["run_id"])
        self.data["messages"] = [rename_keys(m, dict(delivery_state="state")) for m in snapshot["messages"]]
        self.data["events"] = copy.deepcopy(snapshot["trigger_events"])
        self.data["next_event"] = max((e["id"] for e in self.data["events"]), default=0) + 1
        self.data["handoff"] = snapshot.get("handoff")
        self.data["scheduler"]["handoff_result"] = snapshot.get("handoff_result")
        self.data["owner"] = snapshot["workspace"]["owner_session_id"]
        self.store.save()
        self._workspace()

    def _workspace(self, force=True):
        self.probes += 1
        w = self.fixture["snapshot"]["workspace"]
        self.workspace = dict(clean=w["clean"], status=w["git_status"], owner=self.data.get("owner"))
        return self.workspace

    def _worker_context(self, session):
        return copy.deepcopy(self.histories[session["id"]])

    def _virtual_run(self, session, action, mode, trap, message_id, run_id=None):
        record = dict(id=run_id or f"virtual-{session['id']}-{len(self.launches)}", kind="worker", session_id=session["id"],
                      action=action, mode=mode, trap=trap, message_id=message_id, journal_offset=0, pid=None)
        active = dict(record=record, virtual=True)
        self.active[record["id"]] = active
        return active

    def _start_operation(self, session, operation, mode, trap):
        self.launches.append(dict(session_id=session["id"], operation=operation, mode=mode, trap=trap))
        session.update(revision=session["revision"] + 1, retry_authorized=False, blocked=None)
        if mode == "readwrite":
            self.data["owner"] = session["id"]
        for message in self.data["messages"]:
            if message["id"] == operation.get("message_id"):
                message["state"] = "inflight"
        return self._virtual_run(session, operation["action"], mode, trap, operation.get("message_id"))

    def request(self, req):
        if req.get("op") == "shutdown":
            return super().request(req)
        entry = dict(request=req)
        if active := self._running(None):
            entry["seconds_since_start"] = round(time.monotonic() - active["started_at"], 3)
        self.requests.append(entry)
        try:
            result = super().request(req)
            entry["result"] = result
            return result
        except Exception as error:
            entry["error"] = str(error)
            raise

    def close(self):
        self.active = {k: a for k, a in self.active.items() if not a.get("virtual")}
        super().close()

    def advance(self, transition):
        if transition == "recheck":
            self.store.event("recheck")
        else:
            handoff = self.data.get("handoff")
            if not handoff:
                raise ValueError("Scripted completion requires an admitted handoff")
            source = self.store.session(handoff["source"])
            run = self.active.pop(handoff["run_id"])["record"]
            released = transition == "handoff-success"
            summary = "Committed completed task-owned work." if released else "Cannot safely commit incomplete work."
            source.update(revision=source["revision"] + 1, last=dict(exit="clean", action="commit", mode="readwrite",
                trap="destructive", clean=True, message_id=None, journal_offset=0, summary=summary))
            self.histories[source["id"]]["turns"].append(dict(turn_id="scripted-commit",
                request="Commit completed task-owned work for handoff.", response=summary))
            if released:
                self.fixture["snapshot"]["workspace"].update(clean=True, git_status="")
                self.data["owner"] = None
            self._workspace()
            self._finish_handoff(run, "clean")
            self.store.event("worker_exit", source["id"], outcome="clean")
        self.store.save()


def evaluate(result, expectations):
    actions = result["scheduler"].get("decision", {}).get("actions", [])
    problems = []
    if result["scheduler"].get("error"):
        problems.append("Scheduler invocation failed: " + result["scheduler"]["error"])
    for rule in expectations:
        candidates = [a for a in actions if a["applied"]] if rule["expect"] == "present" else actions
        matched = any(all((a["action"].get(k) if k != "session_id" else a[k]) == v
                          for k, v in rule["action"].items()) for a in candidates)
        if matched != (rule["expect"] == "present"):
            problems.append(rule["why"])
    return problems


def run_pass(board, fixture, model, destination, timeout):
    snapshot = board._scheduler_snapshot()
    save(destination / "snapshot.json", snapshot)
    (destination / "prompt.txt").write_text(board._scheduler_prompt(snapshot, bootstrap=not board.data["scheduler"]["session"]))
    first_launch = len(board.launches)
    first_request = len(board.requests)
    active = board._spawn(snapshot=snapshot)
    start = time.monotonic()
    timed_out = False
    while active["process"].poll() is None:
        board.server.drain(board.request)
        board._prepare(active)
        if time.monotonic() - start > timeout and not timed_out:
            timed_out = True
            board._stop(active)
        elif time.monotonic() - start > timeout + 5:
            board._stop(active)
        time.sleep(0.02)
    board._finish(active)
    del board.active[active["record"]["id"]]
    result = dict(case=fixture["id"], model=model, root=str(board.root), timed_out=timed_out,
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True).strip(),
        engine_sha256=hashlib.sha256((Path(__file__).resolve().parents[1] / "muboard/engine.py").read_bytes()).hexdigest(),
        policy_sha256=hashlib.sha256(board._scheduler_policy().replace(str(board.root), "<ROOT>").encode()).hexdigest(),
        elapsed_seconds=board.data["scheduler"]["last_usage"]["seconds"],
        scheduler=copy.deepcopy(board.data["scheduler"]), requests=copy.deepcopy(board.requests[first_request:]),
        launches=copy.deepcopy(board.launches[first_launch:]), handoff=copy.deepcopy(board.data.get("handoff")),
        workspace_probes=board.probes)
    result["problems"] = evaluate(result, fixture.get("expectations", []))
    result["assessment"] = "fail" if result["problems"] else "pass" if fixture.get("expectations") else "ungraded"
    save(destination / "result.json", result)
    sid = board.data["scheduler"]["session"]
    events = list(journal_events(board.root, sid, active["record"]["journal_offset"]))
    save(destination / "tool-calls.json", [item for e in events if e["type"] == "provider_completed"
        for item in e.get("projection", {}).get("items", []) if item["type"] == "bash_call"])
    transcript = subprocess.run(["mu", "transcript", "-s", sid, "-o", "concise"], cwd=board.root, capture_output=True, text=True)
    (destination / "transcript.txt").write_text(transcript.stdout + transcript.stderr)
    print(json.dumps(dict(case=result["case"], assessment=result["assessment"], problems=result["problems"],
                          elapsed_seconds=result["elapsed_seconds"], result=str(destination / "result.json")), ensure_ascii=False), flush=True)
    return result


def preserve(root, fixture, destination):
    """Keep local replay inputs and Mu evidence after the temporary root disappears."""
    shutil.copytree(root / ".mu/sessions", destination / "journals")
    if (root / ".mu/objects").exists():
        shutil.copytree(root / ".mu/objects", destination / "objects")
    portable = copy.deepcopy(fixture)
    for key, evidence in portable.get("evidence", {}).items():
        if not evidence.get("unresolved"):
            evidence["journal"] = str(destination / "journals" / f"ses_replay{key}.jsonl")
    save(destination / "replay.json", portable)


def run(fixture_path, model, destination, timeout):
    fixture = json.loads(fixture_path.read_text())
    destination.mkdir(parents=True, exist_ok=False)
    # Short, independent Git root avoids Unix socket path limits and inherited project instructions.
    root = Path(tempfile.mkdtemp(prefix="mub-replay-"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH")]))
    board = ReplayEngine(root, fixture, model)
    save(destination / "fixture.json", fixture)
    try:
        result = run_pass(board, fixture, model, destination, timeout)
        for number, stage in enumerate(fixture.get("followups", []), 1):
            first_launch = len(board.launches)
            board.advance(stage["transition"])
            folder = destination / f"followup-{number}"
            folder.mkdir()
            stage_result = run_pass(board, dict(stage, id=f"{fixture['id']}/{stage['transition']}"), model, folder, timeout)
            stage_result["transition_launches"] = board.launches[first_launch:len(board.launches) - len(stage_result["launches"])]
            save(folder / "result.json", stage_result)
            result.setdefault("followups", []).append(stage_result)
            result["problems"].extend(stage_result["problems"])
            if stage_result["assessment"] == "fail":
                result["assessment"] = "fail"
        save(destination / "result.json", result)
    finally:
        board.close()
        preserve(root, fixture, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    recover_parser = sub.add_parser("recover")
    recover_parser.add_argument("source", type=Path)
    recover_parser.add_argument("destination", type=Path)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("fixture", type=Path)
    run_parser.add_argument("destination", type=Path)
    run_parser.add_argument("--model", default="copilot/gpt-5.6-luna:high")
    run_parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    if args.command == "recover":
        recover(args.source.resolve(), args.destination.resolve())
    else:
        run(args.fixture.resolve(), args.model, args.destination.resolve(), args.timeout)


if __name__ == "__main__":
    main()
