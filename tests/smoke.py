#!/usr/bin/env python3
"""Essential invariants, using only fake Mu processes and temporary worktrees."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests/fake_mu.py"
sys.path.insert(0, str(ROOT))

from muboard.engine import Engine, owned_members
from muboard.ipc import ControlServer
from muboard.state import read_state


class SchedulerSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mub-smoke-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.addCleanup(self.temp.cleanup)
        self.boards = []
        self.addCleanup(self.close_boards)

    def close_boards(self):
        for board in reversed(self.boards):
            board.close()

    def board(self, directory=None):
        board = Engine(directory or self.root, mu=str(FAKE))
        board.server = ControlServer(board.root)
        self.boards.append(board)
        return board

    def config(self, **values):
        folder = self.root / ".mu/fake"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".gitignore").write_text("*\n")
        (folder / "config.json").write_text(json.dumps(values))

    def until(self, board, condition, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            board.tick()
            if condition():
                return
            time.sleep(0.01)
        self.fail("Timed out: " + json.dumps(board.state()))

    def new(self, board, text):
        key = board.request(dict(op="new", text=text))["session_id"]
        return board.store.session(key)

    def journal(self, session):
        return json.loads((self.root / ".mu/fake" / (session["session"] + ".json")).read_text())

    def test_fifo_late_messages_and_persistent_scheduler(self):
        self.config(scheduler_delay=0.12, worker_delay=0.15)
        board = self.board()
        first = self.new(board, "read first")
        board.tick()
        self.assertIsNotNone(board._running(None))
        second = self.new(board, "read independent")
        board.request(dict(op="send", session_id=first["id"], text="read second"))
        saved = read_state(self.root)
        self.assertEqual(len(saved["messages"]), 3)
        self.until(board, lambda: board._running(first["id"]) is not None)
        board.request(dict(op="send", session_id=first["id"], text="read late"))
        self.until(board, board.idle)
        self.assertFalse(board.data["messages"])
        turns = self.journal(first)["invocations"]
        self.assertEqual([t["prompt"].split("USER MESSAGE:\n")[1].strip() for t in turns],
                         ["read first", "read second", "read late"])
        self.assertEqual(len(self.journal(second)["invocations"]), 1)
        scheduler = self.journal(board.data["scheduler"])
        self.assertGreaterEqual(len(scheduler["invocations"]), 3)
        count = len(scheduler["invocations"])
        for _ in range(10):
            board.tick()
        self.assertEqual(len(self.journal(board.data["scheduler"])["invocations"]), count)

    def test_concurrent_readers_one_writer_and_dirty_handoff(self):
        self.config(worker_delay=0.25)
        board = self.board()
        reader1 = self.new(board, "read one")
        reader2 = self.new(board, "read two")
        writer1 = self.new(board, "write one")
        writer2 = self.new(board, "write two")
        self.until(board, lambda: len([a for a in board.active.values() if a["record"]["kind"] == "worker"]) >= 3)
        self.assertIsNotNone(board._running(reader1["id"]))
        self.assertIsNotNone(board._running(reader2["id"]))
        self.assertIsNotNone(board._running(writer1["id"]))
        self.assertIsNone(board._running(writer2["id"]))
        while not board.idle():
            board.tick()
            writers = [a for a in board.active.values() if a["record"]["kind"] == "worker" and a["record"]["mode"] == "readwrite"]
            self.assertLessEqual(len(writers), 1)
            time.sleep(0.01)
        self.assertTrue(board._workspace()["clean"])
        self.assertIsNone(board.data["owner"])
        for session in (writer1, writer2):
            turns = self.journal(session)["invocations"]
            self.assertEqual(len(turns), 2)
            self.assertIn("SCHEDULER HANDOFF REQUEST", turns[1]["prompt"])

    def test_interrupt_holds_and_new_message_is_not_retry_permission(self):
        board = self.board()
        session = self.new(board, "write hold")
        self.until(board, lambda: any(self.root.glob("work-*.txt")))
        board.request(dict(op="send", session_id=session["id"], text="read already queued"))
        board.request(dict(op="interrupt", session_id=session["id"]))
        self.until(board, board.idle)
        self.assertTrue(session["hold"])
        self.assertEqual(session["gate"], "interrupted")
        self.assertEqual(board.data["owner"], session["id"])
        other = self.new(board, "write waiting")
        reader = self.new(board, "read independent")
        self.until(board, board.idle)
        self.assertIsNone(other["session"])
        self.assertIsNotNone(reader["last"])
        board.request(dict(op="send", session_id=session["id"], text="read changed request"))
        self.until(board, board.idle)
        self.assertFalse(session["hold"])
        self.assertEqual(len(self.journal(session)["invocations"]), 1)
        with self.assertRaisesRegex(ValueError, "workspace"):
            board.request(dict(op="remove", session_id=session["id"], discard=True))
        self.config(release_hold=True)
        board.request(dict(op="resume", session_id=session["id"]))
        self.until(board, board.idle)
        self.assertTrue(board.workspace["clean"])
        self.assertFalse(board.data["messages"])
        turns = self.journal(session)["invocations"]
        self.assertIn("retry", turns[1]["args"])

    def test_trap_promotion_and_policy_reset(self):
        board = self.board()
        session = self.new(board, "trap write")
        self.until(board, board.idle)
        turns = self.journal(session)["invocations"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["args"][turns[0]["args"].index("--trap") + 1], "reversible")
        self.assertIn("retry", turns[1]["args"])
        self.assertEqual(turns[1]["args"][turns[1]["args"].index("--trap") + 1], "off")
        snapshots = [json.loads(p.read_text()) for p in (self.root / ".mu/fake").glob("snapshot-*")]
        trapped = next(s for snap in snapshots for s in snap["sessions"] if s["gate"] == "trapped")
        self.assertIn("complete stdin", trapped["last"]["trap_output"])
        board.request(dict(op="send", session_id=session["id"], text="read next"))
        self.until(board, board.idle)
        args = self.journal(session)["invocations"][-1]["args"]
        self.assertNotIn("retry", args)
        self.assertEqual(args[args.index("--trap") + 1], "reversible")

    def test_failures_block_dependents_without_repairs(self):
        board = self.board()
        prerequisite = self.new(board, "fail provider")
        dependent = self.new(board, f"depends {prerequisite['id']}")
        independent = self.new(board, "read independent")
        self.until(board, board.idle)
        self.assertEqual(prerequisite["gate"], "failed")
        self.assertEqual(len(self.journal(prerequisite)["invocations"]), 1)
        self.assertIsNone(dependent["session"])
        self.assertIn("Waiting", dependent["blocked"])
        self.assertEqual(independent["last"]["exit"], "clean")

    def test_failed_commit_does_not_loop(self):
        self.config(commit_refused=True)
        board = self.board()
        session = self.new(board, "write unfinished")
        other = self.new(board, "write later")
        self.until(board, board.idle)
        self.assertEqual(len(self.journal(session)["invocations"]), 2)
        self.assertEqual(board.data["owner"], session["id"])
        self.assertIsNone(other["session"])

    def test_stale_dispatch_and_invalid_fifo(self):
        self.config(scheduler_delay=0.3)
        board = self.board()
        held = self.new(board, "read held")
        removed = self.new(board, "read removed")
        board.request(dict(op="send", session_id=held["id"], text="read second"))
        self.until(board, lambda: board._running(None) is not None and self.journal(board.data["scheduler"])["active"]["busy"])
        active = board._running(None)
        last_message = board.data["messages"][-1]
        with self.assertRaisesRegex(ValueError, "FIFO"):
            board._validate_plan(dict(reason="wrong", actions=[dict(type="dispatch", message_id=last_message["id"], mode="readonly")]), active)
        board.request(dict(op="interrupt", session_id=held["id"]))
        board.request(dict(op="remove", session_id=removed["id"], discard=True))
        self.until(board, board.idle)
        self.assertIsNone(held["session"])
        self.assertTrue(held["hold"])
        self.assertEqual(len(board.data["messages"]), 2)

    def test_worktree_lock_models_restart_and_removal(self):
        board = self.board()
        child = self.root / "nested"
        child.mkdir()
        with self.assertRaisesRegex(RuntimeError, "already has"):
            Engine(child, mu=str(FAKE))
        board.request(dict(op="set_models", models=dict(scheduler="fake/model:low", worker="fake/model:high")))
        session = self.new(board, "read hold")
        self.until(board, lambda: board._running(session["id"]) is not None and self.journal(session)["active"]["busy"])
        run = dict(board._running(session["id"])["record"])
        board.request(dict(op="set_models", models=dict(worker="fake/other")))
        self.assertEqual(run["model"], "fake/model:high")
        board.close()
        self.assertFalse(owned_members(run))
        reopened = self.board(child)
        self.assertEqual(reopened.root, self.root)
        current = reopened.store.session(session["id"])
        self.assertTrue(current["hold"])
        self.assertEqual(reopened.models["worker"], "fake/other")
        self.until(reopened, reopened.idle)
        self.assertEqual(len(self.journal(current)["invocations"]), 1)
        with self.assertRaisesRegex(ValueError, "confirm"):
            reopened.request(dict(op="remove", session_id=current["id"]))
        reopened.request(dict(op="remove", session_id=current["id"], discard=True))
        self.assertTrue((self.root / ".mu/fake" / f"{session['session']}.json").exists())
        self.assertFalse(reopened.data["sessions"])
        self.assertEqual(subprocess.run(["git", "check-ignore", "-q", ".mu/mub.json"], cwd=self.root).returncode, 0)
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.root), b"")

    def test_scheduler_failure_needs_explicit_recheck(self):
        self.config(scheduler_fail=True)
        board = self.board()
        session = self.new(board, "read queued")
        self.until(board, board.idle)
        old = board.data["scheduler"]["session"]
        self.assertIsNotNone(board.data["scheduler"]["error"])
        self.new(board, "read also queued")
        board.tick()
        self.assertFalse(board.active)
        self.config()
        board.request(dict(op="schedule"))
        self.until(board, board.idle)
        self.assertNotEqual(board.data["scheduler"]["session"], old)
        self.assertEqual(session["last"]["exit"], "clean")

    def test_early_exit_keeps_undelivered_input(self):
        self.config(early_fail=True)
        board = self.board()
        session = self.new(board, "read not yet delivered")
        self.until(board, board.idle)
        self.assertEqual(session["gate"], "failed")
        self.assertTrue(session["last"]["clean"])
        self.assertEqual(len(board.data["messages"]), 1)
        self.assertEqual(self.journal(session)["invocations"], [])
        self.config()
        board.request(dict(op="resume", session_id=session["id"]))
        self.until(board, board.idle)
        self.assertFalse(board.data["messages"])
        turns = self.journal(session)["invocations"]
        self.assertEqual(len(turns), 1)
        self.assertNotIn("retry", turns[0]["args"])

    def test_crash_recovery_holds_dirty_work_and_retains_mailbox(self):
        board = self.board()
        session = self.new(board, "write hold")
        self.until(board, lambda: any(self.root.glob("work-*.txt")))
        active = board._running(session["id"])
        active["process"].kill()
        active["process"].wait()
        active["output"].close()
        board.active.clear()
        board.server.close()
        board.store.close()
        board.closed = True
        reopened = self.board()
        current = reopened.store.session(session["id"])
        self.assertTrue(current["hold"])
        self.assertEqual(current["gate"], "interrupted")
        self.assertEqual(reopened.data["owner"], session["id"])
        self.assertEqual(len(reopened.data["messages"]), 1)
        self.until(reopened, reopened.idle)
        self.assertEqual(len(self.journal(current)["invocations"]), 1)

    def test_tui_session_composer_interrupt_and_quit(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        process = subprocess.Popen([sys.executable, str(ROOT / "mub"), "-C", str(self.root), "--mu", str(FAKE)],
                                   stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ, TERM="xterm-256color"),
                                   start_new_session=True)
        os.close(slave)
        screen = bytearray()

        def pump(seconds=0.1):
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

        def wait(condition):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                pump()
                if condition():
                    return
                self.assertIsNone(process.poll(), screen[-2000:].decode(errors="replace"))
            self.fail(screen[-3000:].decode(errors="replace"))

        def state():
            return read_state(self.root)

        try:
            wait(lambda: b"Mu Board" in screen)
            send("/new Test\r")
            wait(lambda: len(state()["sessions"]) == 1)
            send("read hold café\r")
            wait(lambda: any(r["kind"] == "worker" for r in state()["inflight"]))
            send("read queued\r")
            wait(lambda: len(state()["messages"]) == 2)
            send("\x11")
            wait(lambda: b"quit" in screen.lower() and b"cancel" in screen.lower())
            send("n")
            self.assertIsNone(process.poll())
            send("discard this draft\x03")
            self.assertFalse(state()["sessions"][0]["hold"])
            send("\x1b[Z")
            send("\x03")
            wait(lambda: state()["sessions"][0]["hold"] and not any(r["kind"] == "worker" for r in state()["inflight"]))
            wait(lambda: not state()["inflight"] and not state()["events"])
            send("\r")
            send("/new Exit check\r")
            send("read hold\r")
            wait(lambda: any(r["kind"] == "worker" for r in state()["inflight"]))
            running = list(state()["inflight"])
            send("\x11")
            send("y")
            wait(lambda: process.poll() is not None)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(len(state()["messages"]), 3)
            self.assertFalse(state()["inflight"])
            self.assertTrue(all(not owned_members(run) for run in running))
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                pump(0.5)
                process.wait(timeout=8)
            os.close(master)


if __name__ == "__main__":
    unittest.main(verbosity=2)
