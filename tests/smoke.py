#!/usr/bin/env python3
"""Essential invariants, using only fake Mu processes and temporary worktrees."""
import fcntl
import curses
from concurrent.futures import Future
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
from unittest.mock import DEFAULT, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests/fake_mu.py"
sys.path.insert(0, str(ROOT))

from muboard.engine import Engine, owned_members
from muboard.ipc import ControlServer
from muboard.output import conversation, excerpt, journal_events, live_prompt, plain_output, prompt_bytes, scheduler_usage
from muboard.terminal import Capture, Screen
from muboard.state import read_state
from muboard.ui import _UI


class InputSmoke(unittest.TestCase):
    def test_composer_sessions_and_output_are_independent(self):
        engine = MagicMock()
        engine.state.return_value = {"sessions": [{"id": 1}, {"id": 2}]}
        with patch.multiple("muboard.ui.curses", raw=DEFAULT, nonl=DEFAULT, set_escdelay=DEFAULT,
                            typeahead=DEFAULT, mousemask=DEFAULT, mouseinterval=DEFAULT, has_colors=DEFAULT) as mocks:
            mocks["has_colors"].return_value = False
            ui = _UI(MagicMock(), engine)

        ui._set_draft("/ren")
        ui._main_key(9)
        self.assertEqual((ui.selected, ui.draft, ui.cursor), (1, "/rename ", 8))
        engine.request.assert_not_called()
        ui._main_key(9)
        self.assertEqual((ui.selected, ui.draft), (2, ""))
        ui._set_draft("second", 2)
        ui._main_key(9)
        self.assertEqual((ui.selected, ui.draft, ui.cursor), (1, "/rename ", 8))
        ui._set_draft("/ren")
        ui._main_key(curses.KEY_RIGHT)
        self.assertEqual(ui.draft, "/rename ")
        ui._main_key(curses.KEY_BTAB)
        self.assertEqual((ui.selected, ui.draft, ui.cursor), (2, "second", 2))
        ui._main_key("[")
        ui._main_key("]")
        self.assertEqual((ui.draft, ui.cursor), ("se[]cond", 4))

        scroll = ui.scrolls[2] = dict(follow=True, line=80, total=100, visible=20)
        ui._main_key(curses.KEY_PPAGE)
        self.assertEqual((scroll["line"], scroll["follow"]), (62, False))
        ui._main_key("shift-up")
        self.assertEqual(scroll["line"], 61)
        ui._main_key("shift-right")
        self.assertEqual(scroll["left"], 8)
        ui._main_key("!")
        self.assertEqual((scroll["line"], scroll["follow"]), (61, False))
        ui._main_key(curses.KEY_HOME)
        self.assertEqual(ui.cursor, 0)
        ui._main_key("ctrl-home")
        self.assertEqual((scroll["line"], scroll["follow"]), (0, False))
        ui._main_key("ctrl-end")
        self.assertTrue(scroll["follow"])
        scroll.update(line=80)

        ui.output_rect = (30, 1, 70, 20)
        ui.session_hits = [(2, 30, 1), (3, 30, 2)]
        ui._main_key(("mouse", 35, 5, curses.BUTTON4_PRESSED))
        self.assertEqual((scroll["line"], scroll["follow"]), (77, False))
        ui._main_key(("mouse", 5, 5, curses.BUTTON4_PRESSED))
        self.assertEqual(scroll["line"], 77)
        ui._main_key(("mouse", 35, 5, curses.BUTTON5_PRESSED))
        self.assertEqual((scroll["line"], scroll["follow"]), (80, True))
        ui._main_key("shift-up")
        ui._main_key(("mouse", 5, 2, curses.BUTTON1_PRESSED))
        self.assertEqual(ui.selected, 1)
        ui._main_key(9)
        self.assertEqual((ui.draft, ui.cursor, scroll["line"], scroll["follow"]), ("se[]!cond", 0, 79, False))
        ui._main_key(3)
        ui._main_key(3)
        self.assertEqual(ui.draft, "")
        engine.request.assert_not_called()
        ui._command("/interrupt")
        engine.request.assert_called_once_with({"op": "interrupt", "session_id": 2})

        ui._set_draft("/scheduler")
        ui._main_key(13)
        self.assertIsNone(ui.selected)
        self.assertEqual(ui.draft, "")
        ui._main_key(curses.KEY_BTAB)
        self.assertEqual(ui.selected, 2)
        ui._command("/scheduler")
        self.assertIsNone(ui.selected)
        engine.request.assert_called_once_with({"op": "interrupt", "session_id": 2})
        ui._main_key(9)
        self.assertEqual(ui.selected, 1)

        ui.scrolls[1] = dict(follow=False, line=0, total=100, visible=20)
        ui.pending_keys.extend(["ctrl-end", "shift-up", "x"])
        ui._coalesce_scroll()
        self.assertEqual(ui.scrolls[1]["line"], 79)
        self.assertEqual(ui._getch(), "x")
        ui.window.get_wch.side_effect = ["\x1b", "z"]
        ui._coalesce_scroll()
        self.assertEqual([ui._getch(), ui._getch()], [27, "z"])

    def test_redraw_invalidation_and_prompt_reuse(self):
        screen = Screen(77, 24)
        screen.feed(b"output\r\n" * 50)
        session = dict(id=1, name="Example", session="mu-session", active=dict(mode="readonly"))
        engine = MagicMock()
        engine.state.return_value = dict(root="/work", sessions=[session], messages=[],
                                         workspace=dict(owner=None), scheduler=dict(active=None))
        engine.display.return_value = screen
        window = MagicMock()
        window.getmaxyx.return_value = (30, 110)
        with patch.multiple("muboard.ui.curses", raw=DEFAULT, nonl=DEFAULT, set_escdelay=DEFAULT,
                            typeahead=DEFAULT, mousemask=DEFAULT, mouseinterval=DEFAULT, has_colors=DEFAULT) as mocks:
            mocks["has_colors"].return_value = False
            ui = _UI(window, engine)
            ui._refresh = MagicMock()
            ui._draw_main()
            ui._draw_main()
            self.assertEqual(window.erase.call_count, 1)
            self.assertEqual(engine.display.call_count, 2)  # Poll replay completion even without painting.
            session["name"] = "Renamed"
            ui._draw_main()
            self.assertEqual(window.erase.call_count, 2)
            screen.feed(b"new output")
            ui._draw_main()
            self.assertEqual(window.erase.call_count, 3)
            session["active"] = None
            ui._draw_main()
            prompt = ui.prompt_screen
            ui._set_draft("draft")
            ui._draw_main()
            self.assertIs(ui.prompt_screen, prompt)
            window.getmaxyx.return_value = (30, 100)
            ui._draw_main()
            self.assertIsNot(ui.prompt_screen, prompt)


