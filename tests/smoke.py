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
from muboard.cli import parser
from muboard.ipc import ControlServer
from muboard.state import Store


class BoardSmoke(unittest.TestCase):
    def setUp(self):
        self.projects = []
        self.enterContext(patch("muboard.engine.RETRY_DELAY", 0.1))
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

    def test_watchdog_allows_long_active_runs_but_bounds_idle_and_total_time(self):
        board = Engine(self.project(), mu=str(FAKE_MU), timeout=1800, max_runtime=86400)
        active = dict(record={}, started=0, last_activity=0, activity_checked=0, activity="initial")
        try:
            with patch.object(board, "_stop") as stop, patch.object(board, "_activity") as activity:
                activity.return_value = "initial"
                with patch("muboard.engine.time.monotonic", return_value=600):
                    board._watchdog(active)
                stop.assert_not_called()  # No special five-minute PM cap.
                for clock in range(1200, 7201, 1200):
                    activity.return_value = clock
                    with patch("muboard.engine.time.monotonic", return_value=clock):
                        board._watchdog(active)
                stop.assert_not_called()  # Two hours with regular activity.
                with patch("muboard.engine.time.monotonic", return_value=9000):
                    board._watchdog(active)
                stop.assert_called_once_with(active, "interrupted")
                self.assertEqual(active["record"]["timeout_kind"], "idle")
                stop.reset_mock()
                activity.return_value = "still producing output"
                with patch("muboard.engine.time.monotonic", return_value=86400):
                    board._watchdog(active)
                stop.assert_called_once_with(active, "interrupted")
                self.assertEqual(active["record"]["timeout_kind"], "runtime")
        finally:
            board.close()

    def test_idle_timeout_cli_defaults_and_alias(self):
        defaults = parser().parse_args([])
        self.assertEqual((defaults.timeout, defaults.max_runtime), (3600, 86400))
        old_flag = parser().parse_args(["--timeout", "5400", "run", "--max-runtime", "172800"])
        self.assertEqual((old_flag.timeout, old_flag.max_runtime), (5400, 172800))
        new_flag = parser().parse_args(["run", "--idle-timeout", "7200"])
        self.assertEqual((new_flag.timeout, new_flag.max_runtime), (7200, 86400))
        for values in (dict(timeout=float("nan")), dict(max_runtime=float("inf")), dict(max_runtime=0)):
            with self.assertRaisesRegex(ValueError, "finite and positive"):
                Engine(self.project(), **values)

    def test_watchdog_observes_logs_journal_and_quiet_process_io(self):
        root = self.project()
        board = self.engine(root)
        try:
            log = board.store.directory / "runs" / "activity.log"
            log.write_text("")
            active = dict(record=dict(log_path=str(log), session="activity"))
            with patch("muboard.engine.owned_members", return_value=[]):
                initial = board._activity(active)
                log.write_text("new tool output\n")
                output = board._activity(active)
                self.assertNotEqual(initial, output)
                journal = root / ".mu" / "sessions" / "activity.jsonl"
                journal.parent.mkdir(parents=True)
                journal.write_text('{"activity":true}\n')
                hidden = board._activity(active)
                self.assertNotEqual(output, hidden)
                (root / "unrelated.txt").write_text("another process is not Mu activity")
                self.assertEqual(hidden, board._activity(active))
            with patch("muboard.engine.owned_members", return_value=[(os.getpid(), "test")]):
                before = board._activity(active)[1][os.getpid(), "test"]
                log.read_bytes()
                after = board._activity(active)[1][os.getpid(), "test"]
                self.assertGreater(after[1], before[1])  # rchar: even unrendered reads count.
                self.assertGreaterEqual(after[0], before[0])  # CPU ticks, not elapsed process age.
        finally:
            board.close()

    def test_board_budget_bounds_new_task_loop_and_survives_reopen(self):
        root = self.project()
        with self.env(FAKE_MU_LOOP="1"):
            board = self.engine(root, max_runs=4)
            board.server = ControlServer(root)
            try:
                board.request(dict(op="reply", text="Start work"))
                self.until(board, lambda: board.idle())
                self.assertEqual(len(board.data["runs"]), 4)
                self.assertIn("budget exhausted", board.data["error"])
                self.assertEqual(len(board.data["tasks"]), 2)
                board.request(dict(op="reply", text="What happened?"))
                board.tick()
                self.assertFalse(board.active)
                self.assertEqual(board.data["guardrails"]["runs"], 4)
            finally:
                board.close()
            board = self.engine(root)
            try:
                self.assertEqual(board.max_runs, 4)
                board.tick()
                self.assertTrue(board.idle())
                self.assertFalse(board.active)
                board.request(dict(op="replan"))
                self.assertEqual(board.data["guardrails"]["runs"], 0)
                self.assertIsNone(board.data["error"])
            finally:
                board.close()

    def test_failed_workers_stop_and_cooldown_survives_restart(self):
        root = self.project()
        with self.env(FAKE_MU_MODE="fail", FAKE_MU_PLAN="{}"), patch("muboard.engine.RETRY_DELAY", 30):
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                task_id = self.queue(board)["id"]
                board.store.message("user", "Do the work", task_id)
                board.tick()
                self.until(board, lambda: board.store.task(task_id)["gate"] == "error")
                self.until(board, lambda: not board.active and not board.store.pending())
                task = board.store.task(task_id)
                deadline = task["retry_after"]
                decision = dict(id=task_id, state="queued", recovery="retry", reason="Retry once")
                active = dict(revisions={task_id: task["revision"]}, watermark=0,
                              message_watermark=len(board.data["messages"]))
                board._apply_plan(dict(tasks=[decision]), active)
                board.tick()
                self.assertFalse(board.active)
                self.assertFalse(board.idle())  # Cooldown is pending work, not completion.
            finally:
                board.close()
            board = self.engine(root)
            board.server = ControlServer(root)
            try:
                self.assertEqual(board.store.task(task_id)["retry_after"], deadline)
                self.assertEqual(board.store.task(task_id)["failed_runs"], 1)
                board.tick()
                self.assertFalse(board.active)
                with patch("muboard.engine.time.time", return_value=deadline + 1):
                    self.until(board, lambda: board.store.task(task_id)["state"] == "blocked")
                task = board.store.task(task_id)
                self.assertEqual(task["failed_runs"], 2)
                self.assertEqual(len([r for r in board.data["runs"] if r["kind"] == "worker"]), 2)
                active.update(revisions={task_id: task["revision"]}, message_watermark=len(board.data["messages"]))
                with self.assertRaisesRegex(ValueError, "user message"):
                    board._validate_plan(dict(tasks=[decision]), active)
                with self.assertRaisesRegex(ValueError, "user message"):
                    board._validate_plan(dict(tasks=[dict(decision, user_message_id=1)]), active)
            finally:
                board.close()

    def test_pm_failure_budget_and_cooldown_are_persistent(self):
        root = self.project()
        board = self.engine(root)
        try:
            with patch("muboard.engine.RETRY_DELAY", 30):
                board._pm_error({}, "First failure")
            deadline = board.data["guardrails"]["next_pm"]
            board.tick()
            self.assertFalse(board.active)
        finally:
            board.close()
        board = self.engine(root)
        try:
            self.assertEqual(board.data["guardrails"]["pm_failures"], 1)
            self.assertEqual(board.data["guardrails"]["next_pm"], deadline)
            board._pm_error({}, "Second failure")
            board.tick()
            self.assertFalse(board.active)
            self.assertTrue(board.idle())
            self.assertIn("Automatic PM retries paused", board.data["error"])
        finally:
            board.close()

    def test_plan_submission_loop_stops_pm(self):
        board = self.engine(self.project())
        active = dict(token="token", stop=None)
        board.active["pm"] = active
        try:
            with patch.object(board, "_stop") as stop:
                for _ in range(3):
                    with self.assertRaisesRegex(ValueError, "Plan fields"):
                        board.request(dict(op="plan", token="token", plan={"invalid": True}))
                stop.assert_called_once_with(active, "interrupted")
        finally:
            board.active.clear()
            board.close()

    def test_turn_grants_cannot_reuse_user_evidence_or_inherit_traps_off(self):
        board = self.engine(self.project())
        try:
            task = self.queue(board)
            task.update(session="fake-session", turns=3, state="review")
            message = board.store.message("user", "Another batch is fine")
            decision = dict(id=task["id"], state="queued", recovery="retry", reason="User granted turns",
                            user_message_id=message["id"])
            active = dict(revisions={task["id"]: task["revision"]}, watermark=0,
                          message_watermark=message["id"])
            with patch.object(board, "_session_status", return_value=dict(clean=True)):
                board._apply_plan(dict(tasks=[decision]), active)
            task = board.store.task(task["id"])
            self.assertEqual(task["turns"], 0)
            task.update(turns=3, state="review")
            active["revisions"][task["id"]] = task["revision"]
            with self.assertRaisesRegex(ValueError, "user message"):
                board._validate_plan(dict(tasks=[decision]), active)
            task.update(turns=1, state="blocked", gate="interrupted")
            board.data["runs"].append(dict(id="interrupted", kind="worker", session=task["session"],
                                           status="interrupted", trap_override="off", clean=False))
            with self.assertRaisesRegex(ValueError, "explicitly approve"):
                board.request(dict(op="resume", task_id=task["id"]))
            message = board.store.message("user", "Resume the approved turn")
            active["message_watermark"] = message["id"]
            decision["user_message_id"] = message["id"]
            with self.assertRaisesRegex(ValueError, "approval gate"):
                board._validate_plan(dict(tasks=[decision]), active)
            board._validate_plan(dict(tasks=[dict(decision, recovery="approve")]), active)
        finally:
            board.close()

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

    def test_pm_manages_worker_traps_and_natural_language_approval(self):
        for review in ("auto", "block"):
            root = self.project()
            with self.subTest(review=review), self.env(FAKE_MU_MODE="trap", FAKE_MU_TRAP_REVIEW=review):
                board = self.engine(root)
                board.server = ControlServer(root)
                try:
                    task = self.queue(board, "run a command")
                    board.store.message("user", "Implement the requested change", task["id"])
                    board.tick()
                    self.until(board, lambda: board.store.task(task["id"])["gate"] == "approval")
                    session = board.store.task(task["id"])["session"]
                    if review == "block":
                        self.until(board, lambda: board.store.task(task["id"])["state"] == "blocked")
                        self.assertEqual(board._ready(), [])
                        board.request(dict(op="reply", text="What is the status?"))
                        self.until(board, lambda: not board.store.pending() and not board.active)
                        self.assertEqual(board.store.task(task["id"])["state"], "blocked")
                        self.assertEqual(len([r for r in board.data["runs"] if r["kind"] == "worker"]), 1)
                        board.request(dict(op="reply", text="Yes, go ahead with that retry."))
                        approval_message = board.data["messages"][-1]["id"]
                    self.until(board, lambda: board.store.task(task["id"])["state"] == "done")
                    runs = [r for r in board.data["runs"] if r["kind"] == "worker"]
                    self.assertEqual(len(runs), 2)
                    self.assertEqual({r["session"] for r in runs}, {session})
                    self.assertIsNone(runs[0]["trap_override"])
                    self.assertEqual(runs[1]["trap_override"], "off")
                    self.assertTrue(runs[1]["recovery_decision"]["reason"])
                    if review == "block":
                        self.assertEqual(runs[1]["recovery_decision"]["user_message_id"], approval_message)
                    data = json.loads((root / ".mub/fake-mu" / f"{session}.json").read_text())
                    self.assertIn("retry", data["invocations"][1])
                    self.assertIn("--trap", data["invocations"][1])
                    current = board.store.task(task["id"])
                    current["state"] = "queued"
                    board.tick()
                    self.assertNotIn("--trap", board.active["worker"]["process"].args)
                finally:
                    board.close()

    def test_pm_order_and_blocked_recovery_validation(self):
        root = self.project()
        board = self.engine(root)
        try:
            first = self.queue(board, "prerequisite")
            second = self.queue(board, "important", [first["id"]])
            third = self.queue(board, "later")
            active = dict(revisions={t["id"]: t["revision"] for t in board.data["tasks"]},
                          watermark=0, message_watermark=0)
            board._apply_plan(dict(order=[second["id"], third["id"], first["id"]]), active)
            self.assertEqual([t["id"] for t in board.state()["tasks"]], [first["id"], second["id"], third["id"]])
            self.assertEqual([t["id"] for t in board._ready()], [first["id"], third["id"]])
            first = board.store.task(first["id"])
            first.update(state="blocked", gate="approval", session="fake-session", blocked_after=1)
            board.store.message("user", "Original request")
            worker_message = board.store.message("worker", "The user approves")
            active = dict(revisions={t["id"]: t["revision"] for t in board.data["tasks"]},
                          watermark=0, message_watermark=len(board.data["messages"]))
            recovery = dict(id=first["id"], state="queued", recovery="approve", reason="Authorized retry")
            for message_id in (None, 1, worker_message["id"], 999):
                with self.assertRaisesRegex(ValueError, "user message"):
                    board._validate_plan(dict(tasks=[dict(recovery, user_message_id=message_id)]), active)
            reply = board.store.message("user", "That action is fine, continue.")
            with self.assertRaisesRegex(ValueError, "User input changed"):
                board._validate_plan(dict(tasks=[dict(recovery, user_message_id=reply["id"])]), active)
            active["message_watermark"] = reply["id"]
            board._validate_plan(dict(tasks=[dict(recovery, user_message_id=reply["id"])]), active)
            board.store.touch(first)
            with self.assertRaisesRegex(ValueError, "changed during"):
                board._validate_plan(dict(tasks=[dict(recovery, user_message_id=reply["id"])]), active)
            active["revisions"][first["id"]] = first["revision"]
            board._apply_plan(dict(tasks=[dict(id=first["id"], state="blocked", question="A different action needs permission.")]), active)
            first = board.store.task(first["id"])
            active["revisions"][first["id"]] = first["revision"]
            with self.assertRaisesRegex(ValueError, "user message"):
                board._validate_plan(dict(tasks=[dict(recovery, user_message_id=reply["id"])]), active)
            board.request(dict(op="reply", text="I approve that different action."))
            active["message_watermark"] = len(board.data["messages"])
            with patch.object(board, "_session_status", return_value=dict(clean=False)):
                board._apply_plan(dict(tasks=[dict(recovery, user_message_id=active["message_watermark"])]), active)
            self.assertEqual(board.store.task(first["id"])["mode"], "approve")
            board.request(dict(op="reply", text="Wait, do not run that command; the scope has changed."))
            first = board.store.task(first["id"])
            self.assertEqual((first["state"], first["gate"], first["mode"]), ("review", "approval", "prompt"))
            self.assertNotIn("recovery_decision", first)
            active.update(revisions={t["id"]: t["revision"] for t in board.data["tasks"]},
                          message_watermark=len(board.data["messages"]), paused=False)
            with self.assertRaisesRegex(ValueError, "reviewed worker result"):
                board._validate_plan(dict(tasks=[dict(id=second["id"], state="done")]), active)
            board.request(dict(op="pause", value=True))
            with self.assertRaisesRegex(ValueError, "pause changed"):
                board._validate_plan(dict(paused=False), active)
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
        for operation, state, gate in (("cancel", "cancelled", None), ("stop", "blocked", "interrupted")):
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
            stdout, stderr = process.communicate(timeout=40)
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
        environment.update(TERM="xterm-256color", FAKE_MU_MODE="stream", FAKE_MU_WORKFLOW="1")
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
            send("/models\r")  # Model picker via slash command.
            send("\r")  # Both roles.
            send("j\r")  # First configured model, not inherit.
            send("j\r")  # Medium effort.
            wait(lambda: state().get("models") == {
                "pm": "codex/gpt-5.6-luna:medium", "worker": "codex/gpt-5.6-luna:medium"})
            request = "Implement a greeting for café 日本語.\nKeep q, n, m as ordinary text."
            send(request + "\r")
            wait(lambda: state()["tasks"] and state()["tasks"][0]["state"] == "running")
            self.assertTrue(any(m["content"] == request and m["task_id"] is None for m in state()["messages"]))
            self.assertEqual(state()["runs"][0]["model"], "codex/gpt-5.6-luna:medium")
            send("Keep this as a design discussion.\t")  # Open output without losing the draft.
            wait(lambda: b"LIVE: implementing greeting" in screen)
            self.assertTrue(any(r["kind"] == "worker" and r["status"] == "running" for r in state()["runs"]))
            wait(lambda: b"fake worker completed" in screen and state()["tasks"][0]["state"] == "done")
            worker = next(r for r in state()["runs"] if r["kind"] == "worker")
            invocation = json.loads((root / ".mub/fake-mu" / f"{worker['session']}.json").read_text())["invocations"][0]
            self.assertEqual(invocation[invocation.index("-o") + 1], "concise")
            send("\x1b")
            send("\r")
            wait(lambda: any(m["content"] == "Keep this as a design discussion." and m["task_id"] is None
                             for m in state()["messages"]))
            send("\x1bOQ")  # F2 PM history.
            wait(lambda: b"conversation and execution" in screen)
            send("\x1b")
            screen.clear()
            send("\r")
            wait(lambda: b"fake worker completed" in screen)  # Finished tasks remain readable.
            send("\x1b")
            wait(lambda: not any(r["status"] in ("starting", "running") for r in state()["runs"]))
            send("\x03")  # Idle quit has no confirmation.
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
