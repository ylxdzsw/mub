"""Event-driven orchestration; all mutations run on the TUI's thread."""

import copy
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

from .state import Store, now


def process_stamp(pid):
    """Linux PID birth time, including the boot ID, to avoid PID reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]
    except FileNotFoundError:
        return None


def session_members(sid):
    members = []
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z" and int(fields[3]) == sid:
                members.append(int(path.parent.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return members


def owned_members(record):
    stamp = record.get("stamp")
    if not stamp or not stamp.startswith(Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":"):
        return []
    leader = process_stamp(record["pid"])
    if leader and leader != stamp:
        return []  # A new leader means the original process session is gone.
    return [(pid, birth) for pid in session_members(record["pid"])
            if (birth := process_stamp(pid)) is not None]


def signal_process(pid, stamp, signum):
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if stamp and process_stamp(pid) == stamp:
            signal.pidfd_send_signal(fd, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def tail(path, size=60000):
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - size))
        return stream.read().decode("utf-8", "replace")


class Engine:
    def __init__(self, root, *, mu="mu", pm_model=None, worker_model=None,
                 max_turns=8, timeout=1800, paused=False):
        self.root = Path(root).resolve()
        self.store = Store(self.root)
        self.data = self.store.data
        self.mu = mu
        models = self.data.setdefault("models", dict(pm=None, worker=None))
        self.pm_model = pm_model if pm_model is not None else models["pm"]
        self.worker_model = worker_model if worker_model is not None else models["worker"]
        self.model_catalog = None
        self.max_turns = max_turns
        self.timeout = timeout
        self.active = {}
        self.done = False
        self.stopping = None
        self.finish_task = None
        self.server = None
        self.pm_failures = 0
        self.pm_recovery = None
        self.next_pm = 0.0
        self.closed = False
        self.client_command = shlex.join([sys.executable, "-m", "muboard", "-C", str(self.root)])
        try:
            self._recover()
            dirty = self.workspace()
            if dirty and self.data["hold"] is None:
                self.data["workspace_block"] = "Checkout has existing changes. Inspect them, then accept the baseline (b)."
            if paused:
                self.data["paused"] = True
            self.store.save()
        except BaseException:
            self.store.close()
            raise

    def workspace(self):
        result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _mu(self, args, timeout=20):
        return subprocess.run([self.mu, *args], cwd=self.root, capture_output=True,
                              text=True, timeout=timeout)

    def _session_status(self, session):
        result = self._mu(["status", "-s", session, "--json"])
        if result.returncode:
            raise RuntimeError(result.stdout.strip() or result.stderr.strip() or "Cannot inspect Mu session")
        return json.loads(result.stdout)

    def models(self):
        if self.model_catalog is None:
            result = self._mu(["status", "--json", "--include-models"])
            if result.returncode:
                raise RuntimeError(result.stdout.strip() or result.stderr.strip())
            providers = json.loads(result.stdout)["available_models"]["providers"]
            self.model_catalog = [model for provider in providers for model in provider["models"]]
        return self.model_catalog

    def _recover(self):
        for run in self.data["runs"]:
            if run["status"] not in ("running", "starting"):
                continue
            if owned_members(run):
                raise RuntimeError(f"Previous run {run['id']} still has processes in session {run['pid']}. Stop them before reopening.")
            if run.get("session"):
                status = self._session_status(run["session"])
                if (status.get("active") or {}).get("busy"):
                    raise RuntimeError(f"Mu session {run['session']} is still busy; refusing a replacement worker")
            run.update(status="interrupted", finished=now())
            if run["kind"] == "worker":
                task = self.store.task(run["task_id"])
                cancelled = run.get("stop_reason") == "cancelled"
                task.update(state="cancelled" if cancelled else "needs_input",
                            gate=None if cancelled else "interrupted",
                            question="" if cancelled else "Previous owner stopped. Inspect the run and resume explicitly.")
                self.store.touch(task)
                self.data["hold"] = task["id"]
                if cancelled:
                    run["status"] = "cancelled"
                    self._release_cancelled(task)
            self.store.event("interrupted", run["task_id"], f"Run {run['id']} was interrupted")

    def state(self):
        return dict(root=str(self.root), paused=self.data["paused"], stopping=self.stopping,
                    models=dict(pm=self.pm_model, worker=self.worker_model),
                    error=self.data["error"] or self.data["workspace_block"], hold=self.data["hold"],
                    pm=self.active.get("pm", {}).get("record"),
                    worker=self.active.get("worker", {}).get("record"),
                    tasks=self.data["tasks"], messages=self.data["messages"][-100:],
                    decisions=self.data["decisions"], runs=self.data["runs"][-100:])

    def request(self, req):
        op = req.get("op")
        if op not in ("status", "show", "log", "plan", "models") and self._agent_peer(req.get("_peer_pid")):
            raise ValueError("Owned agents may inspect the board or submit a PM plan; this control requires user input")
        if op == "status":
            return self.state()
        if op == "models":
            self.model_catalog = None
            return dict(available=self.models(), selected=self.state()["models"])
        if op == "show":
            task = self.store.task(int(req["task_id"]))
            return dict(task=task, messages=[m for m in self.data["messages"] if m["task_id"] == task["id"]],
                        runs=[r for r in self.data["runs"] if r["task_id"] == task["id"]])
        if op == "log":
            run = next((r for r in self.data["runs"] if r["id"] == req["run_id"]), None)
            if not run:
                raise ValueError("Unknown run")
            return dict(text=tail(run["log_path"]) if Path(run["log_path"]).exists() else "Starting…")
        if op == "plan":
            active = self.active.get("pm")
            if not active or req.get("token") != active["token"]:
                raise ValueError("Only the current PM invocation can submit its plan")
            self._validate_plan(req["plan"], active)
            active["plan"] = req["plan"]
            active["record"]["plan"] = req["plan"]
            self.store.save()
            return dict(staged=True)
        if self.stopping and op != "shutdown":
            raise ValueError("Board is shutting down")
        if op == "set_models":
            selections = req["models"]
            if not isinstance(selections, dict) or not selections or set(selections) - {"pm", "worker"}:
                raise ValueError("Select a PM model, worker model, or both")
            catalog = {model["id"]: model for model in self.models()}
            for reference in selections.values():
                if reference is None:
                    continue
                if not isinstance(reference, str):
                    raise ValueError("Model must be a configured model reference or null for Mu's default")
                name, _, effort = reference.partition(":")
                if name not in catalog or (effort and effort not in (catalog[name].get("supported_efforts") or [])):
                    raise ValueError(f"Unknown model or effort: {reference}")
            for role, reference in selections.items():
                setattr(self, f"{role}_model", reference)
                self.data["models"][role] = reference
            result = dict(models=self.state()["models"])
        elif op == "add":
            task = self.store.add_task(req["text"], req.get("title"))
            self.store.message("user", req["text"], task["id"])
            self.store.event("submitted", task["id"])
            result = dict(task_id=task["id"])
        elif op == "reply":
            text = req["text"].strip()
            if not text:
                raise ValueError("Message cannot be empty")
            task_id = req.get("task_id")
            if task_id is not None:
                task = self.store.task(int(task_id))
                task_id = task["id"]
                self.store.touch(task)
            self.store.message("user", text, task_id)
            self.store.event("message", task_id, text)
            result = dict(received=True)
        elif op == "pause":
            self.data["paused"] = bool(req["value"])
            result = dict(paused=self.data["paused"])
        elif op == "replan":
            self.data["error"] = None
            self.pm_failures = 0
            self.store.event("reconsider", text="Reconsider current work and pending requests")
            result = dict(queued=True)
        elif op == "approve_pm":
            if "pm" in self.active:
                raise ValueError("The PM is still running")
            runs = [r for r in self.data["runs"] if r["kind"] == "pm"]
            if not runs or runs[-1]["status"] != "approval":
                raise ValueError("The latest PM run is not waiting for command approval")
            self.pm_recovery = copy.deepcopy(runs[-1])
            self.data["error"] = None
            self.store.message("user", "Approved one PM Mu retry with traps off.")
            result = dict(queued=True)
        elif op == "priority":
            task = self.store.task(int(req["task_id"]))
            task["priority"] = int(req["priority"])
            self.store.touch(task)
            self.store.event("priority", task["id"])
            result = dict(updated=True)
        elif op in ("resume", "approve"):
            task = self.store.task(int(req["task_id"]))
            if task["state"] == "running":
                raise ValueError("Task is already running")
            if not task["session"]:
                raise ValueError("No worker session to resume; reply to the task instead")
            if op == "approve" and task["gate"] != "approval":
                raise ValueError("This task is not waiting for command approval")
            if task["gate"] == "approval" and op != "approve":
                raise ValueError("Inspect the trapped command and explicitly approve, or cancel")
            status = self._session_status(task["session"])
            if (status.get("active") or {}).get("busy"):
                raise ValueError("The Mu session is still busy")
            task.update(state="queued", gate=None, question="",
                        mode="approve" if op == "approve" else ("prompt" if status.get("clean") else "retry"))
            task["turns"] = 0
            self.store.touch(task)
            self.store.message("user", "Approved one Mu retry with traps off." if op == "approve" else "Resume this task.", task["id"])
            result = dict(queued=True)
        elif op in ("cancel", "stop"):
            task = self.store.task(int(req["task_id"]))
            worker = self.active.get("worker")
            if worker and worker["record"]["task_id"] == task["id"]:
                self._stop(worker, "cancelled" if op == "cancel" else "interrupted")
            elif op == "stop":
                raise ValueError("This task is not running")
            else:
                task.update(state="cancelled", question="", gate=None)
                self.store.touch(task)
                self._release_cancelled(task)
                self.store.event("cancelled", task["id"])
            result = dict(stopping=True)
        elif op == "ack_workspace":
            if "worker" in self.active:
                raise ValueError("Cannot release the checkout while a worker is running")
            self.data["workspace_block"] = None
            self.data["hold"] = None
            self.store.message("user", "Accepted the current checkout as a safe baseline for other tasks.")
            self.store.event("baseline_accepted")
            result = dict(accepted=True)
        elif op == "shutdown":
            mode = req["mode"]
            if mode not in ("stop", "finish"):
                raise ValueError("Shutdown mode must be stop or finish")
            self.stopping = mode
            self.finish_task = self.active.get("worker", {}).get("record", {}).get("task_id")
            if self.finish_task is None:
                self.finish_task = self.data["hold"]
            if mode == "stop":
                for run in self.active.values():
                    self._stop(run, "interrupted")
            result = dict(stopping=mode)
        else:
            raise ValueError(f"Unknown operation: {op}")
        self.store.save()
        return result

    def _agent_peer(self, pid):
        agents = {r["record"]["pid"] for r in self.active.values()}
        while pid and pid > 1:
            if pid in agents:
                return True
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                return False
            if int(fields[3]) in agents:
                return True
            pid = int(fields[1])
        return False

    def _release_cancelled(self, task):
        if self.data["hold"] == task["id"]:
            if self.workspace():
                self.data["workspace_block"] = f"Cancelled T{task['id']} left changes. Inspect them before accepting a new baseline (b)."
            else:
                self.data["hold"] = None

    def _spawn(self, kind, prompt, task=None, recovery=None):
        session = recovery["session"] if recovery else (task["session"] if task else None)
        if not session:
            created = self._mu(["new"])
            if created.returncode:
                raise RuntimeError(created.stdout.strip() or created.stderr.strip())
            session = created.stdout.strip()
            if task:
                task["session"] = session
        run_id = uuid.uuid4().hex[:12]
        log_path = self.store.directory / "runs" / f"{run_id}.log"
        prompt_path = self.store.directory / "runs" / f"{run_id}.prompt"
        prompt_path.write_text(prompt)
        record = dict(id=run_id, kind=kind, task_id=task["id"] if task else None,
                      session=session, status="starting", log_path=str(log_path),
                      prompt_path=str(prompt_path), created=now(), finished=None,
                      pid=None, stamp=None, exit_code=None)
        self.data["runs"].append(record)
        if task:
            task.update(state="running", question="", gate=None, turns=task["turns"] + 1)
            self.data["hold"] = task["id"]
            self.store.touch(task)
        active = dict(record=record, started=time.monotonic(), stop=None, plan=None,
                      revisions={t["id"]: t["revision"] for t in self.data["tasks"]},
                      watermark=max((e["id"] for e in self.store.pending()), default=0))
        if recovery:
            active.update(revisions={int(k): v for k, v in recovery["revisions"].items()},
                          watermark=recovery["watermark"], plan=recovery.get("plan"))
        record.update(revisions=active["revisions"], watermark=active["watermark"])
        env = os.environ.copy()
        env.update(NO_COLOR="1", MUB_PROJECT=str(self.root))
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH")]))
        if kind == "pm":
            active["token"] = uuid.uuid4().hex
            env["MUB_PM_TOKEN"] = active["token"]
        else:
            env.pop("MUB_PM_TOKEN", None)
        if self.server:
            env["MUB_SOCKET"] = str(self.server.path)
        model = self.pm_model if kind == "pm" else self.worker_model
        record["model"] = model
        mode = "approve" if recovery else (task["mode"] if task else "prompt")
        args = [self.mu]
        if mode in ("retry", "approve"):
            args += ["retry", "-s", session, "-o", "concise"]
            if mode == "approve":
                args += ["--trap", "off"]
        else:
            args += ["-s", session, "-o", "concise"]
        if model:
            args += ["-m", model]
        if task:
            task["mode"] = "prompt"
        self.store.save()
        try:
            with log_path.open("wb") as output, prompt_path.open("rb") as source:
                process = subprocess.Popen(args, cwd=self.root, env=env,
                                           stdin=source if mode == "prompt" else subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            record.update(status="failed", finished=now())
            if task:
                task.update(state="failed", gate="error", question=str(error))
            self.store.save()
            raise
        record.update(status="running", pid=process.pid, stamp=process_stamp(process.pid))
        active["process"] = process
        self.active[kind] = active
        self.store.save()

    def _pm_prompt(self):
        state = dict(tasks=self.data["tasks"], decisions=self.data["decisions"],
                     messages=self.data["messages"][-60:], events=self.store.pending(),
                     workspace_owner=self.data["hold"], workspace_status=self.workspace())
        return f"""You are the project manager for {self.root}. Triage requests, clarify consequential ambiguities, and arrange a serial development queue. Discuss designs without turning discussion into unauthorized implementation. Read code when useful; workers do the implementation. Current worker edits are provisional.