class TerminalSmoke(unittest.TestCase):
    def test_viewport_cache_and_history_anchor(self):
        screen = Screen(12, 3)
        screen.feed(b"line\r\n" * 20)
        _, total, first = screen.frame(2, 5)
        _, _, second = screen.frame(3, 5)
        self.assertIs(first[1], second[0])
        self.assertEqual(screen.frame(total - 2, 10)[0], total - 2)
        revision = screen.revision
        screen.feed(b"replacement\r\n" * 10001)
        self.assertGreater(screen.revision, revision)
        self.assertIsNot(screen.frame(3, 5)[2][0], second[0])
        screen.feed(b"\x1b[?1049h\x1b[2J\x1b[Halt")
        _, total, alternate = screen.frame(0, 5)
        self.assertEqual(total, 3)
        self.assertEqual("".join(cell[0] for cell in alternate[0]).strip(), "alt")

    def test_native_controls_events_and_unicode(self):
        prompt = Screen(60, 5)
        prompt.feed(prompt_bytes(live_prompt("# **literal**\n```", "/work", {})))
        self.assertIn("mu> # **literal**", prompt.core.get_line(1))
        self.assertTrue(prompt.core.get_line(2).startswith("```"))
        screen = Screen(32, 5)
        screen.feed(b"old progress\r\x1b[2K\x1b[94mdone\x1b[0m")
        self.assertTrue(screen.core.get_line(0).startswith("done"))
        self.assertNotIn("progress", screen.core.get_line(0))
        self.assertEqual(screen.color(screen.core.get_line_cells(0)[0][1]), 12)
        screen.feed(b"\r\n\x1b]0;worker title\x07\x1b]8;;https://example.com\x07link\x1b]8;;\x07")
        self.assertEqual(screen.links(), ["https://example.com"])
        self.assertEqual(screen.core.title(), "worker title")
        wide = "中".encode()
        screen.feed(b"\r\n" + wide[:1])
        screen.feed(wide[1:])
        self.assertTrue(screen.core.get_line_cells(2)[0][3].wide_char)
        self.assertTrue(screen.core.get_line_cells(2)[1][3].wide_char_spacer)
        screen.feed(b"\x1b[?1049h\x1b[2J\x1b[Halternate")
        self.assertTrue(screen.core.is_alt_screen_active())
        screen.feed(b"\x1b[?1049l")
        self.assertFalse(screen.core.is_alt_screen_active())
        self.assertTrue(screen.core.get_line(0).startswith("done"))
        cursor = screen.core.cursor_position()
        screen.feed(b"\x07")
        self.assertEqual(screen.core.cursor_position(), cursor)
        self.assertFalse(screen.core.poll_events())
        self.assertFalse(screen.core.drain_bell_events())
        screen.feed(b"\x1b]52;c;dGVzdA==\x07\x1b]52;c;?\x07\x1b[6n")
        self.assertFalse(screen.core.allow_clipboard_read())
        self.assertEqual(screen.core.drain_responses(), b"")

    def test_pty_drains_without_ui_and_keeps_complete_evidence(self):
        screen = Screen(30, 6)
        capture = Capture(screen)
        extra_slave = os.dup(capture.slave)
        process = None
        try:
            process = subprocess.Popen([sys.executable, "-c",
                "import os; assert os.isatty(1) and os.isatty(2); "
                "os.write(1, b'line\\n' * 15000); "
                "os.write(1, b'< \\t' + b'x' * 20000 + b'\\nFINAL\\x07')"],
                stdin=subprocess.DEVNULL, stdout=capture.slave, stderr=subprocess.STDOUT)
            capture.start()
            self.assertEqual(process.wait(timeout=15), 0)
            capture.finish()
            self.assertIsNone(capture.error)
            self.assertIsNone(screen.error)
            data = os.pread(capture.raw.fileno(), os.fstat(capture.raw.fileno()).st_size, 0)
            text = plain_output(data)
            self.assertIn("< \t" + "x" * 20000 + "\nFINAL", text)
            self.assertEqual(text.count("line\n"), 15000)
            self.assertLessEqual(screen.core.scrollback_len(), 10000)
            self.assertFalse(screen.core.drain_bell_events())
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if capture.thread is None or capture.thread.is_alive():
                capture.finish()
            os.close(extra_slave)
            capture.raw.close()


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

    def until(self, board, condition, seconds=15):
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

    def test_batched_snapshots_rotation_and_usage(self):
        board = self.board()
        board.scheduler_turn_limit = 100
        first = self.new(board, "read design; do not write")
        board.tick()
        self.assertIsNone(board._running(None))
        second = self.new(board, "read independent")
        self.until(board, lambda: board._running(None) is not None)
        snapshot = board._running(None)["snapshot"]
        self.assertEqual(len(snapshot["messages"]), 2)
        self.assertNotIn("models", snapshot)
        self.assertNotIn("next_model", snapshot["sessions"][0])
        self.until(board, board.idle)
        scheduler = board.data["scheduler"]
        old = scheduler["session"]
        prompts = self.journal(scheduler)["invocations"]
        self.assertGreaterEqual(len(prompts), 2)
        self.assertIn("You schedule messages", prompts[0]["prompt"])
        self.assertTrue(all("You schedule messages" not in p["prompt"] for p in prompts[1:]))
        self.assertEqual(first["last"]["summary"], "Handled: read design; do not write")
        self.assertNotIn("LIVE:", first["last"]["summary"])
        self.assertEqual(scheduler["usage_totals"]["input_tokens"], 100 * len(prompts))
        self.assertEqual(scheduler["last_usage"]["cache_read_input_tokens"], 60)
        self.assertEqual(scheduler["last_usage"]["reasoning_output_tokens"], 5)
        self.assertGreater(scheduler["last_usage"]["seconds"], 0)
        snapshot = board._scheduler_snapshot()
        payload = json.loads(board._scheduler_prompt(snapshot, bootstrap=False).split("SNAPSHOT:\n", 1)[1])
        self.assertTrue(payload["sessions"][0]["context"]["unchanged"])
        payload = json.loads(board._scheduler_prompt(snapshot).split("SNAPSHOT:\n", 1)[1])
        self.assertIn("turns", payload["sessions"][0]["context"])

        board.scheduler_turn_limit = scheduler["turns"]
        board.request(dict(op="send", session_id=first["id"], text="read follow-up"))
        self.until(board, lambda: board._running(None) is not None)
        snapshot = board._running(None)["snapshot"]
        self.assertNotEqual(scheduler["session"], old)
        self.assertIn(old, scheduler["previous_sessions"])
        self.assertTrue((self.root / ".mu/sessions" / f"{old}.jsonl").exists())
        context = snapshot["sessions"][0]["context"]
        self.assertEqual(context["turns"][0]["request"], "read design; do not write")
        self.assertEqual(context["turns"][0]["response"], first["last"]["summary"])
        self.until(board, board.idle)
        board.request(dict(op="send", session_id=first["id"], text="read third"))
        self.until(board, board.idle)
        snapshot = board._scheduler_snapshot()
        context = snapshot["sessions"][0]["context"]
        self.assertEqual(context["omitted_turns"], 1)
        self.assertEqual([t["request"] for t in context["turns"]], ["read follow-up", "read third"])
        result = subprocess.check_output([sys.executable, "-m", "muboard", "-C", str(self.root),
            "context", f"S{first['id']}", "--before", context["read"].split()[-1], "--turn", "t1"],
            env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual(json.loads(result)["turns"][0]["request"], "read design; do not write")
        board.close()
        reopened = self.board()
        self.assertEqual(reopened._scheduler_snapshot()["sessions"][0]["context"], context)
        self.assertEqual(len(self.journal(second)["invocations"]), 1)

    def test_canonical_responses_and_compaction_accounting(self):
        events = [dict(type="prompt_queued", prompt_id="q1", prompt=dict(text="Discuss only")),
                  dict(type="prompt_materialized", prompt_id="q1", turn_id="t1"),
                  dict(type="provider_requested", exchange_id="e1", turn_id="t1"),
                  dict(type="provider_completed", exchange_id="e1", projection=dict(kind="assistant", items=[
                      dict(type="text", text="Inspecting"), dict(type="bash_call", arguments="{}")])),
                  dict(type="provider_requested", exchange_id="e2", turn_id="t1"),
                  dict(type="provider_completed", exchange_id="e2", usage=dict(input_tokens=10, output_tokens=2),
                       projection=dict(kind="assistant", items=[dict(type="text", text="The actual answer")])),
                  dict(type="provider_requested", exchange_id="e3", turn_id="compact"),
                  dict(type="provider_completed", exchange_id="e3", usage=dict(input_tokens=20, output_tokens=3),
                       projection=dict(kind="assistant", items=[dict(type="text", text="Do not use this checkpoint")])),
                  dict(type="compaction_applied")]
        context = conversation(events)
        self.assertEqual(context["turns"], [dict(turn_id="t1", request="Discuss only", response="The actual answer")])
        usage = scheduler_usage(events)
        self.assertEqual((usage["requests"], usage["input_tokens"], usage["output_tokens"], usage["compactions"]), (3, 30, 5, 1))
        self.assertIn("omitted", excerpt("a" * 100, 20))
        board = self.board()
        session = board._mu("new")
        path = self.root / ".mu/sessions" / f"{session}.jsonl"
        end = path.stat().st_size
        with path.open("a") as stream:
            stream.write('{"type":')
        self.assertEqual(len(list(journal_events(self.root, session, before=end))), 1)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            list(journal_events(self.root, session))

    def test_evolving_names_and_user_ownership(self):
        board = self.board()
        auto = self.new(board, "read naming design")
        locked = board.store.session(board.request(dict(op="new", name="Session 2"))["session_id"])
        removed = board.store.session(board.request(dict(op="new"))["session_id"])

        def proposal(name):
            snapshot = json.loads(json.dumps(dict(board.state(), events=board.data["events"])))
            return dict(snapshot=snapshot, plan=dict(reason="Name topics", actions=[], names=[
                dict(session_id=auto["id"], name=name),
                dict(session_id=locked["id"], name="Must not replace user name"),
            ]))

        active = proposal("Session naming")
        active["plan"]["names"].append(dict(session_id=removed["id"], name="Removed"))
        active["plan"]["actions"] = [dict(type="dispatch", message_id=board.data["messages"][0]["id"], mode="readonly")]
        board.request(dict(op="remove", session_id=removed["id"]))
        with patch.object(board, "_spawn") as spawn:
            board._apply_plan(active)
            spawn.assert_called_once()
        self.assertEqual(auto["name"], "Session naming")
        self.assertEqual(locked["name"], "Session 2")
        self.assertEqual(auto["name_source"], "auto")

        board.request(dict(op="interrupt", session_id=auto["id"]))
        revision = auto["revision"]
        pending = json.loads(json.dumps(board.data["messages"]))
        board._apply_plan(proposal("Naming implementation"))
        self.assertEqual(auto["name"], "Naming implementation")
        self.assertTrue(auto["hold"])
        self.assertEqual(auto["revision"], revision)
        self.assertEqual(board.data["messages"], pending)

        active = proposal("Stale automatic name")
        board.request(dict(op="rename", session_id=auto["id"], name="My title"))
        board._apply_plan(active)
        self.assertEqual(auto["name"], "My title")
        events = list(board.data["events"])
        board.request(dict(op="rename", session_id=auto["id"], name=None))
        self.assertEqual(board.data["events"], events)
        board._apply_plan(proposal("Automatic again"))
        saved = read_state(self.root)["sessions"][0]
        self.assertEqual((saved["name"], saved["name_source"]), ("Automatic again", "auto"))
        for invalid in ("", "two\nlines", "x" * 81):
            with self.assertRaises(ValueError):
                board._validate_plan(proposal(invalid)["plan"], proposal(invalid))

    def test_legacy_names_are_user_owned(self):
        board = self.board()
        session = self.new(board, "read legacy")
        del session["name_source"]
        board.store.save()
        saved = read_state(self.root)["sessions"][0]
        self.assertEqual(saved["name_source"], "user")
        self.assertEqual(saved["name"], "Session 1")

    def test_fifo_late_messages_and_persistent_scheduler(self):
        self.config(scheduler_delay=0.12, worker_delay=0.15)
        board = self.board()
        raw = "  read first\n<example>α & β</example>\n\n"
        first = self.new(board, raw)
        self.until(board, lambda: board._running(None) is not None)
        second = self.new(board, "read independent")
        board.request(dict(op="send", session_id=first["id"], text="read second"))
        saved = read_state(self.root)
        self.assertEqual(len(saved["messages"]), 3)
        self.until(board, lambda: board._running(first["id"]) is not None)
        screen = board.screens[first["id"]]
        _, _, rows = screen.frame(0, 100)
        rendered = "\n".join("".join(cell[0] for cell in row).rstrip() for row in rows)
        self.assertIn("fake/model ~42%", rendered)
        self.assertIn("mu> " + raw.rstrip(), rendered)
        self.assertNotIn("Live invocation", rendered)
        board.request(dict(op="send", session_id=first["id"], text="read late"))
        self.until(board, board.idle)
        self.assertFalse(board.data["messages"])
        turns = self.journal(first)["invocations"]
        self.assertEqual([t["prompt"] for t in turns],
                         [raw, "read second", "read late"])
        self.assertEqual(board.output(first["id"])["text"].count("mu> "), 3)
        _, _, rows = board.screens[first["id"]].frame(0, 10000)
        self.assertEqual("\n".join("".join(cell[0] for cell in row) for row in rows).count("mu> "), 3)
        self.assertTrue(all(t["tty"] for t in turns))
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
            self.assertEqual(turns[0]["prompt"], "write one" if session is writer1 else "write two")
            self.assertTrue(turns[1]["prompt"].startswith("<system-request>\n"))
            self.assertTrue(turns[1]["prompt"].endswith("\n</system-request>"))

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
        self.assertEqual(board.output(session["id"])["text"].count("mu> "), 1)
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

    def test_session_models(self):
        board = self.board()
        first = board.request(dict(op="new", model="fake/other"))["session_id"]
        second = board.request(dict(op="new"))["session_id"]
        self.assertEqual([s["next_model"] for s in board.state()["sessions"]], ["fake/other", "fake/model"])
        board.request(dict(op="set_models", models=dict(worker="fake/model:low")))
        board.request(dict(op="send", session_id=first, text="read hold"))
        session = board.store.session(first)
        self.until(board, lambda: board._running(first) is not None and self.journal(session)["active"]["busy"])
        run = board._running(first)["record"]
        args = self.journal(session)["invocations"][0]["args"]
        self.assertEqual(args[args.index("-m") + 1], "fake/other")
        board.request(dict(op="set_session_model", session_id=first, model="fake/model:high"))
        self.assertEqual(run["model"], "fake/other")
        self.assertEqual(board.state()["sessions"][0]["next_model"], "fake/model:high")
        self.assertIsNone(board.store.session(second)["model"])
        board.close()
        reopened = self.board()
        self.assertEqual(reopened.store.session(first)["model"], "fake/model:high")
        reopened.request(dict(op="set_session_model", session_id=first, model=None))
        self.assertEqual(reopened.state()["sessions"][0]["next_model"], "fake/model:low")
        self.assertTrue(reopened.store.session(first)["hold"])

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
        diagnostic = "[mu] compacted epoch 0 → 1"
        self.config(scheduler_fail=True, scheduler_stderr=diagnostic, scheduler_delay=0.2)
        board = self.board()
        session = self.new(board, "read queued")
        self.until(board, lambda: diagnostic in board.output(None)["text"])
        active = board._running(None)
        self.assertIsNotNone(active)
        self.until(board, board.idle)
        self.assertTrue(active["output"].closed)
        self.assertTrue(active["stderr"].closed)
        old = board.data["scheduler"]["session"]
        self.assertIn(diagnostic, board.data["scheduler"]["error"])
        self.assertIn(diagnostic, board.output(None)["text"])
        _, _, rows = board.screens[None].frame(0, 10000)
        self.assertIn(diagnostic, "\n".join("".join(cell[0] for cell in row) for row in rows))
        self.new(board, "read also queued")
        board.tick()
        self.assertFalse(board.active)
        self.config(scheduler_stdout="not JSON", scheduler_stderr=json.dumps(dict(
            reason="Not a stdout decision", actions=[dict(type="dispatch", message_id=1, mode="readonly")])))
        board.request(dict(op="schedule"))
        self.until(board, board.idle)
        self.assertIn("Invalid scheduler decision", board.data["scheduler"]["error"])
        self.assertIsNone(session["session"])
        self.assertTrue(all(m["state"] == "pending" for m in board.data["messages"]))
        self.config(scheduler_stderr=diagnostic)
        board.request(dict(op="schedule"))
        self.until(board, board.idle)
        self.assertNotEqual(board.data["scheduler"]["session"], old)
        self.assertIsNone(board.data["scheduler"]["error"])
        self.assertIn(diagnostic, board.output(None)["text"])
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

    def test_history_preparation_gate_and_failure(self):
        board = self.board()
        session = self.new(board, "read gated")
        session["session"] = board._mu("new")
        data = self.journal(session)
        data["transcript"] = "Prior history\n"
        (self.root / ".mu/fake" / (session["session"] + ".json")).write_text(json.dumps(data))
        future = Future()
        with patch.object(board.replay_pool, "submit", return_value=future):
            active = board._spawn(session=session, message=board.data["messages"][0], action="message")
        self.assertEqual(self.journal(session)["invocations"], [])
        self.assertIsNotNone(active["release"])
        future.set_exception(RuntimeError("Replay fixture failed"))
        board._prepare(active)
        self.until(board, board.idle)
        self.assertEqual(session["gate"], "failed")
        self.assertIn("Replay fixture failed", session["last"]["summary"])
        self.assertEqual(self.journal(session)["invocations"], [])
        self.assertEqual(len(board.data["messages"]), 1)

    def test_crash_recovery_holds_dirty_work_and_retains_mailbox(self):
        board = self.board()
        session = self.new(board, "write hold")
        self.until(board, lambda: any(self.root.glob("work-*.txt")))
        active = board._running(session["id"])
        active["process"].kill()
        active["process"].wait()
        active["capture"].finish()
        active["output"].close()
        board.active.clear()
        board.replay_pool.shutdown(wait=True, cancel_futures=True)
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
        process = subprocess.Popen([sys.executable, "-m", "muboard", "-C", str(self.root), "--mu", str(FAKE)],
                                   cwd=ROOT,
                                   stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ, TERM="xterm-256color"),
                                   start_new_session=True)
        os.close(slave)
        screen = bytearray()
        rendered = Screen(110, 30)

        def visible():
            return "\n".join("".join(cell[0] for cell in rendered.core.get_line_cells(row)) for row in range(30))

        def pump(seconds=0.1):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.02)[0]:
                    try:
                        data = os.read(master, 65536)
                        screen.extend(data)
                        rendered.feed(data)
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
            self.fail(visible() + "\n" + screen[-3000:].decode(errors="replace"))

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
            send("/quit\r")
            wait(lambda: "Stop work and quit?" in visible() and "cancel" in visible())
            send("n")
            self.assertIsNone(process.poll())
            send("discard this draft\x03")
            self.assertFalse(state()["sessions"][0]["hold"])
            send("\x03")
            self.assertFalse(state()["sessions"][0]["hold"])
            send("/interrupt\r")
            wait(lambda: state()["sessions"][0]["hold"] and not any(r["kind"] == "worker" for r in state()["inflight"]))
            wait(lambda: not state()["inflight"] and not state()["events"])
            send("/new Exit check\r")
            wait(lambda: "To S2" in visible())
            send("read ho\t")
            wait(lambda: "To S1" in visible())
            send("/ren\x1b[Z")
            wait(lambda: "To S2" in visible() and "> read ho" in visible())
            send("\x1b[Z")
            wait(lambda: "To S1" in visible() and "> /ren" in visible())
            send("\x1bOC")
            wait(lambda: "> /rename " in visible())
            send("\x03\t")
            wait(lambda: "To S2" in visible())
            send("\x1b[<0;5;3M\x1b[<0;5;3m")
            wait(lambda: "To S1" in visible())
            send("\x1b[<64;40;6M")
            wait(lambda: "History" in visible())
            send("\x1b[1;5F")
            wait(lambda: "History" not in visible())
            send("\x1b[<0;5;4M\x1b[<0;5;4m")
            wait(lambda: "To S2" in visible() and "> read ho" in visible())
            send("\x1b[1;2A\x1b[1;2B\x1b[1;2C\x1b[1;2D\x1b[1;5H\x1b[1;5F\x1b[5~\x1b[6~")
            send("ld\r")
            wait(lambda: any(r["kind"] == "worker" for r in state()["inflight"]))
            self.assertEqual(state()["messages"][-1]["text"], "read hold")
            running = list(state()["inflight"])
            send("/quit\r")
            wait(lambda: "Stop work and quit?" in visible() and "cancel" in visible())
            send("y")
            wait(lambda: process.poll() is not None)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(len(state()["messages"]), 3)
            self.assertFalse(state()["inflight"])
            self.assertTrue(all(not owned_members(run) for run in running))
            self.assertGreater(screen.count(b"\x1b[?2026h"), 0)
            self.assertEqual(screen.count(b"\x1b[?2026h"), screen.count(b"\x1b[?2026l"))
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                pump(0.5)
                process.wait(timeout=8)
            os.close(master)


if __name__ == "__main__":
    unittest.main(verbosity=2)
