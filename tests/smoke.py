#!/usr/bin/env python3
import json
import fcntl
import os
from pathlib import Path
import subprocess
import pty
import select
import struct
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FAKE_MU = ROOT / "tests" / "fake_mu.py"
sys.path.insert(0, str(ROOT))

from muboard.engine import Engine
from muboard.ipc import ControlServer
from muboard.state import Store


class BoardSmoke(unittest.TestCase):
    def setUp(self):
        self.projects = []
        FAKE_MU.chmod(0o755)

    def tearDown(self):
        for project in reversed(self.projects):
            project.cleanup()

    def project(self):
        project = tempfile.TemporaryDirectory(prefix="mub-smoke-")
        self.projects.append(project)
        root = Path(project.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        return root

    def env(self, **values):
        return patch.dict(os.environ, values, clear=False)

    def engine(self, root, **values):
        return Engine(root, mu=str(FAKE_MU), max_turns=3, timeout=3, **values)

    def queue(self, engine, text="work", depends_on=None):
        task = engine.store.add_task(text)
        task["state"] = "queued"
        if depends_on is not None:
            task["depends_on"] = list(depends_on)
        engine.store.save()
        return task

    def until(self, engine, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            engine.tick()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for board state")

    def test_owner_lock_git_private_and_reopen(self):
        root = self.project()
        first = self.engine(root)
        try:
            self.assertEqual((root / ".mub" / ".gitignore").read_text(), "*\n")
            self.assertTrue((root / ".mub" / "state.json").exists())
            self.assertTrue((root / ".mub" / "owner.lock").exists())
            with self.assertRaisesRegex(RuntimeError, "already has a running mub"):
                self.engine(root)
            task = first.store.add_task("durable request")
            first.store.message("user", "durable request", task["id"])
            first.store.save()
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=root, text=True, capture_output=True, check=True,
            )
            self.assertEqual(status.stdout, "")
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", ".mub/.gitignore"], cwd=root).returncode, 0)
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", ".mub/state.json"], cwd=root).returncode, 0)
        finally:
            first.close()
        reopened = self.engine(root)
        try:
            self.assertEqual(reopened.store.task(task["id"])["request"], "durable request")
            self.assertEqual(len(reopened.data["messages"]), 1)
        finally:
            reopened.close()

    def test_project_is_pwd_not_an_ancestor(self):
        parent = self.project()
        store = Store(parent)
        store.add_task("Parent board")
        store.save()
        store.close()
        child = parent / "nested"
        child.mkdir()
        environment = dict(os.environ, MUB_PROJECT=str(parent))
        status = subprocess.run([sys.executable, str(ROOT / "mub"), "status"], cwd=child,
                                env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(status.stdout)["root"], str(child))
        self.assertEqual(json.loads(status.stdout)["tasks"], [])
        subprocess.run([sys.executable, str(ROOT / "mub"), "--mu", str(FAKE_MU),
                        "--headless", "--until-idle"], cwd=child, env=environment,
                       capture_output=True, text=True, check=True, timeout=10)
        self.assertTrue((child / ".mub/state.json").exists())
        self.assertEqual(len(json.loads((parent / ".mub/state.json").read_text())["tasks"]), 1)

    def test_model_choices_persist_and_do_not_change_running_work(self):
        root = self.project()
        with self.env(FAKE_MU_MODE="long"):
            board = self.engine(root)
            try:
                models = board.request({"op": "models"})["available"]
                self.assertEqual(models[0]["id"], "codex/gpt-5.6-luna")
                board.request({"op": "set_models", "models": {
                    "pm": "codex/gpt-5.6-luna:medium", "worker": "codex/gpt-5.6-luna:high"}})
                self.queue(board)
                board.tick()
                run = board.active["worker"]
                self.assertEqual(run["record"]["model"], "codex/gpt-5.6-luna:high")
                self.assertIn("codex/gpt-5.6-luna:high", run["process"].args)
                board.request({"op": "set_models", "models": {"worker": "codex/other:medium"}})
                self.assertEqual(run["record"]["model"], "codex/gpt-5.6-luna:high")
                self.assertIn("codex/gpt-5.6-luna:high", run["process"].args)
            finally:
                board.close()
        reopened = self.engine(root, pm_model="codex/other:high")
        try:
            self.assertEqual(reopened.pm_model, "codex/other:high")
            self.assertEqual(reopened.worker_model, "codex/other:medium")
            self.assertEqual(reopened.data["models"]["pm"], "codex/gpt-5.6-luna:medium")
            reopened.request({"op": "set_models", "models": {"worker": None}})
            self.assertIsNone(reopened.worker_model)
            self.assertIsNone(reopened.data["models"]["worker"])
        finally:
            reopened.close()

    def test_pm_plan_is_staged_then_applied_atomically(self):
        root = self.project()
        with self.env(FAKE_MU_MODE="ok", FAKE_MU_PLAN=json.dumps({
            "tasks": [{"id": "clarify", "title": "Clarify", "brief": "answer a question",
                       "state": "needs_input", "question": "Which answer?"}],
        })):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                board.request({"op": "add", "text": "start a project"})
                board.tick()
                self.until(board, lambda: board.active.get("pm", {}).get("plan") is not None)
                self.assertFalse(any(t["title"] == "Clarify" for t in board.data["tasks"]))
                self.until(board, lambda: "pm" not in board.active)
                task = next(t for t in board.data["tasks"] if t["title"] == "Clarify")
                self.assertEqual(task["state"], "needs_input")
                self.assertEqual(task["question"], "Which answer?")
            finally:
                board.close()

    def test_staged_pm_plan_is_discarded_on_unclean_exit(self):
        root = self.project()
        plan = {"tasks": [{"id": "discard", "title": "Discard", "brief": "must not apply",
                            "state": "needs_input", "question": "No apply"}]}
        with self.env(FAKE_MU_MODE="ok", FAKE_MU_PM_FAIL="1", FAKE_MU_PLAN=json.dumps(plan)):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                board.request({"op": "add", "text": "start then fail"})
                board.tick()
                self.until(board, lambda: board.active.get("pm", {}).get("plan") is not None)
                self.until(board, lambda: "pm" not in board.active)
                self.assertFalse(any(t["title"] == "Discard" for t in board.data["tasks"]))
                self.assertTrue(board.data["events"][-1]["kind"] == "plan_failed")
            finally:
                board.close()

    def test_stale_pm_plan_is_rejected_after_user_reply(self):
        root = self.project()
        plan = {"tasks": [{"id": 1, "title": "PM rewrite", "state": "queued"}]}
        with self.env(FAKE_MU_MODE="ok", FAKE_MU_PLAN=json.dumps(plan), FAKE_MU_DELAY="0.08"):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                task_id = board.request({"op": "add", "text": "original"})["task_id"]
                board.tick()
                self.assertIn("pm", board.active)
                board.request({"op": "reply", "task_id": task_id, "text": "new information"})
                self.until(board, lambda: "pm" not in board.active)
                self.assertEqual(board.store.task(task_id)["title"], "original")
                self.assertTrue(any(e["kind"] == "plan_failed" for e in board.data["events"]))
            finally:
                board.close()

    def test_dependencies_block_and_cycles_are_rejected(self):
        root = self.project()
        board = self.engine(root)
        try:
            first = self.queue(board, "first")
            second = self.queue(board, "second", [first["id"]])
            self.assertEqual([t["id"] for t in board._ready()], [first["id"]])
            first["state"] = "done"
            self.assertEqual([t["id"] for t in board._ready()], [second["id"]])
            active = {"revisions": {t["id"]: t["revision"] for t in board.data["tasks"]}}
            with self.assertRaisesRegex(ValueError, "Dependency cycle"):
                board._validate_plan({"tasks": [
                    {"id": first["id"], "depends_on": [second["id"]]},
                    {"id": second["id"], "depends_on": [first["id"]]},
                ]}, active)
        finally:
            board.close()

    def test_only_one_worker_can_own_the_checkout(self):
        root = self.project()
        with self.env(FAKE_MU_MODE="long"):
            board = self.engine(root)
            try:
                first = self.queue(board, "one")
                second = self.queue(board, "two")
                board.tick()
                self.assertEqual(list(board.active), ["worker"])
                self.assertEqual(board.active["worker"]["record"]["task_id"], first["id"])
                self.assertEqual(board.store.task(second["id"])["state"], "queued")
            finally:
                board.close()

    def test_trap_requires_approval_and_approval_reuses_session(self):
        root = self.project()
        with self.env(FAKE_MU_MODE="trap", FAKE_MU_PLAN="{}"):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                task = self.queue(board, "run a command")
                board.tick()
                self.until(board, lambda: board.store.task(task["id"])["gate"] == "approval")
                session = board.store.task(task["id"])["session"]
                with self.assertRaisesRegex(ValueError, "approve|approval"):
                    board.request({"op": "resume", "task_id": task["id"]})
                board.request({"op": "approve", "task_id": task["id"]})
                self.until(board, lambda: "worker" in board.active and board.active["worker"]["record"]["session"] == session)
                self.until(board, lambda: board.store.task(task["id"])["state"] == "review")
                data = json.loads((root / ".mub" / "fake-mu" / f"{session}.json").read_text())
                self.assertTrue(any("--trap" in args and "off" in args for args in data["invocations"]))
            finally:
                board.close()

    def test_explicit_resume_retries_same_worker_session(self):
        root = self.project()
        with self.env(FAKE_MU_MODE="retry", FAKE_MU_PLAN="{}"):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                task = self.queue(board, "retry me")
                board.tick()
                self.until(board, lambda: board.store.task(task["id"])["gate"] == "error")
                session = board.store.task(task["id"])["session"]
                board.request({"op": "resume", "task_id": task["id"]})
                self.until(board, lambda: "worker" in board.active and board.active["worker"]["record"]["session"] == session)
                self.until(board, lambda: board.store.task(task["id"])["state"] == "review")
                self.assertEqual(len([r for r in board.data["runs"] if r["kind"] == "worker"]), 2)
                self.assertEqual({r["session"] for r in board.data["runs"] if r["kind"] == "worker"}, {session})
            finally:
                board.close()

    def test_pm_approval_requires_explicit_retry(self):
        root = self.project()
        with self.env(FAKE_MU_PM_TRAP="1", FAKE_MU_PLAN=json.dumps({
            "tasks": [{"id": 1, "state": "needs_input", "question": "Which format?"}],
        })):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                board.request({"op": "add", "text": "Make a document"})
                self.until(board, lambda: board.data["error"] is not None)
                original = board.data["runs"][-1]
                self.assertEqual(original["status"], "approval")
                self.assertEqual(board.store.task(1)["state"], "inbox")
                board.request({"op": "approve_pm"})
                self.until(board, lambda: board.store.task(1)["state"] == "needs_input")
                self.assertEqual(board.data["runs"][-1]["session"], original["session"])
                self.assertIsNone(board.data["error"])
            finally:
                board.close()

    def test_owned_agent_cannot_use_user_controls(self):
        root = self.project()
        with self.env(FAKE_MU_PEER_CONTROL="1", FAKE_MU_PLAN="{}"):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                board.request({"op": "add", "text": "Inspect the project"})
                self.until(board, lambda: (root / ".mub/fake-mu/peer-control.json").exists())
                response = json.loads((root / ".mub/fake-mu/peer-control.json").read_text())
                self.assertFalse(response["ok"])
                self.assertIn("requires user input", response["error"])
            finally:
                board.close()

    def test_cancel_intent_survives_owner_loss(self):
        root = self.project()
        with self.env(FAKE_MU_MODE="dirty"):
            board = self.engine(root)
            task = self.queue(board, "Provisional work")
            board.tick()
            self.until(board, lambda: (root / "dirty-worker.txt").exists())
            active = board.active["worker"]
            board.request({"op": "cancel", "task_id": task["id"]})
            saved = json.loads((root / ".mub/state.json").read_text())
            self.assertEqual(saved["runs"][-1]["stop_reason"], "cancelled")
            active["process"].wait(timeout=5)
            # Simulate the owner disappearing before it records the exit.
            board.active.clear()
            board.store.close()
            recovered = self.engine(root)
            try:
                self.assertEqual(recovered.store.task(task["id"])["state"], "cancelled")
                self.assertEqual(recovered.data["hold"], task["id"])
                self.assertTrue((root / "dirty-worker.txt").exists())
            finally:
                recovered.close()

    def test_dirty_workspace_is_not_released_by_cancel_or_interrupt(self):
        for operation, state, gate in (("cancel", "cancelled", None), ("stop", "needs_input", "interrupted")):
            root = self.project()
            with self.subTest(operation=operation), self.env(FAKE_MU_MODE="dirty"):
                board = self.engine(root)
                try:
                    task = self.queue(board, "make provisional change")
                    board.tick()
                    self.until(board, lambda: "worker" in board.active and
                               (root / "dirty-worker.txt").exists())
                    board.request({"op": operation, "task_id": task["id"]})
                    self.until(board, lambda: not board.active)
                    current = board.store.task(task["id"])
                    self.assertEqual(current["state"], state)
                    self.assertEqual(current["gate"], gate)
                    self.assertEqual(board.data["hold"], task["id"])
                    self.assertTrue((root / "dirty-worker.txt").exists())
                finally:
                    board.close()

    def test_headless_owner_accepts_control_requests(self):
        root = self.project()
        seed = Store(root)
        seed.add_task("seed pending event")
        seed.event("submitted", 1)
        seed.save()
        seed.close()
        environment = os.environ.copy()
        environment.update(FAKE_MU_MODE="ok", FAKE_MU_PLAN="{}", FAKE_MU_DELAY="0.25")
        command = [sys.executable, str(ROOT / "mub"), "-C", str(root), "--mu", str(FAKE_MU),
                   "run", "--headless", "--until-idle"]
        process = subprocess.Popen(command, cwd=ROOT, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (root / ".mub" / "control.sock").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((root / ".mub" / "control.sock").exists())
            status = subprocess.run([sys.executable, str(ROOT / "mub"), "-C", str(root), "status"],
                                    cwd=ROOT, env=environment, capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(status.stdout)["root"].endswith(Path(root).name))
            added = subprocess.run([sys.executable, str(ROOT / "mub"), "-C", str(root), "add", "via control"],
                                   cwd=ROOT, env=environment, capture_output=True, text=True, check=True)
            self.assertIn('"task_id"', added.stdout)
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stderr)
            state = json.loads(subprocess.run([sys.executable, str(ROOT / "mub"), "-C", str(root), "status"],
                                              cwd=ROOT, env=environment, capture_output=True, text=True, check=True).stdout)
            self.assertTrue(any(task["request"] == "via control" for task in state["tasks"]))
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

    def test_tui_submission_and_project_reply(self):
        root = self.project()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        environment = os.environ.copy()
        environment.update(TERM="xterm-256color", FAKE_MU_MODE="ok", FAKE_MU_PLAN=json.dumps({
            "reply": "Please choose a name.",
            "tasks": [{"id": 1, "state": "needs_input", "question": "Which name?"}],
        }))
        process = subprocess.Popen([sys.executable, str(ROOT / "mub"), "-C", str(root),
                                    "--mu", str(FAKE_MU)], env=environment,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        screen = bytearray()

        def pump(seconds=0.3):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.02)[0]:
                    try:
                        screen.extend(os.read(master, 65536))
                    except OSError:
                        break

        def send(text):
            os.write(master, text.encode())
            pump()

        def state():
            path = root / ".mub" / "state.json"
            return json.loads(path.read_text()) if path.exists() else {"tasks": [], "messages": []}

        def wait(predicate):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                pump(0.1)
                if predicate():
                    return
                self.assertIsNone(process.poll(), screen[-2000:].decode(errors="replace"))
            self.fail("TUI timed out: " + screen[-2000:].decode(errors="replace"))

        try:
            wait(lambda: b"Mu Board" in screen)
            send("M")
            send("\r")  # Both roles.
            send("j\r")  # First configured model, not inherit.
            send("j\r")  # Medium effort.
            wait(lambda: state().get("models") == {
                "pm": "codex/gpt-5.6-luna:medium", "worker": "codex/gpt-5.6-luna:medium"})
            send("n")
            send("Greeting\tImplement a greeting for café 日本語.\nAsk me which name.\x13")
            wait(lambda: state()["tasks"] and state()["tasks"][0]["state"] == "needs_input")
            self.assertIn("café 日本語", state()["tasks"][0]["request"])
            self.assertEqual(state()["runs"][0]["model"], "codex/gpt-5.6-luna:medium")
            send("m")
            send(" ")
            send("Keep this as a design discussion.\x13")
            wait(lambda: any(m["content"] == "Keep this as a design discussion." and m["task_id"] is None
                             for m in state()["messages"]))
            send("3")
            wait(lambda: b"staged" in screen)
            send("\x1b")
            send("\r")
            wait(lambda: b"Brief" in screen and b"Discussion" in screen)
            send("\x1b")
            send("q")
            send("s")
            send("y")
            wait(lambda: process.poll() is not None)
            self.assertEqual(process.returncode, 0, screen[-2000:].decode(errors="replace"))
        finally:
            if process.poll() is None:
                process.terminate()
                pump(1)
                process.wait(timeout=8)
            os.close(master)


if __name__ == "__main__":
    unittest.main(verbosity=2)
