"""Session mailboxes and a small, event-driven Mu scheduler."""

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .state import Store, session_name
from .output import delivery, journal_path, live_prompt, plain_output, prompt_bytes, replay
from .terminal import Capture, Screen, replay_screen, terminal_env


def process_stamp(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
    except FileNotFoundError:
        return None


def owned_members(record):
    stamp = record.get("stamp")
    if not stamp or not stamp.startswith(Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":"):
        return []
    leader = process_stamp(record["pid"])
    if leader and leader != stamp:
        return []
    members = []
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z" and int(fields[3]) == record["pid"]:
                pid = int(path.parent.name)
                if birth := process_stamp(pid):
                    members.append((pid, birth))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return members


def signal_process(pid, stamp, signum):
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if process_stamp(pid) == stamp:
            signal.pidfd_send_signal(fd, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


class Engine:
    def __init__(self, root, *, mu="mu", scheduler_model=None, worker_model=None):
        self.store = Store(root)
        self.root, self.data = self.store.root, self.store.data
        self.mu = mu
        self.models = dict(self.data["models"])
        for role, model in (("scheduler", scheduler_model), ("worker", worker_model)):
            if model is not None:
                self.models[role] = model
        self.active = {}
        self.server = None
        self.done = self.stopping = self.closed = False
        self.history = {}
        self.screens = {}
        self.replays = {}
        self.replay_stop = threading.Event()
        self.replay_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mub-history")
        self.terminal_size = (80, 24)
        self.model_cache = {}
        self.traps = {}
        self.client = shlex.join([sys.executable, "-m", "muboard", "-C", str(self.root)])
        try:
            self._recover()
            self._workspace()
            self.store.save()
        except BaseException:
            self.store.close()
            raise

    def _mu(self, *args):
        result = subprocess.run([self.mu, *args], cwd=self.root, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Mu command failed")
        return result.stdout.strip()

    def _session_status(self, session, model=None):
        return json.loads(self._mu("status", "-s", session, "--json", "--include-session-details",
                                  *(["--model", model] if model else [])))

    def _recover(self):
        for run in self.data["inflight"]:
            if owned_members(run):
                raise RuntimeError(f"Previous invocation {run['id']} still has live processes (PID {run['pid']}); stop them before reopening")
            if run["session"] and self._session_status(run["session"]).get("active", {}).get("busy"):
                raise RuntimeError(f"Mu session {run['session']} is still busy")
        for run in self.data["inflight"]:
            if run["kind"] == "scheduler":
                self.data["scheduler"]["error"] = "Scheduler interrupted. Use /schedule to start a fresh scheduling session."
            else:
                session = self.store.session(run["session_id"])
                session.update(hold=True, gate="interrupted", reason="Previous owner stopped; inspect the session before continuing.",
                               retry_authorized=False, revision=session["revision"] + 1,
                               last=dict(exit="interrupted", action=run["origin"], mode=run["mode"],
                                         message_id=run.get("message_id"), journal_offset=run["journal_offset"],
                                         clean=False, summary="Owner stopped during invocation."))
                for message in self.data["messages"]:
                    if message["id"] == run.get("message_id"):
                        message["state"] = "interrupted"
                self.store.event("interrupted", session["id"])
        self.data["inflight"] = []

    def _workspace(self):
        result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=self.root, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Cannot inspect Git workspace")
        dirty = result.stdout.strip()
        writer = next((a["record"]["session_id"] for a in self.active.values()
                       if a["record"]["kind"] == "worker" and a["record"]["mode"] == "readwrite"), None)
        if not dirty and writer is None:
            self.data["owner"] = None
        self.workspace = dict(clean=not dirty, status=dirty, owner=self.data["owner"])
        return self.workspace

    def _running(self, session_id):
        return next((a for a in self.active.values() if a["record"]["session_id"] == session_id), None)

    def state(self):
        sessions = [dict(s, next_model=self._next_model(s),
                         active=(a["record"] if (a := self._running(s["id"])) else None))
                    for s in self.data["sessions"]]
        scheduler = self._running(None)
        return dict(root=str(self.root), sessions=sessions, messages=self.data["messages"],
                    scheduler=dict(self.data["scheduler"], active=scheduler["record"] if scheduler else None),
                    workspace=self.workspace, models=self.models, stopping=self.stopping)

    def _next_model(self, session):
        override = session.get("model") or self.models["worker"]
        if override:
            return override
        key = session["session"]
        if key not in self.model_cache:
            try:
                status = (self._session_status(session["session"]) if session["session"] else
                          json.loads(self._mu("status", "--json")))
                self.model_cache[key] = status.get("model", {}).get("canonical") or "Mu/session default"
            except (RuntimeError, ValueError):
                self.model_cache[key] = "Mu/session default (unavailable)"
        return self.model_cache[key]

    @staticmethod
    def _model_reference(value):
        if value is not None and (not isinstance(value, str) or not value.strip() or not value.isprintable()):
            raise ValueError("Model must be a reference or null for default")
        return value.strip() if value is not None else None

    def _agent_peer(self, pid):
        agents = {a["record"].get("pid") for a in self.active.values()}
        while pid and pid > 1:
            if pid in agents:
                return True
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                return False
            if int(fields[3]) in agents:
                return True
            pid = int(fields[1])
        return False

    def request(self, req):
        op = req.get("op")
        if op not in ("status", "output", "models") and self._agent_peer(req.get("_peer_pid")):
            raise ValueError("This control requires the user; agents may only inspect state")
        if op == "status":
            return self.state()
        if op == "output":
            return self.output(req.get("session_id"))
        if op == "models":
            status = json.loads(self._mu("status", "--json", "--include-models"))
            return dict(available=[m for p in status["available_models"]["providers"] for m in p["models"]],
                        selected=self.models)
        if self.stopping and op != "shutdown":
            raise ValueError("mub is shutting down")
        if op == "new":
            if "text" in req and not req["text"].strip():
                raise ValueError("Message cannot be empty")
            model = self._model_reference(req.get("model"))
            session = self.store.new_session(req.get("name"))
            session["model"] = model
            if req.get("text"):
                self.store.submit(session["id"], req["text"])
            result = dict(session_id=session["id"])
        elif op == "send":
            message = self.store.submit(int(req["session_id"]), req["text"])
            result = dict(message_id=message["id"])
        elif op == "rename":
            session = self.store.session(int(req["session_id"]))
            name = req.get("name")
            if name is None:
                session["name_source"] = "auto"
            else:
                session.update(name=session_name(name), name_source="user")
            result = dict(name=session["name"], name_source=session["name_source"])
        elif op == "interrupt":
            session = self.store.session(int(req["session_id"]))
            session.update(hold=True, retry_authorized=False, revision=session["revision"] + 1)
            self.store.event("held", session["id"])
            self.store.save()
            if active := self._running(session["id"]):
                self._stop(active)
            result = dict(held=True)
        elif op == "resume":
            session = self.store.session(int(req["session_id"]))
            if self._running(session["id"]):
                raise ValueError("Wait for the session to stop before continuing")
            clean = not session["session"] or self._session_status(session["session"])["clean"]
            last = session["last"]
            delivered = delivery(self.root, session["session"], last["journal_offset"], clean) if last else "undelivered"
            retry = not clean or delivered == "interrupted"
            for message in list(self.data["messages"]):
                if last and message["id"] == last.get("message_id"):
                    if delivered == "complete":
                        self.data["messages"].remove(message)
                    elif delivered == "undelivered" and clean:
                        message["state"] = "pending"
            session.update(hold=False, gate="interrupted" if retry else None, blocked=None,
                           reason="User authorized continuation of the interrupted turn." if retry else "",
                           retry_authorized=retry, revision=session["revision"] + 1)
            self.store.event("resumed", session["id"])
            result = dict(eligible=True, retry=retry)
        elif op == "remove":
            session = self.store.session(int(req["session_id"]))
            self._workspace()
            if self._running(session["id"]) or self.data["owner"] == session["id"]:
                raise ValueError("Stop the session and resolve its workspace changes before removing it")
            pending = [m for m in self.data["messages"] if m["session_id"] == session["id"]]
            if pending and not req.get("discard"):
                raise ValueError("Session has queued/interrupted messages; confirm discarding them")
            self.data["sessions"].remove(session)
            self.screens.pop(session["id"], None)
            pending_replay = self.replays.pop(session["id"], None)
            if pending_replay:
                pending_replay[1].cancel()
            self.data["messages"] = [m for m in self.data["messages"] if m["session_id"] != session["id"]]
            self.store.event("removed", session["id"])
            result = dict(removed=True)
        elif op == "set_session_model":
            session = self.store.session(int(req["session_id"]))
            session["model"] = self._model_reference(req["model"])
            result = dict(session_id=session["id"], model=session["model"], next_model=self._next_model(session))
        elif op == "set_models":
            models = req["models"]
            if not isinstance(models, dict) or not models or set(models) - {"scheduler", "worker"}:
                raise ValueError("Choose scheduler and/or worker models")
            if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in models.values()):
                raise ValueError("Model must be a reference or null for Mu default")
            self.models.update(models)
            self.data["models"].update(models)
            result = dict(models=self.models)
        elif op == "schedule":
            if self._running(None):
                raise ValueError("Scheduler is already running")
            if self.data["scheduler"]["error"]:
                self.data["scheduler"]["session"] = None
            self.data["scheduler"]["error"] = None
            self._workspace()
            self.store.event("recheck")
            result = dict(scheduled=True)
        elif op == "shutdown":
            if self.active and not req.get("confirmed"):
                raise ValueError("Running agents: confirm stopping them before quitting")
            self.stopping = True
            self.replay_stop.set()
            for active in list(self.active.values()):
                if active["record"]["kind"] == "worker":
                    session = self.store.session(active["record"]["session_id"])
                    session.update(hold=True, retry_authorized=False, revision=session["revision"] + 1)
                self._stop(active)
            self.done = not self.active
            result = dict(stopping=True)
        else:
            raise ValueError(f"Unknown operation: {op}")
        self.store.save()
        return result

    def output(self, session_id):
        target = self.data["scheduler"] if session_id is None else self.store.session(int(session_id))
        active = self._running(session_id)
        if active:
            stream = active["output"]
            text = plain_output(os.pread(stream.fileno(), os.fstat(stream.fileno()).st_size, 0))
            return dict(text=active["history"] + "\n── Live invocation ──\n" + text,
                        source="Mu history + live output")
        if not target["session"]:
            return dict(text="", source="No Mu turn yet")
        key = target["session"]
        if key not in self.history:
            self.history[key] = replay(self.root, key, self.mu)
        return dict(text=self.history[key], source="Mu session journal")

    def display(self, session_id, cols, rows):
        """Local UI API: return cells, never route worker controls through IPC."""
        target = self.data["scheduler"] if session_id is None else self.store.session(session_id)
        if not target["session"]:
            return None
        screen = self.screens.get(session_id)
        if self._running(session_id):
            return screen
        # Keep failed/interrupted live output visible, including transient errors
        # absent from Mu's journal. It remains horizontally scrollable on resize.
        failed = target.get("error") if session_id is None else (target.get("last") or {}).get("exit") not in (None, "clean")
        if screen and (failed or (screen.cols, screen.rows) == (cols, rows)):
            return screen
        key = (target["session"], cols, rows)
        pending = self.replays.get(session_id)
        if pending and pending[1].done():
            if pending[0] == key:
                result = pending[1].result()
                self.screens[session_id] = result
                del self.replays[session_id]
                return result
            del self.replays[session_id]
            pending = None
        if pending is None:
            self.replays[session_id] = (key, self.replay_pool.submit(replay_screen, self.root, *key, self.mu, self.replay_stop))
        return screen

    def _scheduler_prompt(self, snapshot):
        return f"""You schedule messages for Mu sessions sharing {self.root}. You do NOT manage implementation quality, review code, invent tasks, or fix failures. Do not edit project files, launch agents, or call user controls. The runtime owns processes and delivery. Interpret dependencies from messages and worker responses; preserve FIFO within sessions, prefer global submission order unless priorities/prerequisites justify another choice. Readers see a live, possibly changing checkout.

Choose any number of independent readonly messages and at most one readwrite invocation. Messages can only go to idle, unheld sessions with a clean Mu turn. A writer needs a clean checkout or its own dirty checkout. A failed/held dirty owner blocks other writers, not independent readers. Do not run dependents merely because a prerequisite exited or failed. Mark blocked sessions with a short reason; reconsider them when circumstances change. A worker's own response and exit reason are sufficient evidence; do not review its implementation.

You may resolve traps by inspecting their FULL command and stdin in the outcome (or `{self.client} logs S<ID>` if needed) and retrying with a suitable policy, within the user's authorized scope. Readonly uses trap reversible; a write requires promotion to the writer slot. Relaxing traps authorizes the REMAINDER OF THE TURN, not one command. Use destructive for ordinary reversible writes, off only when the broader permission is justified. Retry does not accept new instructions. Never retry failures/interrupted user-stopped sessions unless retry_authorized is true. Do not troubleshoot provider or Mu bugs. Label genuine failures and withhold dependent work.

You may request a commit from the dirty workspace owner to release the checkout. This is only a handoff request to commit task-owned completed changes or explain why it cannot; not a repair/implementation request. Do not repeat a commit request after an unsuccessful handoff without new user input. User holds prohibit execution actions, including commit and retry.

For sessions with name_source "auto", optionally propose names based on messages and outcomes. Choose a short topic-based title (at most 80 characters) once there is enough context; rename when the main topic materially changes, not on every turn. Avoid execution status such as Running or Done. Never rename name_source "user" sessions. Names are display metadata, independent of actions: you may name active or held sessions and name and dispatch the same session. Omit names or use an empty list when no change is useful.

Return ONLY a JSON decision as your final answer, without Markdown fences or surrounding prose. It applies after your clean exit. Shape:
{{"reason":"short scheduling explanation", "names":[{{"session_id":1, "name":"API pagination"}}], "actions":[
  {{"type":"dispatch", "message_id":12, "mode":"readonly"}},
  {{"type":"retry", "session_id":2, "mode":"readwrite", "trap":"destructive", "reason":"why authorized"}},
  {{"type":"commit", "session_id":3, "reason":"handoff needed"}},
  {{"type":"label", "session_id":4, "status":"blocked", "reason":"waiting for S2"}}
]}}
Examples show available actions, not a required batch. Label status: blocked, failed, clear. One action per session per decision. dispatch may optionally specify trap (readonly always reversible; readwrite defaults destructive). An empty actions list means wait. You cannot create sessions, rewrite user messages, reorder a session's mailbox, or release user holds. Only consider the snapshot below; later arrivals wait for another pass.

SNAPSHOT:
{json.dumps(snapshot, ensure_ascii=False)}
"""

    def _spawn(self, *, session=None, message=None, action="schedule", mode="readonly", trap=None, snapshot=None):
        kind = "worker" if session else "scheduler"
        target = session if session else self.data["scheduler"]
        if not target["session"]:
            target["session"] = self._mu("new")
            self.store.save()
        mu_session = target["session"]
        model = target.get("model") or self.models[kind]
        status = self._session_status(mu_session, model)
        if status.get("active", {}).get("busy"):
            raise RuntimeError(f"Mu session {mu_session} is already busy")
        if action != "retry" and not status["clean"]:
            raise RuntimeError(f"Mu session {mu_session} has an interrupted turn; use explicit continuation")
        history = replay(self.root, mu_session, self.mu)
        # Complete replay BEFORE releasing the worker: replaying its growing
        # journal concurrently would duplicate new output at the history boundary.
        key = session["id"] if session else None
        previous = self.screens.get(key)
        preparation = None
        pending = self.replays.pop(key, None)
        if pending:
            pending[1].cancel()
        if (previous and previous.finished and not previous.error and previous.session == mu_session
                and not previous.core.is_alt_screen_active()
                and (previous.cols, previous.rows) == self.terminal_size):
            screen = previous
        else:
            screen = Screen(*self.terminal_size)
            screen.session = mu_session
            if history:
                preparation = self.replay_pool.submit(replay_screen, self.root, mu_session,
                                                      *self.terminal_size, self.mu, self.replay_stop)
        self.history.pop(mu_session, None)
        if kind == "scheduler":
            prompt = self._scheduler_prompt(snapshot)
        elif action == "commit":
            prompt = """<system-request>
Commit only this session's completed, task-owned changes so another writer can proceed. Do not blindly stage everything, commit incomplete work, or implement additional work. If a safe handoff is not possible, explain why and return.
</system-request>"""
        elif action == "retry":
            prompt = ""
        else:
            prompt = message["text"]
        prefix = b"\x1b[0m\r\n"
        if action != "retry":
            prefix += prompt_bytes(live_prompt(prompt, self.root, status))
        screen.feed(prefix)
        screen.finished = False
        self.screens[key] = screen
        record = dict(id=uuid.uuid4().hex[:12], kind=kind, session_id=session["id"] if session else None,
                      session=mu_session, action=action, mode=mode, trap=trap or ("reversible" if mode == "readonly" else "destructive"),
                      origin=session["last"]["action"] if action == "retry" and session["last"] else action,
                      journal_offset=session["last"]["journal_offset"] if action == "retry" and session["last"] else journal_path(self.root, mu_session).stat().st_size,
                      model=model or status.get("model", {}).get("canonical"), message_id=message["id"] if message else
                      (session["last"].get("message_id") if action == "retry" and session["last"] else None),
                      pid=None, stamp=None, stopping=False)
        if session:
            session.update(revision=session["revision"] + 1, retry_authorized=False, blocked=None)
            if mode == "readwrite":
                self.data["owner"] = session["id"]
            for item in self.data["messages"]:
                if item["id"] == record["message_id"]:
                    item["state"] = "inflight"
        self.data["inflight"].append(record)
        self.store.save()
        try:
            capture = Capture(screen) if kind == "worker" else None
            output = capture.raw if capture else tempfile.TemporaryFile()
        except OSError:
            if preparation:
                preparation.cancel()
            self.data["inflight"].remove(record)
            if message:
                message["state"] = "pending"
            self.store.save()
            raise
        active = dict(record=record, output=output, capture=capture, screen=screen, screen_offset=0,
                      history=history, snapshot=snapshot, plan=None,
                      stopped_at=None, preparation=preparation, preparation_error=None, prefix=prefix, release=None)
        env = dict(terminal_env() if capture else os.environ, MUB_PROJECT=str(self.root), MUB_ROLE=kind)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH")]))
        if self.server:
            env["MUB_SOCKET"] = str(self.server.path)
        args = [self.mu, *(["retry"] if action == "retry" else []), "-s", mu_session,
                "-o", "final" if kind == "scheduler" else "concise", "--trap", record["trap"]]
        if model:
            args += ["-m", model]
        ready, release = os.pipe()
        release_owned = False
        # The child cannot execute Mu until its PID is durable. Parent death closes
        # the pipe, so a crash in the launch window cannot create an untracked writer.
        launcher = [sys.executable, "-c",
                    "import os,sys; fd=int(sys.argv[1]); ready=os.read(fd,1); os.close(fd); "
                    "os.execvpe(sys.argv[2],sys.argv[2:],os.environ) if ready else sys.exit(0)",
                    str(ready), *args]
        try:
            try:
                with tempfile.TemporaryFile() as source:
                    source.write(prompt.encode())
                    source.seek(0)
                    process = subprocess.Popen(launcher, cwd=self.root, env=env, pass_fds=(ready,),
                                               stdin=source if action != "retry" else subprocess.DEVNULL,
                                               stdout=capture.slave if capture else output,
                                               stderr=subprocess.STDOUT, start_new_session=True)
            except OSError:
                if preparation:
                    preparation.cancel()
                if capture:
                    capture.finish()
                output.close()
                self.data["inflight"].remove(record)
                if message:
                    message["state"] = "pending"
                self.store.save()
                raise
            record.update(pid=process.pid, stamp=process_stamp(process.pid))
            active["process"] = process
            self.active[record["id"]] = active
            active["release"] = release
            release_owned = True
            if capture:
                capture.start()
            self.store.save()
            if self.stopping:
                self._stop(active)
            elif preparation is None:
                self._release(active)
        finally:
            os.close(ready)
            if not release_owned:
                os.close(release)
        self._workspace()
        return active

    def _release(self, active, execute=True):
        fd, active["release"] = active["release"], None
        if fd is not None:
            try:
                if execute:
                    os.write(fd, b"1")
            finally:
                os.close(fd)

    def _prepare(self, active):
        future = active["preparation"]
        if future is None or not future.done() or active["record"]["stopping"]:
            return
        active["preparation"] = None
        try:
            screen = future.result()
            screen.feed(active["prefix"])
            screen.finished = False
            active["screen"] = screen
            self.screens[active["record"]["session_id"]] = screen
            if active["capture"]:
                active["capture"].screen = screen
            self._release(active)
        except (OSError, RuntimeError, ValueError) as error:
            active["preparation_error"] = str(error)
            active["screen"].error = "Cannot prepare Mu history: " + str(error)
            self._release(active, execute=False)

    def _validate_plan(self, plan, active):
        if not isinstance(plan, dict) or set(plan) - {"reason", "actions", "names"} or not isinstance(plan.get("reason"), str) or not isinstance(plan.get("actions"), list):
            raise ValueError("Plan needs reason and actions")
        snapshot = active["snapshot"]
        sessions = {s["id"]: s for s in snapshot["sessions"]}
        messages = {m["id"]: m for m in snapshot["messages"]}
        names = plan.get("names", [])
        if not isinstance(names, list):
            raise ValueError("Names must be a list")
        named = set()
        for update in names:
            if not isinstance(update, dict) or set(update) != {"session_id", "name"}:
                raise ValueError("Name updates need session_id and name")
            key = update["session_id"]
            if type(key) is not int or key not in sessions or key in named:
                raise ValueError("Use at most one name per snapshot session")
            if len(session_name(update["name"])) > 80:
                raise ValueError("Automatic names must be at most 80 characters")
            named.add(key)
        seen = set()
        for action in plan["actions"]:
            if not isinstance(action, dict):
                raise ValueError("Actions must be objects")
            kind = action.get("type")
            fields = {"dispatch": {"type", "message_id", "mode", "trap"},
                      "retry": {"type", "session_id", "mode", "trap", "reason"},
                      "commit": {"type", "session_id", "reason"},
                      "label": {"type", "session_id", "status", "reason"}}
            if kind not in fields or set(action) - fields[kind]:
                raise ValueError("Unsupported scheduler action")
            if kind == "dispatch":
                message = messages.get(action.get("message_id"))
                if not message or message["state"] != "pending":
                    raise ValueError("Dispatch must reference a pending snapshot message")
                key = message["session_id"]
                head = next(m for m in snapshot["messages"] if m["session_id"] == key)
                if message["id"] != head["id"]:
                    raise ValueError("Messages are FIFO within each session")
            else:
                key = action.get("session_id")
            if key not in sessions or key in seen:
                raise ValueError("Use at most one action per snapshot session")
            seen.add(key)
            if kind in ("dispatch", "retry"):
                mode = action.get("mode")
                trap = action.get("trap", "reversible" if mode == "readonly" else "destructive")
                if mode not in ("readonly", "readwrite") or trap not in ("reversible", "destructive", "off"):
                    raise ValueError("Choose readonly/readwrite and a valid trap policy")
                if mode == "readonly" and trap != "reversible":
                    raise ValueError("Readonly execution must retain reversible traps")
            if kind != "dispatch" and (not isinstance(action.get("reason"), str) or not action["reason"].strip()):
                raise ValueError("Interventions need a reason")
            if kind == "label" and action.get("status") not in ("failed", "blocked", "clear"):
                raise ValueError("Label status must be failed, blocked, or clear")

    def _apply_plan(self, active):
        plan, snapshot = active["plan"], active["snapshot"]
        self._validate_plan(plan, active)
        revisions = {s["id"]: s["revision"] for s in snapshot["sessions"]}
        messages = {m["id"]: m for m in snapshot["messages"]}
        self.data["scheduler"]["reason"] = plan["reason"]
        automatic = {s["id"] for s in snapshot["sessions"] if s["name_source"] == "auto"}
        for update in plan.get("names", []):
            session = next((s for s in self.data["sessions"] if s["id"] == update["session_id"]), None)
            if session and session["id"] in automatic and session["name_source"] == "auto":
                session["name"] = session_name(update["name"])
        skipped = []
        for action in plan["actions"]:
            kind = action["type"]
            key = messages[action["message_id"]]["session_id"] if kind == "dispatch" else action["session_id"]
            session = next((s for s in self.data["sessions"] if s["id"] == key), None)
            if not session or session["revision"] != revisions[key] or self._running(key) or session["hold"]:
                skipped.append(f"S{key}: state changed or held")
                continue
            if kind == "label":
                if action["status"] == "failed":
                    session.update(gate="failed", reason=action["reason"], retry_authorized=False)
                else:
                    session["blocked"] = action["reason"] if action["status"] == "blocked" else None
                session["revision"] += 1
                continue
            mode = action.get("mode", "readwrite")
            self._workspace()
            if mode == "readwrite":
                writer = any(a["record"]["kind"] == "worker" and a["record"]["mode"] == "readwrite" for a in self.active.values())
                if writer or (not self.workspace["clean"] and self.data["owner"] != key):
                    skipped.append(f"S{key}: writer slot or dirty workspace unavailable")
                    continue
            message = None
            if kind == "dispatch":
                message = next((m for m in self.data["messages"] if m["id"] == action["message_id"]), None)
                if session["gate"] or not message or message["state"] != "pending":
                    skipped.append(f"S{key}: session requires continuation or recovery")
                    continue
            elif kind == "retry":
                if session["gate"] != "trapped" and not session["retry_authorized"]:
                    skipped.append(f"S{key}: retry requires user authorization")
                    continue
            elif kind == "commit":
                if session["gate"] or self.data["owner"] != key or self.workspace["clean"]:
                    skipped.append(f"S{key}: commit is not an available handoff")
                    continue
                last = session["last"] or {}
                if last.get("action") == "commit" and not any(e["kind"] == "submitted" and e["session_id"] == key for e in snapshot["events"]):
                    skipped.append(f"S{key}: previous commit request did not release the workspace")
                    continue
            try:
                self._spawn(session=session, message=message, action="message" if kind == "dispatch" else kind,
                            mode=mode, trap=action.get("trap"))
            except (OSError, RuntimeError) as error:
                session.update(gate="failed", reason=str(error), revision=session["revision"] + 1)
                self.store.event("failed", key, detail=str(error))
        if skipped:
            self.data["scheduler"]["reason"] += "\nNot dispatched: " + "; ".join(skipped)
        handled = {e["id"] for e in snapshot["events"]}
        self.data["events"] = [e for e in self.data["events"] if e["id"] not in handled]
        self.store.save()

    def _stop(self, active):
        if active["record"]["stopping"]:
            signum = signal.SIGKILL
        else:
            active["record"]["stopping"] = True
            active["stopped_at"] = time.monotonic()
            self.store.save()
            signum = signal.SIGINT
        for pid, stamp in owned_members(active["record"]):
            signal_process(pid, stamp, signum)

    def _finish(self, active):
        run = active["record"]
        self._release(active, execute=False)
        if active["preparation"]:
            active["preparation"].cancel()
        if active["capture"]:
            active["capture"].finish()
        else:
            self._scheduler_display(active)
        active["screen"].finished = True
        self.model_cache.pop(run["session"], None)
        self.data["inflight"].remove(run)
        self.history.pop(run["session"], None)
        stream = active["output"]
        data = os.pread(stream.fileno(), os.fstat(stream.fileno()).st_size, 0)
        raw = plain_output(data) if active["capture"] else data.decode("utf-8", "replace")
        stream.close()
        code = active["process"].returncode
        try:
            clean = self._session_status(run["session"])["clean"]
        except (RuntimeError, OSError, ValueError) as error:
            clean = False
            raw += "\nCannot inspect Mu session: " + str(error)
        exit_reason = "interrupted" if run["stopping"] else "trapped" if code == 3 else "failed" if code else "clean" if clean else "interrupted"
        if active["preparation_error"]:
            exit_reason = "failed"
            raw += "\nCannot prepare Mu history: " + active["preparation_error"]
        if active["capture"] and active["capture"].error:
            exit_reason = "failed"
            raw += "\nPTY capture failed: " + active["capture"].error
        if run["kind"] == "scheduler":
            if exit_reason != "clean":
                self.data["scheduler"]["error"] = f"Scheduler {exit_reason}; no decision applied. {raw[-2000:]} Use /schedule to try a fresh scheduler session."
            elif not self.stopping:
                try:
                    active["plan"] = json.loads(raw)
                    self._apply_plan(active)
                except (ValueError, RuntimeError, OSError) as error:
                    self.data["scheduler"]["error"] = f"Invalid scheduler decision: {error}. Use /schedule to try again."
        else:
            session = self.store.session(run["session_id"])
            # Traps need complete command/stdin evidence, never a bounded tail.
            outcome = dict(exit=exit_reason, code=code, clean=clean, action=run["origin"], mode=run["mode"],
                           trap=run["trap"], message_id=run["message_id"], journal_offset=run["journal_offset"], summary=raw[-4000:])
            if exit_reason == "trapped":
                self.traps[session["id"]] = raw
            else:
                self.traps.pop(session["id"], None)
            session.update(last=outcome, gate=None if exit_reason == "clean" else exit_reason,
                           reason="" if exit_reason == "clean" else f"Mu {exit_reason} (exit {code}).",
                           retry_authorized=False, revision=session["revision"] + 1)
            try:
                delivered = delivery(self.root, run["session"], run["journal_offset"], clean)
            except (OSError, ValueError) as error:
                delivered = "interrupted"
                session.update(hold=True, gate="interrupted", reason=f"Cannot establish delivery: {error}")
            if delivered == "complete":
                self.data["messages"] = [m for m in self.data["messages"] if m["id"] != run["message_id"]]
            else:
                if exit_reason == "clean":
                    session.update(hold=True, gate="interrupted", reason="Mu returned without completing delivery; inspect before resuming.")
                for message in self.data["messages"]:
                    if message["id"] == run["message_id"]:
                        message["state"] = "interrupted"
            if exit_reason == "interrupted":
                session["hold"] = True
            self.store.event("worker_exit", session["id"], exit=exit_reason)
        try:
            self._workspace()
        except (RuntimeError, OSError) as error:
            self.data["scheduler"]["error"] = f"Cannot inspect workspace: {error}"
        self.store.save()

    def _scheduler_display(self, active):
        stream = active["output"]
        offset = active["screen_offset"]
        chunk = os.pread(stream.fileno(), max(0, os.fstat(stream.fileno()).st_size - offset), offset)
        active["screen_offset"] += len(chunk)
        active["screen"].feed(chunk.replace(b"\n", b"\r\n"))

    def tick(self):
        if self.server:
            self.server.drain(self.request)
        for key, active in list(self.active.items()):
            process, record = active["process"], active["record"]
            if process.poll() is None:
                self._prepare(active)
            if active["capture"]:
                if active["capture"].error and not record["stopping"]:
                    self._stop(active)
            else:
                self._scheduler_display(active)
            ended = process.poll() is not None
            members = owned_members(record) if ended or record["stopping"] else []
            if record["stopping"] and active["stopped_at"] is not None and time.monotonic() - active["stopped_at"] >= 5:
                # Signal escalation during explicit stop only; no invocation watchdog.
                for pid, stamp in members:
                    signal_process(pid, stamp, signal.SIGKILL)
            if ended and members:
                self._stop(active)
                continue
            if ended:
                del self.active[key]
                self._finish(active)
        if self.stopping:
            self.done = not self.active
            return
        if self._running(None) or not self.data["events"] or self.data["scheduler"]["error"]:
            return
        try:
            self._workspace()
            snapshot = copy.deepcopy(dict(self.state(), events=self.data["events"]))
            for session in snapshot["sessions"]:
                if session["gate"] == "trapped" and not session["hold"]:
                    evidence = self.traps.get(session["id"])
                    if evidence is None:
                        evidence = replay(self.root, session["session"], self.mu, full=True)
                    session["last"]["trap_output"] = evidence
            self._spawn(snapshot=snapshot)
        except (RuntimeError, OSError) as error:
            self.data["scheduler"]["error"] = str(error)
            self.store.save()

    def idle(self):
        return not self.active and (not self.data["events"] or bool(self.data["scheduler"]["error"]))

    def close(self):
        if self.closed:
            return
        self.request(dict(op="shutdown", confirmed=True))
        while self.active:
            self.tick()
            time.sleep(0.05)
        if self.server:
            self.server.close()
        self.replay_pool.shutdown(wait=True, cancel_futures=True)
        self.store.save()
        self.store.close()
        self.closed = True