Submit one plan with `{self.client_command} plan` using JSON on stdin, then give a short reply. The board applies the plan when you finish. Use `{self.client_command} show ID` for full task history. New events arriving during your turn will get another PM turn.

Plan shape:
{{"reply":"optional project reply", "decisions":["durable agreed decision"], "tasks":[{{"id":1,"state":"queued","brief":"implementation and acceptance criteria","depends_on":[],"priority":0}}]}}
All fields except task id are optional. States: queued, needs_input, done, cancelled. Use question for clarification, result for an outcome, and title to rename. For new subtasks use a string id (e.g. "api"); dependencies can refer to those ids in this plan. Higher priority runs first. Dependencies require done, not cancelled. A queued task with a session continues that session. Ask only questions that materially affect the work; otherwise choose a reasonable approach. Leave tasks with no change out of the plan. Cancelled tasks stay cancelled unless the user explicitly reopens them.

Assess worker results before marking done. Done accepts its checkout changes as the next task's baseline; if work is incomplete, queue a continuation or ask a question. Do not change running tasks or tasks with a gate (those require user recovery/approval). No direct Mu launches: the board starts all workers. An empty tasks list is fine for discussion.

Current board:
{json.dumps(state, ensure_ascii=False)}
"""

    def _worker_prompt(self, task):
        history = [m for m in self.data["messages"] if m["task_id"] == task["id"]][-20:]
        return f"""Work on T{task['id']}: {task['title']} in {self.root}.

Original request:
{task['request']}

Current brief:
{task['brief'] or task['request']}

Project decisions:
{json.dumps(self.data['decisions'], ensure_ascii=False)}

Task discussion:
{json.dumps(history, ensure_ascii=False)}

You own the checkout for this turn. Follow the project's conventions, implement this task, and run relevant checks. Keep unrelated work out; do not launch other editing agents. Finish with what changed, checks run, and any remaining blocker. If a decision is needed, ask rather than inventing a requirement. The board handles follow-ups after you exit.
"""

    def _validate_plan(self, plan, active):
        if not isinstance(plan, dict) or set(plan) - {"reply", "decisions", "tasks"}:
            raise ValueError("Plan fields: reply, decisions, tasks")
        if not isinstance(plan.get("reply", ""), str):
            raise ValueError("reply must be text")
        if not isinstance(plan.get("decisions", []), list) or any(not isinstance(d, str) for d in plan.get("decisions", [])):
            raise ValueError("decisions must be a list of strings")
        patches = plan.get("tasks", [])
        if not isinstance(patches, list):
            raise ValueError("tasks must be a list")
        current = {t["id"]: copy.deepcopy(t) for t in self.data["tasks"]}
        aliases = {}
        seen = set()
        next_id = max(current, default=0) + 1
        for patch in patches:
            if not isinstance(patch, dict) or "id" not in patch:
                raise ValueError("Every task patch needs an id")
            if set(patch) - {"id", "title", "brief", "state", "depends_on", "priority", "question", "result"}:
                raise ValueError("Unsupported task field")
            key = patch["id"]
            if not isinstance(key, (str, int)) or isinstance(key, bool) or key in seen:
                raise ValueError("Task ids must be distinct numbers or new string labels")
            seen.add(key)
            if isinstance(key, str):
                if not isinstance(patch.get("brief"), str) or not patch["brief"].strip() or not isinstance(patch.get("title"), str) or not patch["title"].strip():
                    raise ValueError("New tasks need a title and brief")
                aliases[key] = next_id
                current[next_id] = dict(id=next_id, state="inbox", depends_on=[])
                next_id += 1
            else:
                task = self.store.task(key)
                if active["revisions"].get(key) != task["revision"]:
                    raise ValueError(f"T{key} changed during this PM turn; refresh required")
                if task["state"] == "running" or task["gate"]:
                    raise ValueError(f"T{key} is running or requires explicit user recovery")
                if task["state"] == "cancelled" and patch.get("state", "cancelled") != "cancelled":
                    raise ValueError(f"T{key} was cancelled by the user; only an explicit resume can reopen it")
        for patch in patches:
            key = aliases.get(patch["id"], patch["id"])
            normalized = dict(patch, id=key)
            if "state" in patch and patch["state"] not in ("queued", "needs_input", "done", "cancelled"):
                raise ValueError("PM states: queued, needs_input, done, cancelled")
            for field in ("title", "brief", "question", "result"):
                if field in patch and not isinstance(patch[field], str):
                    raise ValueError(f"{field} must be text")
            if "priority" in patch and type(patch["priority"]) is not int:
                raise ValueError("priority must be an integer")
            if "depends_on" in patch:
                if not isinstance(patch["depends_on"], list):
                    raise ValueError("depends_on must be a list")
                normalized["depends_on"] = [aliases.get(d, d) for d in patch["depends_on"]]
                if any(type(d) is not int or d not in current or d == key for d in normalized["depends_on"]):
                    raise ValueError("Dependencies must name other existing tasks or new labels")
                for dependency in normalized["depends_on"]:
                    if dependency in active["revisions"] and self.store.task(dependency)["revision"] != active["revisions"][dependency]:
                        raise ValueError(f"Dependency T{dependency} changed during this PM turn; refresh required")
            current[key].update(normalized)
            if current[key]["state"] == "needs_input" and not current[key].get("question"):
                raise ValueError("needs_input requires a question")
        visiting, visited = set(), set()

        def visit(key):
            if key in visiting:
                raise ValueError("Dependency cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in current[key]["depends_on"]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in current:
            visit(key)
        return current, aliases

    def _apply_plan(self, plan, active):
        original = self.data
        self.data = self.store.data = copy.deepcopy(original)
        try:
            self._apply_plan_changes(plan, active)
        except BaseException:
            self.data = self.store.data = original
            raise
        finally:
            records = {r["id"]: r for r in self.data["runs"]}
            for running in self.active.values():
                running["record"] = records[running["record"]["id"]]

    def _apply_plan_changes(self, plan, active):
        current, aliases = self._validate_plan(plan, active)
        for patch in plan.get("tasks", []):
            if isinstance(patch["id"], str):
                task = self.store.add_task(patch["brief"], patch["title"])
                key = aliases[patch["id"]]
                self.store.message("pm", patch["brief"], task["id"])
            else:
                key = patch["id"]
                task = self.store.task(key)
            for field in ("title", "brief", "state", "depends_on", "priority", "question", "result"):
                if field in patch:
                    task[field] = current[key][field]
            if task["state"] != "needs_input":
                task["question"] = ""
            self.store.touch(task)
            note = patch.get("question") or patch.get("result") or patch.get("brief")
            if note:
                self.store.message("pm", note, task["id"])
            if self.data["hold"] == key and task["state"] == "done":
                self.data["hold"] = None
            if task["state"] == "cancelled":
                self._release_cancelled(task)
        for decision in plan.get("decisions", []):
            self.data["decisions"].append(dict(id=len(self.data["decisions"]) + 1, content=decision, created=now()))
        if plan.get("reply"):
            self.store.message("pm", plan["reply"])
        for event in self.data["events"]:
            if event["id"] <= active["watermark"]:
                event["handled"] = True

    def _stop(self, active, reason):
        if active["stop"]:
            return
        active["stop"] = reason
        active["record"]["stop_reason"] = reason
        active["stop_time"] = time.monotonic()
        self.store.save()
        process = active["process"]
        # Mu forwards SIGINT to its active Bash process groups.
        if process.poll() is None:
            signal_process(process.pid, active["record"]["stamp"], signal.SIGINT)
        else:
            for pid, stamp in owned_members(active["record"]):
                signal_process(pid, stamp, signal.SIGINT)

    def _finish(self, kind, active):
        run = active["record"]
        code = active["process"].returncode
        run.update(exit_code=code, finished=now(), status="finished")
        status = self._session_status(run["session"])
        clean = status.get("clean", False)
        transcript = self._mu(["transcript", "-s", run["session"], "-o", "final"])
        output = transcript.stdout.strip() if transcript.returncode == 0 else ""
        if not output or code != 0:
            output = tail(run["log_path"], 20000)
        # Full conversation is retained by Mu; board keeps the latest result.
        output = output[-24000:]
        run["result"] = output
        if kind == "worker":
            task = self.store.task(run["task_id"])
            task["result"] = output
            if active["stop"]:
                task.update(state="cancelled" if active["stop"] == "cancelled" else "needs_input",
                            gate=None if active["stop"] == "cancelled" else "interrupted",
                            question="Worker stopped. Inspect its changes before resuming.")
                run["status"] = active["stop"]
                if task["state"] == "cancelled":
                    self._release_cancelled(task)
            elif code == 3:
                task.update(state="needs_input", gate="approval", question="Mu trapped a command. Inspect Execution before approving a retry with traps off.")
                run["status"] = "approval"
            elif code or not clean:
                task.update(state="failed" if code else "needs_input", gate="error" if code else "interrupted",
                            question="Mu did not finish cleanly. Inspect its output and resume explicitly.")
                run["status"] = "failed" if code else "interrupted"
            else:
                task.update(state="review", gate=None)
            self.store.touch(task)
            self.store.message("worker", output or "(No final response)", task["id"])
            self.store.event("worker_finished", task["id"], f"Run {run['id']}: {run['status']}, exit={code}")
        else:
            if self.server:
                self.server.drain(self.request)
            if active["stop"]:
                run["status"] = "interrupted"
                self.data["error"] = "PM stopped. Pending events were preserved; press r to retry."
            elif code == 3:
                run["status"] = "approval"
                self.data["error"] = "PM command needs approval. Open m → 3 to inspect, then a to approve; r starts a fresh PM instead."
            elif code or not clean or active["plan"] is None:
                self._pm_error(run, f"PM did not submit a clean plan (exit {code}). See its execution log; press r to retry.")
            else:
                try:
                    self._apply_plan(active["plan"], active)
                    self.pm_failures = 0
                    self.data["error"] = None
                except ValueError as error:
                    self._pm_error(run, str(error))
        self.store.save()

    def _pm_error(self, run, message):
        run["status"] = "failed"
        self.pm_failures += 1
        self.store.message("system", message)
        self.store.event("plan_failed", text=message)
        self.next_pm = time.monotonic() + 1
        if self.pm_failures >= 2:
            self.data["error"] = message + " Automatic PM retries paused; press r."

    def _ready(self):
        tasks = {t["id"]: t for t in self.data["tasks"]}
        ready = [t for t in tasks.values() if t["state"] == "queued" and not t["gate"]
                 and all(tasks[d]["state"] == "done" for d in t["depends_on"])
                 and (self.data["hold"] is None or self.data["hold"] == t["id"])
                 and (not self.stopping or t["id"] == self.finish_task)]
        return sorted(ready, key=lambda t: (-t["priority"], t["id"]))

    def tick(self):
        if self.server:
            self.server.drain(self.request)
        for kind, active in list(self.active.items()):
            if not active["stop"] and time.monotonic() - active["started"] > self.timeout:
                self._stop(active, "interrupted")
            if active["stop"] and time.monotonic() - active.get("stop_time", 0) > 5:
                for pid, stamp in owned_members(active["record"]):
                    signal_process(pid, stamp, signal.SIGKILL)
            if active["process"].poll() is not None:
                if owned_members(active["record"]):
                    self._stop(active, "interrupted")
                    continue
                del self.active[kind]
                try:
                    self._finish(kind, active)
                except Exception as error:
                    # Never dispatch another writer after an uncertain completion.
                    self.data["error"] = f"Cannot assess run {active['record']['id']}: {error}"
                    self.data["paused"] = True
                    active["record"]["status"] = "interrupted"
                    if kind == "worker":
                        task = self.store.task(active["record"]["task_id"])
                        task.update(state="needs_input", gate="interrupted", question=self.data["error"])
                        self.store.touch(task)
                    self.store.save()
        if self.stopping == "stop":
            self.done = not self.active
            return
        if self.stopping == "finish" and not self.active:
            task = self.store.task(self.finish_task) if self.finish_task else None
            if not task or task["state"] not in ("queued", "running", "review"):
                self.done = True
                return
            if self.data["error"] or self.data["workspace_block"] or (task["state"] == "queued" and not self._ready()):
                self.done = True
                return
        if self.pm_recovery and "pm" not in self.active:
            recovery = self.pm_recovery
            self.pm_recovery = None
            try:
                self._spawn("pm", Path(recovery["prompt_path"]).read_text(), recovery=recovery)
            except Exception as error:
                self.data["error"] = f"Cannot retry PM: {error}"
                self.store.save()
            return
        if "pm" not in self.active and self.store.pending() and not self.data["error"] and time.monotonic() >= self.next_pm:
            try:
                self._spawn("pm", self._pm_prompt())
            except Exception as error:
                self.data["error"] = f"Cannot start PM: {error}"
                self.store.save()
            return
        if self.active or self.store.pending() or self.data["error"] or self.data["workspace_block"]:
            return
        if self.server:
            self.server.drain(self.request)
            if self.store.pending() or self.stopping == "stop":
                return
        if self.data["paused"] and not self.stopping:
            return
        ready = self._ready()
        if ready:
            task = ready[0]
            if task["turns"] >= self.max_turns:
                task.update(state="needs_input", gate="limit", question=f"Reached {self.max_turns} worker turns. Inspect and resume to grant another batch.")
                self.store.touch(task)
                self.store.save()
            else:
                try:
                    self._spawn("worker", self._worker_prompt(task), task)
                except Exception as error:
                    self.data["error"] = f"Cannot start worker: {error}"
                    self.store.save()

    def idle(self):
        if self.active:
            return False
        if self.data["error"]:
            return True
        if self.store.pending():
            return False
        return bool(self.data["paused"] or self.data["workspace_block"] or not self._ready())

    def close(self):
        if self.closed:
            return
        self.closed = True
        for active in self.active.values():
            self._stop(active, "interrupted")
        self.stopping = "stop"
        while self.active:
            self.tick()
            time.sleep(0.05)
        if self.server:
            self.server.close()
        self.store.save()
        self.store.close()
