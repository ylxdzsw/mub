#!/usr/bin/env python3
"""Essential invariants, using only fake Mu processes and temporary worktrees."""
import fcntl
import curses
from concurrent.futures import Future
import hashlib
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

from muboard.engine import Engine, SCHEDULER_SCHEMA, owned_members
from muboard.ipc import ControlServer
from muboard.output import conversation, excerpt, journal_events, live_prompt, plain_output, prompt_bytes, scheduler_usage
from muboard.terminal import Capture, Screen
from muboard.state import read_state
from muboard.ui import _UI


class InputSmoke(unittest.TestCase):
    def test_steer_command_retains_draft_until_confirmed_and_accepted(self):
        ui = _UI.__new__(_UI)
        session = dict(id=1, gate="interrupted", active=None)
        ui.selected, ui.ui_error = 1, ""
        ui.state = dict(sessions=[session], messages=[], scheduler={})
        ui.drafts = {}
        ui.engine = MagicMock()
        ui.engine.state.return_value = ui.state
        ui._clear_close_warning = MagicMock()
        ui._confirm = MagicMock(return_value=False)
        draft = "/steer Use the existing schema instead."
        ui._set_draft(draft)
        ui._submit()
        ui.engine.request.assert_not_called()
        self.assertEqual(ui.draft, draft)
        ui._confirm.return_value = True
        ui.engine.request.side_effect = ValueError("Steering is unavailable")
        ui._submit()
        self.assertEqual(ui.draft, draft)
        self.assertEqual(ui.ui_error, "Steering is unavailable")
        ui.engine.request.side_effect = None
        ui.engine.request.return_value = dict(message_id=2, steering="ready")
        ui._submit()
        ui.engine.request.assert_called_with(dict(op="steer", session_id=1,
                                                 text="Use the existing schema instead."))
        self.assertEqual(ui.draft, "")
        ui._set_draft("/steer")
        ui.engine.request.reset_mock()
        ui._submit()
        ui.engine.request.assert_not_called()
        self.assertEqual(ui.draft, "/steer")

    def test_notices_only_show_attention_required_states(self):
        ui = _UI.__new__(_UI)
        session = dict(id=1, gate="interrupted", retry_authorized=True,
                       reason="Resume authorized; awaiting scheduler decision.")
        ui.state = dict(sessions=[session], scheduler=dict(reason="Normal scheduling decision"))
        ui.selected, ui.ui_error = 1, ""
        self.assertEqual(ui._session_status(session), "resume authorized")
        self.assertEqual(ui._notice(), "")
        session["blocked"] = "Inspection scope conflict"
        self.assertEqual(ui._session_status(session), "blocked")
        self.assertEqual(ui._notice(), "Blocked: Inspection scope conflict")
        session.update(blocked=None, retry_authorized=False,
                       active=dict(action="retry", mode="readwrite"))
        self.assertEqual(ui._session_status(session), "writing")
        self.assertEqual(ui._notice(), "")
        session["scheduling"] = dict(action=dict(type="continue_turn", execution_policy="write", reason="Build task-owned output"),
                                     applied=True, detail="")
        self.assertEqual(ui._notice(), "")
        session["scheduling"].update(applied=False, detail="Writer slot unavailable")
        self.assertEqual(ui._notice(), "")
        session.update(active=None, gate=None, reason="")
        self.assertEqual(ui._notice(), "")
        for gate in ("failed", "trapped", "interrupted"):
            with self.subTest(gate=gate):
                session.update(gate=gate, reason=f"Mu {gate}.")
                self.assertEqual(ui._notice(), session["reason"])
        session.update(hold=True, reason="Inspect before resuming")
        self.assertEqual(ui._notice(), session["reason"])
        session.update(hold=False, gate="redirect", reason="Redirect pending; awaiting runtime admission.")
        self.assertEqual(ui._notice(), "")
        session.update(active=dict(action="redirect"), steering=dict(phase="stopping"),
                       reason="Steering requested; waiting for Mu's safe boundary.")
        self.assertEqual(ui._notice(), "")
        ui.selected = None
        self.assertEqual(ui._notice(), "")
        ui.state["scheduler"]["error"] = "Cannot apply decision"
        self.assertIn("Scheduler held: Cannot apply decision", ui._notice())
        ui.ui_error = "Close needs confirmation"
        self.assertEqual(ui._notice(), ui.ui_error)

    def test_dirty_close_requires_currently_displayed_warning(self):
        sessions = [dict(id=1, name="Dirty", active=None), dict(id=2, name="Other", active=None)]
        engine = MagicMock(done=False)
        engine.state.return_value = dict(root="/work", sessions=sessions, messages=[],
                                         workspace=dict(owner=1, clean=False), scheduler=dict(active=None))
        engine.display.return_value = None
        window = MagicMock()
        window.getmaxyx.return_value = (30, 110)
        with patch.multiple("muboard.ui.curses", raw=DEFAULT, nonl=DEFAULT, set_escdelay=DEFAULT,
                            typeahead=DEFAULT, mousemask=DEFAULT, mouseinterval=DEFAULT,
                            has_colors=DEFAULT, curs_set=DEFAULT) as mocks, patch("muboard.ui.os.write"):
            mocks["has_colors"].return_value = False
            ui = _UI(window, engine)
            ui._command("/close")
            warning = ui.close_warning
            ui._main_key(4)  # A warning not yet painted cannot confirm.
            engine.request.assert_not_called()
            ui._draw_main()
            self.assertEqual(ui.displayed_notice, warning)
            ui._select(2)
            ui._select(1)
            ui._draw_main()
            self.assertIsNone(ui.close_warning)

            for hide in ("error", "dialog", "resize"):
                with self.subTest(hide=hide):
                    ui._main_key(4)
                    engine.request.assert_not_called()
                    ui._draw_main()
                    self.assertEqual(ui.displayed_notice, warning)
                    if hide == "error":
                        ui.ui_error = "Other notice"
                        ui._draw_main()
                    elif hide == "dialog":
                        with patch.object(ui, "_getch", return_value=27):
                            ui._info("Information", ["Example"])
                    else:
                        window.getmaxyx.return_value = (5, 110)
                        ui._draw_main()
                        window.getmaxyx.return_value = (30, 110)
                    ui._draw_main()
                    self.assertIsNone(ui.close_warning)

            ui._main_key(4)
            engine.request.assert_not_called()
            ui._draw_main()
            def commit(request):
                sessions[0]["active"] = dict(mode="readwrite", close_messages=[])
                return dict(committing=True)
            engine.request.side_effect = commit
            ui._command("/close")
            engine.request.assert_called_once_with(dict(op="commit_close", session_id=1, discard=False))
            ui._main_key(4)
            self.assertEqual(engine.request.call_count, 1)
            self.assertEqual(ui._notice(), "")
            sessions.pop(0)
            ui._tick()
            self.assertEqual(ui.selected, 2)
            self.assertNotIn(1, ui.drafts)
            self.assertNotIn(1, ui.scrolls)

    def test_composer_overflow_counts(self):
        ui = _UI.__new__(_UI)
        ui.window = MagicMock()
        ui.state = {"sessions": [{"id": 1, "name": "Long session name " * 10}]}
        ui.selected = 1
        ui.colors = {}
        draft = "\n".join(str(i) for i in range(10))
        cases = [
            ("a\nb\nc\nd", 7, 4, 80, "", (3, 4)),
            (draft, 0, 4, 80, "↓ 6 more", (0, 3)),
            (draft, 10, 4, 80, "↑ 2 more · ↓ 4 more", (3, 3)),
            (draft, len(draft), 4, 80, "↑ 6 more", (3, 4)),
            ("a\nb\nc", 5, 1, 80, "↑ 2 more", (0, 4)),
            ("x" * 175, 175, 4, 40, "↑ 1 more", (3, 38)),
            ("x" * 175, 0, 4, 40, "↓ 1 more", (0, 3)),
            ("界" * 85, 85, 4, 40, "↑ 1 more", (3, 37)),
        ]
        for text, cursor, rows, width, overflow, position in cases:
            with self.subTest(rows=rows, width=width, overflow=overflow, cursor=cursor):
                ui.window.getmaxyx.return_value = (30, width)
                ui.window.addstr.reset_mock()
                ui.draft, ui.cursor = text, cursor
                self.assertEqual(ui._draw_composer(0, 1, rows, width), position)
                divider = [call.args for call in ui.window.addstr.call_args_list if call.args[0] == 0]
                badges = [call for call in divider if "more" in call[2]]
                self.assertEqual([call[2].strip() for call in badges], [overflow] if overflow else [])
                if badges:
                    label = divider[-1]
                    self.assertLessEqual(label[1] + len(label[2]), badges[0][1])
                self.assertEqual(ui.draft, text)

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

    def steerable(self, board, session):
        active = board._running(session["id"])
        return bool(active and self.journal(session)["active"]["busy"] and board._steer_available(active))

    def test_empty_exit_retires_scheduler_without_deleting_history(self):
        board = self.board()
        worker = self.new(board, "read design")
        self.until(board, board.idle)
        board.request(dict(op="set_models", models=dict(scheduler="fake/scheduler", worker="fake/worker")))
        board.request(dict(op="remove", session_id=worker["id"]))
        scheduler = board.data["scheduler"]
        scheduler["error"] = "Old scheduling failure"
        old = scheduler["session"]
        journals = {path: path.read_bytes() for path in (self.root / ".mu/sessions").glob("*.jsonl")}
        saved = {key: board.data[key] for key in ("models", "next_session", "next_message", "next_event")}
        board.close()
        state = read_state(self.root)
        self.assertEqual({key: state[key] for key in saved}, saved)
        self.assertEqual(state["scheduler"], dict(session=None, error=None, reason="",
            previous_sessions=[old], **{key: scheduler[key] for key in
                                       ("last_usage", "recent_usage", "usage_totals")}))
        self.assertEqual(state["events"], [])
        self.assertEqual({path: path.read_bytes() for path in journals}, journals)
        reopened = self.board()
        reopened.tick()
        self.assertTrue(reopened.idle())
        self.assertIsNone(reopened.data["scheduler"]["session"])
        self.new(reopened, "read fresh work")
        self.until(reopened, reopened.idle)
        self.assertNotEqual(reopened.data["scheduler"]["session"], old)
        prompt = self.journal(reopened.data["scheduler"])["invocations"][0]["prompt"]
        self.assertIn("You schedule messages", prompt)
        self.assertNotIn("read design", prompt)

    def test_exit_preserves_scheduler_with_remaining_state(self):
        for remaining in ("sessions", "messages", "inflight", "owner"):
            with self.subTest(remaining=remaining):
                directory = self.root / remaining
                subprocess.run(["git", "init", "-q", str(directory)], check=True)
                board = self.board(directory)
                board.data["scheduler"].update(reason="Keep this decision", error="Needs recovery", turns=3)
                board.store.event("recheck")
                if remaining == "sessions":
                    board.store.new_session()
                elif remaining == "owner":
                    board.data[remaining] = 1
                else:
                    board.data[remaining] = [dict(id=1)]
                board.close()
                state = read_state(directory)
                self.assertEqual(state["scheduler"]["reason"], "Keep this decision")
                self.assertEqual(state["scheduler"]["error"], "Needs recovery")
                self.assertEqual(state["scheduler"]["turns"], 3)
                self.assertTrue(state["events"])

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
        self.assertEqual(self.journal(scheduler)["new_args"], ["new", "--no-context"])
        self.assertTrue(all("--no-context" in p["args"] for p in prompts))
        for invocation in prompts:
            args = invocation["args"]
            self.assertEqual(json.loads(args[args.index("--output-schema") + 1]), SCHEDULER_SCHEMA)
        variants = SCHEDULER_SCHEMA["properties"]["actions"]["items"]["anyOf"]
        self.assertEqual(set(SCHEDULER_SCHEMA["properties"]), {"summary", "actions"})
        self.assertEqual(set(SCHEDULER_SCHEMA["required"]), {"summary", "actions"})
        expected_fields = {
            "dispatch_message": {"type", "message_id", "execution_policy", "reason"},
            "continue_turn": {"type", "session_id", "execution_policy", "reason"},
            "request_commit": {"type", "session_id", "reason"},
            "set_wait_reason": {"type", "session_id", "wait_reason"},
            "mark_failed": {"type", "session_id", "reason"},
            "rename_session": {"type", "session_id", "name"},
        }
        self.assertEqual({v["properties"]["type"]["enum"][0] for v in variants}, set(expected_fields))
        schemas = [SCHEDULER_SCHEMA, *variants]
        for schema in schemas:
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for variant in variants:
            action = variant["properties"]["type"]["enum"][0]
            self.assertEqual(set(variant["properties"]), expected_fields[action])
        self.assertEqual(SCHEDULER_SCHEMA["properties"]["summary"]["type"], "string")
        self.assertEqual(SCHEDULER_SCHEMA["properties"]["actions"]["type"], "array")
        for variant in variants:
            if variant["properties"]["type"]["enum"][0] in ("dispatch_message", "continue_turn"):
                self.assertEqual(set(variant["properties"]["execution_policy"]["enum"]),
                                 {"readonly", "write", "unrestricted"})
        self.assertNotIn("names", SCHEDULER_SCHEMA["properties"])
        self.assertNotIn("steer", {v["properties"]["type"]["enum"][0] for v in variants})
        wait_schema = next(v for v in variants if v["properties"]["type"]["enum"] == ["set_wait_reason"])
        self.assertEqual(wait_schema["properties"]["wait_reason"], {"type": ["string", "null"]})
        self.assertNotIn("scheduling", snapshot["sessions"][0])
        self.assertIn("40 characters", board._scheduler_policy())
        for worker in (first, second):
            self.assertEqual(self.journal(worker)["new_args"], ["new"])
            self.assertTrue(all("--no-context" not in p["args"] for p in self.journal(worker)["invocations"]))
            self.assertTrue(all("--output-schema" not in p["args"] for p in self.journal(worker)["invocations"]))
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
        self.assertTrue(payload["sessions"][0]["history"]["unchanged_since_previous_snapshot"])
        self.assertNotIn("revision", payload["sessions"][0])
        self.assertEqual(payload["messages"], snapshot["messages"])
        self.assertEqual(set(payload), {"sessions", "messages", "workspace", "trigger_events"})
        payload = json.loads(board._scheduler_prompt(snapshot).split("SNAPSHOT:\n", 1)[1])
        self.assertIn("turns", payload["sessions"][0]["history"])

        board.scheduler_turn_limit = scheduler["turns"]
        board.request(dict(op="send", session_id=first["id"], text="read follow-up"))
        self.until(board, lambda: board._running(None) is not None)
        snapshot = board._running(None)["snapshot"]
        self.assertNotEqual(scheduler["session"], old)
        self.assertEqual(self.journal(scheduler)["new_args"], ["new", "--no-context"])
        self.assertIn(old, scheduler["previous_sessions"])
        self.assertTrue((self.root / ".mu/sessions" / f"{old}.jsonl").exists())
        context = snapshot["sessions"][0]["history"]
        self.assertEqual(context["turns"][0]["request"], "read design; do not write")
        self.assertEqual(context["turns"][0]["response"], first["last"]["summary"])
        self.until(board, board.idle)
        board.request(dict(op="send", session_id=first["id"], text="read third"))
        self.until(board, board.idle)
        snapshot = board._scheduler_snapshot()
        context = snapshot["sessions"][0]["history"]
        self.assertEqual(context["omitted_turn_count"], 1)
        self.assertEqual([t["request"] for t in context["turns"]], ["read follow-up", "read third"])
        result = subprocess.check_output([sys.executable, "-m", "muboard", "-C", str(self.root),
            "context", f"S{first['id']}", "--before", context["read_command"].split()[-1], "--turn", "t1"],
            env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual(json.loads(result)["turns"][0]["request"], "read design; do not write")
        board.close()
        reopened = self.board()
        self.assertEqual(reopened._scheduler_snapshot()["sessions"][0]["history"], context)
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
                  dict(type="compaction_started", turn_id="compact", mode="await_user"),
                  dict(type="provider_requested", exchange_id="e3", turn_id="compact"),
                  dict(type="provider_completed", exchange_id="e3", usage=dict(input_tokens=20, output_tokens=3),
                       projection=dict(kind="assistant", items=[dict(type="text", text="Do not use this checkpoint")])),
                  dict(type="compaction_applied", seq=10)]
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

    def test_final_response_follows_compaction_continuations(self):
        events = [dict(type="prompt_queued", prompt_id="q1", prompt=dict(text="Implement the change")),
                  dict(type="prompt_materialized", prompt_id="q1", turn_id="t1")]
        active_turn = "t1"
        for index, flags in enumerate((dict(resumable=True), dict(incomplete=True)), 1):
            events += [dict(type="provider_requested", exchange_id=f"partial{index}", turn_id=active_turn),
                       dict(type="provider_completed", exchange_id=f"partial{index}", projection=dict(
                           kind="assistant", items=[dict(type="text", text="Partial answer")], **flags)),
                       dict(type="compaction_started", turn_id=f"compact{index}", mode="continue_turn"),
                       dict(type="provider_requested", exchange_id=f"checkpoint{index}", turn_id=f"compact{index}"),
                       dict(type="provider_completed", exchange_id=f"checkpoint{index}", projection=dict(
                           kind="assistant", items=[dict(type="text", text="Checkpoint prose")]))]
            sequence = len(events) + 1
            events.append(dict(type="compaction_applied", seq=sequence))
            active_turn = f"t{sequence}"
            self.assertEqual(conversation(events)["turns"][0]["response"], "")
        prefix = len(events)
        events += [dict(type="provider_requested", exchange_id="final", turn_id=active_turn),
                   dict(type="provider_completed", exchange_id="final", projection=dict(
                       kind="assistant", items=[dict(type="text", text="Implemented and verified")]))]
        self.assertEqual(conversation(events, limit=1), dict(omitted_turns=0, turns=[
            dict(turn_id="t1", request="Implement the change", response="Implemented and verified")]))
        self.assertEqual(conversation(events[:prefix])["turns"][0]["response"], "")

    def test_existing_scheduler_keeps_its_session(self):
        board = self.board()
        old = board._mu("new")
        scheduler = board.data["scheduler"]
        scheduler.update(session=old, policy_hash=hashlib.sha256(board._scheduler_policy().encode()).hexdigest())
        board.request(dict(op="schedule"))
        self.until(board, board.idle)
        self.assertIsNone(scheduler["error"])
        self.assertEqual(scheduler["session"], old)
        self.assertEqual(self.journal(scheduler)["new_args"], ["new"])
        self.assertIn("--no-context", self.journal(scheduler)["invocations"][0]["args"])

    def test_evolving_names_and_user_ownership(self):
        board = self.board()
        auto = self.new(board, "read naming design")
        locked = board.store.session(board.request(dict(op="new", name="Session 2"))["session_id"])
        removed = board.store.session(board.request(dict(op="new"))["session_id"])

        def proposal(name):
            snapshot = board._scheduler_snapshot()
            return dict(snapshot=snapshot, plan=dict(summary="Name topics", actions=[
                dict(type="rename_session", session_id=auto["id"], name=name),
                dict(type="rename_session", session_id=locked["id"], name="Must not replace user name"),
            ]))

        active = proposal("Session naming")
        active["plan"]["actions"].append(dict(type="rename_session", session_id=removed["id"], name="Removed"))
        active["plan"]["actions"].append(dict(type="dispatch_message", message_id=board.data["messages"][0]["id"],
                                                execution_policy="readonly", reason="Read the naming request"))
        board.request(dict(op="remove", session_id=removed["id"]))
        with patch.object(board, "_spawn") as spawn:
            board._apply_plan(active)
            spawn.assert_called_once()
        self.assertEqual(auto["name"], "Session naming")
        self.assertEqual(locked["name"], "Session 2")
        self.assertEqual(auto["name_source"], "auto")
        decision = board.data["scheduler"]["decision"]
        self.assertEqual(decision["summary"], "Name topics")
        dispatch_result = next(result for result in decision["actions"] if result["action"]["type"] == "dispatch_message")
        self.assertEqual(dispatch_result["session_id"], auto["id"])
        self.assertTrue(dispatch_result["applied"])
        self.assertEqual(dispatch_result["action"]["reason"], "Read the naming request")
        self.assertEqual(auto["scheduling"], dispatch_result)
        self.assertTrue(board.data["scheduler"]["reason"].startswith("Name topics"))
        self.assertIn("Skipped", board.data["scheduler"]["reason"])
        self.assertEqual(read_state(self.root)["scheduler"]["decision"], decision)
        self.assertEqual(next(s for s in read_state(self.root)["sessions"] if s["id"] == auto["id"])["scheduling"],
                         dispatch_result)
        self.assertNotIn("scheduling", board._scheduler_snapshot()["sessions"][0])
        snapshot = board._scheduler_snapshot()
        invalid = dict(summary="Duplicate work", actions=[
            dict(type="dispatch_message", message_id=board.data["messages"][0]["id"],
                 execution_policy="readonly", reason="Read it"),
            dict(type="set_wait_reason", session_id=auto["id"], wait_reason="Wait instead"),
        ])
        with self.assertRaises(ValueError) as rejected:
            board._validate_plan(invalid, dict(snapshot=snapshot))
        error = str(rejected.exception)
        for detail in (f"S{auto['id']}", "actions[0] (dispatch_message)", "actions[1] (set_wait_reason)", "Keep only one"):
            self.assertIn(detail, error)
        prompt = board._scheduler_prompt(snapshot, bootstrap=False, correction=error)
        self.assertIn(f"Rejection: {error}", prompt)
        self.assertIn("No actions from it were applied", prompt)
        invalid["actions"] = [
            dict(type="rename_session", session_id=auto["id"], name="First title"),
            dict(type="rename_session", session_id=auto["id"], name="Second title"),
        ]
        with self.assertRaisesRegex(ValueError, rf"S{auto['id']}: multiple rename_session actions"):
            board._validate_plan(invalid, dict(snapshot=snapshot))

        for reverse, wait_reason in ((False, "Waiting for prerequisite"), (True, "Waiting for prerequisite"), (False, None), (True, None)):
            with self.subTest(rename_wait_reverse=reverse, wait_reason=wait_reason):
                auto["name"] = f"Session {auto['id']}"
                rename = dict(type="rename_session", session_id=auto["id"], name="Naming while waiting")
                wait = dict(type="set_wait_reason", session_id=auto["id"], wait_reason=wait_reason)
                actions = [wait, rename] if reverse else [rename, wait]
                active = dict(snapshot=board._scheduler_snapshot(), plan=dict(summary="Name pending work", actions=actions))
                pending = json.loads(json.dumps(board.data["messages"]))
                revision = auto["revision"]
                with patch.object(board, "_spawn") as spawn:
                    board._apply_plan(active)
                spawn.assert_not_called()
                self.assertEqual(auto["name"], rename["name"])
                self.assertEqual(auto["blocked"], wait["wait_reason"])
                self.assertEqual(auto["revision"], revision + 1)
                self.assertEqual(board.data["messages"], pending)
                self.assertTrue(all(result["applied"] for result in board.data["scheduler"]["decision"]["actions"]))

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
        for invalid in ("", "two\nlines"):
            with self.assertRaises(ValueError):
                board._validate_plan(proposal(invalid)["plan"], proposal(invalid))
        long_title = proposal("x" * 120)
        board._validate_plan(long_title["plan"], long_title)

    def test_scheduler_block_clear_and_fail_actions(self):
        board = self.board()
        session = board.store.session(board.request(dict(op="new"))["session_id"])

        def apply(action, summary):
            board._apply_plan(dict(snapshot=board._scheduler_snapshot(),
                                   plan=dict(summary=summary, actions=[action])))

        apply(dict(type="set_wait_reason", session_id=session["id"], wait_reason="Waiting for review"), "Blocked")
        self.assertEqual(session["blocked"], "Waiting for review")
        self.assertEqual(session["scheduling"]["action"]["wait_reason"], "Waiting for review")
        self.assertIn("set_wait_reason — Waiting for review", board._decision_text())
        apply(dict(type="set_wait_reason", session_id=session["id"], wait_reason="Waiting for handoff"), "Replaced")
        self.assertEqual(session["blocked"], "Waiting for handoff")
        apply(dict(type="set_wait_reason", session_id=session["id"], wait_reason=None), "Cleared")
        self.assertIsNone(session["blocked"])
        self.assertIsNone(session["gate"])
        self.assertIn("set_wait_reason — clear waiting note", board._decision_text())
        self.assertIsNone(read_state(self.root)["sessions"][0]["blocked"])
        self.assertIsNone(read_state(self.root)["scheduler"]["decision"]["actions"][0]["action"]["wait_reason"])
        for invalid in ("", " \t\n", False, 0, [], {}):
            with self.subTest(wait_reason=invalid), self.assertRaisesRegex(ValueError, "wait_reason must be a nonempty string or null"):
                apply(dict(type="set_wait_reason", session_id=session["id"], wait_reason=invalid), "Invalid")
        apply(dict(type="mark_failed", session_id=session["id"], reason="Cannot continue safely"), "Failed")
        self.assertEqual(session["gate"], "failed")
        self.assertEqual(session["reason"], "Cannot continue safely")
        self.assertEqual(session["scheduling"]["action"]["reason"], "Cannot continue safely")
        session["blocked"] = "Obsolete note"
        apply(dict(type="set_wait_reason", session_id=session["id"], wait_reason=None), "Clear only the note")
        self.assertEqual(session["gate"], "failed")
        self.assertIsNone(session["blocked"])
        session.update(gate="trapped", last=dict(exit="trapped", summary="Trapped command"))
        board.traps[session["id"]] = "Trapped command"
        apply(dict(type="set_wait_reason", session_id=session["id"], wait_reason="Waiting for writer access"), "Wait")
        self.assertEqual(session["gate"], "trapped")
        session["retry_authorized"] = True
        reason = "Trapped command deletes unrelated user data despite an explicit restriction"
        apply(dict(type="mark_failed", session_id=session["id"], reason=reason), "Refuse continuation")
        self.assertEqual(session["gate"], "failed")
        self.assertEqual(session["reason"], reason)
        self.assertIsNone(session["blocked"])
        self.assertFalse(session["retry_authorized"])
        self.assertTrue(session["scheduling"]["applied"])
        self.assertEqual(session["last"]["exit"], "trapped")
        self.assertEqual(board.traps[session["id"]], "Trapped command")
        apply(dict(type="continue_turn", session_id=session["id"], execution_policy="readonly",
                   reason="Retry without user authorization"), "Refused")
        self.assertEqual(session["gate"], "failed")
        self.assertFalse(session["scheduling"]["applied"])
        self.assertIn("Continuation requires user authorization", board._decision_text())

    def test_scheduler_execution_clears_waiting_note(self):
        board = self.board()
        session = self.new(board, "read work")
        message = board.data["messages"][0]
        (self.root / "task.txt").write_text("Task-owned changes\n")
        board.data["owner"] = session["id"]
        board._workspace()
        rename = dict(type="rename_session", session_id=session["id"], name="Task topic")
        for kind in ("dispatch_message", "continue_turn", "request_commit"):
            action = dict(type=kind, reason="Continue work")
            action.update(dict(message_id=message["id"]) if kind == "dispatch_message" else dict(session_id=session["id"]))
            if kind != "request_commit":
                action["execution_policy"] = "readonly"
            for reverse in (False, True):
                with self.subTest(kind=kind, reverse=reverse):
                    session.update(blocked="Waiting", gate="failed" if kind == "continue_turn" else None,
                                   retry_authorized=kind == "continue_turn")
                    actions = [action, rename] if reverse else [rename, action]
                    active = dict(snapshot=board._scheduler_snapshot(), plan=dict(summary="Continue", actions=actions))
                    self.assertEqual(board._validate_plan(active["plan"], active), actions)
                    revision = session["revision"]
                    with patch.object(board, "_spawn") as spawn:
                        board._apply_plan(active)
                    spawn.assert_called_once()
                    self.assertEqual(spawn.call_args.kwargs["action"], {"dispatch_message": "message", "continue_turn": "retry", "request_commit": "commit"}[kind])
                    self.assertEqual(session["revision"], revision)
                    self.assertEqual([r["action"] for r in board.data["scheduler"]["decision"]["actions"]], actions)

        action = dict(type="dispatch_message", message_id=message["id"], execution_policy="readonly", reason="Read work")
        active = dict(snapshot=board._scheduler_snapshot(), plan=dict(summary="Continue", actions=[action]))
        session["revision"] += 1
        with patch.object(board, "_spawn") as spawn:
            board._apply_plan(active)
        spawn.assert_not_called()
        self.assertEqual(session["blocked"], "Waiting")
        active["snapshot"] = board._scheduler_snapshot()
        revision = session["revision"]
        board._apply_plan(active)
        self.assertIsNone(session["blocked"])
        self.assertEqual(session["revision"], revision + 1)
        self.assertIsNotNone(board._running(session["id"]))

    def test_scheduler_rejects_conflicting_action_combinations(self):
        board = self.board()
        session = self.new(board, "read work")
        active = dict(snapshot=board._scheduler_snapshot())
        wait = dict(type="set_wait_reason", session_id=session["id"], wait_reason="Waiting")
        clear = dict(wait, wait_reason=None)
        execute = [dict(type="continue_turn", session_id=session["id"], reason="Continue work", execution_policy="readonly")]
        execute += [dict(type=kind, session_id=session["id"], reason="Work decision") for kind in ("request_commit", "mark_failed")]
        execute += [dict(type="dispatch_message", message_id=board.data["messages"][0]["id"],
                         execution_policy="readonly", reason="Read work")]
        combinations = [[wait, clear], [wait, wait], [clear, clear], execute[:2]]
        combinations += [[note, action] for note in (wait, clear) for action in execute]
        for combination in combinations:
            for actions in (combination, list(reversed(combination))):
                with self.subTest(actions=actions):
                    with self.assertRaises(ValueError) as rejected:
                        board._apply_plan(dict(active, plan=dict(summary="Conflicting work", actions=actions)))
                    error = str(rejected.exception)
                    self.assertIn(f"S{session['id']}: conflicting actions", error)
                    for i, action in enumerate(actions):
                        self.assertIn(f"actions[{i}] ({action['type']})", error)
                    self.assertIn("Keep only one action other than rename_session per session", error)
                    self.assertEqual(session["revision"], active["snapshot"]["sessions"][0]["revision"])
                    self.assertIsNone(session["blocked"])

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
        self.assertTrue(all("--automated" in t["args"] for t in turns))
        self.assertEqual(len(self.journal(second)["invocations"]), 1)
        scheduler = self.journal(board.data["scheduler"])
        self.assertGreaterEqual(len(scheduler["invocations"]), 3)
        self.assertTrue(all("--automated" in t["args"] for t in scheduler["invocations"]))
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
            self.assertTrue(all("--no-context" not in t["args"] for t in turns))

    def test_explicit_steer_bypasses_pending_fifo_and_preserves_owner(self):
        board = self.board()
        original_text = "write hold original"
        session = self.new(board, original_text)
        self.until(board, lambda: self.steerable(board, session))
        run = board._running(session["id"])["record"]
        self.assertEqual(board.data["owner"], session["id"])
        self.assertEqual(sum(a["record"]["kind"] == "worker" for a in board.active.values()), 1)

        older_texts = ["read queued one\nkeep exactly", "read queued two & keep spaces  "]
        older_ids = [board.request(dict(op="send", session_id=session["id"], text=text))["message_id"]
                     for text in older_texts]
        replacement = "read steer correction\nkeep exact whitespace  "
        steer_id = board.request(dict(op="steer", session_id=session["id"], text=replacement))["message_id"]
        self.assertEqual(session["steering"]["message_id"], steer_id)
        self.assertEqual([m["id"] for m in board.data["messages"]],
                         [run["message_id"], *older_ids, steer_id])
        self.until(board, lambda: (session.get("last") or {}).get("exit") == "redirected"
                   and board._running(session["id"]) is not None
                   and board._running(session["id"])["record"]["action"] == "redirect")
        self.assertEqual(session["last"]["exit"], "redirected")
        self.assertEqual(session["last"]["code"], 0)
        self.assertFalse(session["last"]["clean"])
        self.assertFalse(run["stopping"])
        self.assertEqual(board.data["owner"], session["id"])
        self.assertFalse(board.workspace["clean"])

        self.until(board, board.idle)
        turns = self.journal(session)["invocations"]
        prompts = [turn["prompt"] for turn in turns
                   if turn["prompt"] and not turn["prompt"].startswith("<system-request>\n")]
        self.assertEqual(prompts, [original_text, replacement, *older_texts])
        self.assertEqual([m["id"] for m in board.data["messages"]], [])
        self.assertTrue(all(turn["args"][0] != "retry" for turn in turns))
        self.assertIsNone(board.data["owner"])

    def test_ordinary_messages_do_not_redirect_active_worker(self):
        self.config(scheduler_delay=0.05, worker_delay=0.5)
        board = self.board()
        session = self.new(board, "read current work")
        self.until(board, lambda: self.steerable(board, session))
        run_id = board._running(session["id"])["record"]["id"]
        scheduler = board.data["scheduler"]
        prior_turns = len(self.journal(scheduler)["invocations"])
        queued = board.request(dict(op="send", session_id=session["id"], text="read queued normally"))["message_id"]
        self.until(board, lambda: len(self.journal(scheduler)["invocations"]) > prior_turns
                   and board._running(None) is None)
        self.assertEqual(board._running(session["id"])["record"]["id"], run_id)
        self.assertIsNone(session.get("steering"))
        self.assertEqual([(m["id"], m["state"]) for m in board.data["messages"] if m["id"] == queued],
                         [(queued, "pending")])
        self.until(board, board.idle)
        prompts = [turn["prompt"] for turn in self.journal(session)["invocations"] if turn["prompt"]]
        self.assertEqual(prompts, ["read current work", "read queued normally"])

    def test_explicit_steer_bypasses_scheduler_error(self):
        board = self.board()
        session = self.new(board, "read hold before scheduler error")
        self.until(board, lambda: self.steerable(board, session))
        self.config(scheduler_fail=True, scheduler_delay=0.05)
        board.request(dict(op="schedule"))
        self.until(board, lambda: bool(board.data["scheduler"].get("error")) and board._running(None) is None)
        self.assertFalse(board.idle())
        replacement = "read explicit redirect despite scheduler error"
        board.request(dict(op="steer", session_id=session["id"], text=replacement))
        self.until(board, lambda: board._running(session["id"]) is not None
                   and board._running(session["id"])["record"]["action"] == "redirect")
        redirect = board._running(session["id"])["record"]
        self.assertEqual(redirect["mode"], "readonly")
        self.assertEqual(redirect["trap"], "reversible")
        self.assertTrue(board.data["scheduler"]["error"])
        self.assertLessEqual(sum(a["record"]["kind"] == "worker" and a["record"]["mode"] == "readwrite"
                                 for a in board.active.values()), 1)

    def test_steer_does_not_inherit_unrestricted_traps(self):
        board = self.board()
        session = self.new(board, "write hold with unrestricted policy")
        message_id = board.data["messages"][0]["id"]
        self.config(plan=dict(summary="Explicit unrestricted request", actions=[dict(
            type="dispatch_message", message_id=message_id, execution_policy="unrestricted", reason="Use the requested policy")]))
        self.until(board, lambda: self.steerable(board, session))
        original = board._running(session["id"])["record"]
        self.assertEqual((original["mode"], original["trap"]), ("readwrite", "off"))
        self.config()
        board.request(dict(op="steer", session_id=session["id"], text="read corrected request"))
        self.until(board, lambda: board._running(session["id"]) is not None
                   and board._running(session["id"])["record"]["action"] == "redirect")
        redirect = board._running(session["id"])["record"]
        self.assertEqual(redirect["mode"], "readwrite")
        self.assertEqual(redirect["trap"], "destructive")
        self.assertLessEqual(sum(a["record"]["kind"] == "worker" and a["record"]["mode"] == "readwrite"
                                 for a in board.active.values()), 1)
        self.until(board, board.idle)

    def test_explicit_redirect_retains_writer_admission(self):
        board = self.board()
        owner = board.store.new_session()
        session = board.store.new_session()
        session["last"] = dict(mode="readwrite")
        board.data["scheduler"]["error"] = "Held for user recovery"
        board.request(dict(op="steer", session_id=session["id"], text="read replacement"))
        with patch.object(board, "_spawn") as spawn:
            writer = dict(record=dict(kind="worker", mode="readwrite", session_id=owner["id"]))
            with patch.dict(board.active, writer=writer):
                board._admit_redirects()
                spawn.assert_not_called()
            (self.root / "unfinished.txt").write_text("Other session's work")
            board.data["owner"] = owner["id"]
            board._admit_redirects()
            spawn.assert_not_called()
            self.assertEqual(session["steering"]["phase"], "ready")
            self.assertIn("dirty workspace", session["blocked"])
            board.data["owner"] = session["id"]
            board._admit_redirects()
            spawn.assert_called_once_with(session=session, message=board.data["messages"][0],
                                          action="redirect", mode="readwrite")

    def test_interrupt_holds_redirect_until_resume(self):
        board = self.board()
        session = self.new(board, "read hold before interrupt")
        self.until(board, lambda: self.steerable(board, session))
        replacement = "read replacement after interrupt"
        redirect_id = board.request(dict(op="steer", session_id=session["id"], text=replacement))["message_id"]
        board.request(dict(op="interrupt", session_id=session["id"]))
        self.assertTrue(session["hold"])
        self.assertEqual(session["steering"]["phase"], "held")
        self.until(board, lambda: board._running(session["id"]) is None)
        self.until(board, board.idle)
        self.assertTrue(session["hold"])
        self.assertEqual(session["steering"]["phase"], "held")
        self.assertEqual(len(self.journal(session)["invocations"]), 1)
        redirect = next(m for m in board.data["messages"] if m["id"] == redirect_id)
        self.assertEqual((redirect["text"], redirect["state"]), (replacement, "pending"))

        result = board.request(dict(op="resume", session_id=session["id"]))
        self.assertEqual(result, dict(eligible=True, retry=False, steering="ready"))
        self.until(board, board.idle)
        turns = self.journal(session)["invocations"]
        self.assertEqual([turn["prompt"] for turn in turns], ["read hold before interrupt", replacement])
        self.assertTrue(all(turn["args"][0] != "retry" for turn in turns))

    def test_owner_restart_holds_redirect_until_resume(self):
        board = self.board()
        session = self.new(board, "read hold before restart")
        self.until(board, lambda: self.steerable(board, session))
        replacement = "read replacement after restart"
        redirect_id = board.request(dict(op="steer", session_id=session["id"], text=replacement))["message_id"]
        board.close()

        reopened = self.board()
        current = reopened.store.session(session["id"])
        self.assertTrue(current["hold"])
        self.assertEqual(current["steering"]["phase"], "held")
        self.assertEqual(len(self.journal(current)["invocations"]), 1)
        redirect = next(m for m in reopened.data["messages"] if m["id"] == redirect_id)
        self.assertEqual((redirect["text"], redirect["state"]), (replacement, "pending"))
        self.until(reopened, reopened.idle)
        self.assertEqual(len(self.journal(current)["invocations"]), 1)

        reopened.request(dict(op="resume", session_id=current["id"]))
        self.until(reopened, reopened.idle)
        turns = self.journal(current)["invocations"]
        self.assertEqual([turn["prompt"] for turn in turns], ["read hold before restart", replacement])
        self.assertTrue(all(turn["args"][0] != "retry" for turn in turns))

    def test_steer_is_rejected_when_mu_has_no_sigquit_handler(self):
        self.config(soft_interrupt=False)
        board = self.board()
        session = self.new(board, "read hold without soft handler")
        self.until(board, lambda: board._running(session["id"]) is not None
                   and self.journal(session)["active"]["busy"])
        self.assertFalse(board._steer_available(board._running(session["id"])))
        with self.assertRaisesRegex(ValueError, "Soft steering unavailable"):
            board.request(dict(op="steer", session_id=session["id"], text="read must not signal"))
        self.assertIsNone(session.get("steering"))
        self.assertEqual([m["text"] for m in board.data["messages"]], ["read hold without soft handler"])
        self.assertIsNotNone(board._running(session["id"]))

    def test_early_redirect_delivery_failure_retains_text_without_retry(self):
        board = self.board()
        original = "read hold original request"
        session = self.new(board, original)
        self.until(board, lambda: self.steerable(board, session))
        self.config(early_fail=True)
        replacement = "read exact redirect to retain"
        redirect_id = board.request(dict(op="steer", session_id=session["id"], text=replacement))["message_id"]
        self.until(board, lambda: session.get("steering", {}).get("phase") == "held"
                   and board._running(session["id"]) is None)
        self.assertEqual(sum(m["id"] == redirect_id and m["text"] == replacement
                             for m in board.data["messages"]), 1)
        self.assertEqual(next(m["state"] for m in board.data["messages"] if m["id"] == redirect_id), "pending")
        self.assertEqual(len(self.journal(session)["invocations"]), 1)

        self.config()
        board.request(dict(op="resume", session_id=session["id"]))
        self.until(board, board.idle)
        turns = self.journal(session)["invocations"]
        self.assertEqual([turn["prompt"] for turn in turns], [original, replacement])
        self.assertTrue(all(turn["args"][0] != "retry" for turn in turns))
        self.assertFalse(any(m["text"] == replacement for m in board.data["messages"]))

    def test_redirect_blockers_wait_for_explicit_recovery(self):
        board = self.board()
        session = self.new(board, "read hold while Mu is compacting")
        self.until(board, lambda: self.steerable(board, session))
        journal = self.root / ".mu/sessions" / f"{session['session']}.jsonl"
        with journal.open("a") as stream:
            stream.write(json.dumps(dict(type="prompt_queued", prompt_id="unmaterialized",
                                        prompt=dict(kind="text", text="older Mu input"))) + "\n")
            stream.write(json.dumps(dict(type="compaction_started", turn_id="compact", mode="await_user")) + "\n")
        replacement = "read after explicit compaction recovery"
        redirect_id = board.request(dict(op="steer", session_id=session["id"], text=replacement))["message_id"]
        self.until(board, lambda: session.get("steering", {}).get("phase") == "held"
                   and board._running(session["id"]) is None)
        self.assertIn("unfinished compaction or unmaterialized input", session["reason"])
        self.assertEqual(len(self.journal(session)["invocations"]), 1)
        self.assertTrue(any(m["id"] == redirect_id and m["text"] == replacement and m["state"] == "pending"
                            for m in board.data["messages"]))

        self.config(release_hold=True)
        result = board.request(dict(op="resume", session_id=session["id"]))
        self.assertEqual(result, dict(eligible=True, retry=True))
        self.assertEqual(session["steering"]["phase"], "recovery")
        self.until(board, board.idle)
        turns = self.journal(session)["invocations"]
        self.assertEqual(turns[0]["prompt"], "read hold while Mu is compacting")
        self.assertEqual(turns[1]["args"][0], "retry")
        self.assertEqual(turns[2]["prompt"], replacement)
        self.assertFalse(any(m["id"] == redirect_id for m in board.data["messages"]))

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
        self.assertEqual(turns[1]["args"][turns[1]["args"].index("--trap") + 1], "destructive")
        self.assertTrue(all("--automated" in t["args"] for t in turns))
        self.assertTrue(all("--no-context" not in t["args"] for t in turns))
        self.assertEqual(board.output(session["id"])["text"].count("mu> "), 1)
        snapshots = [json.loads(p.read_text()) for p in (self.root / ".mu/fake").glob("snapshot-*")]
        trapped = next(s for snap in snapshots for s in snap["sessions"] if s["continuation_state"] == "trapped")
        self.assertIn("complete stdin", trapped["last_run"]["trap_evidence"])
        board.request(dict(op="send", session_id=session["id"], text="read next"))
        self.until(board, board.idle)
        args = self.journal(session)["invocations"][-1]["args"]
        self.assertNotIn("retry", args)
        self.assertEqual(args[args.index("--trap") + 1], "reversible")

    def test_resume_preserves_trap_evidence_and_defers_execution(self):
        board = self.board()
        session = self.new(board, "trap write")
        self.until(board, lambda: board._running(session["id"]) is not None)
        self.config(plan=dict(summary="Inspection scope conflict", actions=[
            dict(type="set_wait_reason", session_id=session["id"], wait_reason="Needs reconsideration")]))
        self.until(board, board.idle)
        self.assertEqual(session["gate"], "trapped")
        evidence = board._scheduler_snapshot()["sessions"][0]["last_run"]["trap_evidence"]
        for gate in ("trapped", "interrupted"):
            session["gate"] = gate
            result = board.request(dict(op="resume", session_id=session["id"]))
            self.assertEqual(result, dict(eligible=True, retry=True))
            self.assertEqual(session["gate"], "trapped")
            self.assertTrue(session["retry_authorized"])
            self.assertIsNone(session["blocked"])
            self.assertIsNone(board._running(session["id"]))
            self.assertEqual(board._scheduler_snapshot()["sessions"][0]["last_run"]["trap_evidence"], evidence)
        self.until(board, board.idle)
        self.assertEqual(session["blocked"], "Needs reconsideration")
        self.assertEqual(len(self.journal(session)["invocations"]), 1)
        self.config()
        board.request(dict(op="resume", session_id=session["id"]))
        self.until(board, board.idle)
        self.assertEqual(session["last"]["exit"], "clean")
        self.assertIn("retry", self.journal(session)["invocations"][1]["args"])

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

    def test_commit_and_close_preserves_mailbox_until_success(self):
        self.config(commit_refused=True)
        board = self.board()
        session = self.new(board, "write unfinished")
        self.until(board, board.idle)
        key = session["id"]
        board.data["scheduler"]["error"] = "Paused for explicit close"
        message = board.request(dict(op="send", session_id=key, text="read queued"))["message_id"]
        with self.assertRaisesRegex(ValueError, "confirm discarding"):
            board.request(dict(op="commit_close", session_id=key))
        board.request(dict(op="commit_close", session_id=key, discard=True))
        active = board._running(key)
        self.assertEqual(active["record"]["close_messages"], [message])
        self.assertEqual(read_state(self.root)["inflight"][0]["close_messages"], [message])
        with self.assertRaisesRegex(ValueError, "finish"):
            board.request(dict(op="commit_close", session_id=key, discard=True))
        self.until(board, lambda: not board.active)
        self.assertIn(session, board.data["sessions"])
        self.assertEqual(board.data["owner"], key)
        self.assertEqual([m["id"] for m in board.data["messages"]], [message])
        self.assertIn("remains open", session["blocked"])
        self.assertEqual(len(self.journal(session)["invocations"]), 3)
        self.config()
        board.request(dict(op="commit_close", session_id=key, discard=True))
        self.until(board, lambda: session not in board.data["sessions"])
        self.assertNotIn(session, board.data["sessions"])
        self.assertTrue(board.workspace["clean"])
        self.assertEqual(board.data["messages"], [])
        self.assertTrue((self.root / ".mu/sessions" / f"{session['session']}.jsonl").exists())

    def test_commit_close_keeps_new_messages_and_failed_turns(self):
        self.config(commit_refused=True)
        board = self.board()
        session = self.new(board, "write unfinished")
        self.until(board, board.idle)
        key = session["id"]
        board.data["scheduler"]["error"] = "Paused for explicit close"
        for field, value in (("hold", True), ("gate", "interrupted")):
            session[field] = value
            with self.assertRaisesRegex(ValueError, "recovery"):
                board.request(dict(op="commit_close", session_id=key))
            session[field] = False if field == "hold" else None
        self.config(early_fail=True)
        board.request(dict(op="commit_close", session_id=key))
        self.until(board, lambda: not board.active)
        self.assertIn(session, board.data["sessions"])
        self.assertTrue(session["hold"])
        self.assertEqual(session["gate"], "failed")
        self.assertFalse(board.workspace["clean"])
        self.config(before_prompt_delay=0.2)
        board.request(dict(op="resume", session_id=key))
        board.request(dict(op="commit_close", session_id=key))
        message = board.request(dict(op="send", session_id=key, text="read new request"))["message_id"]
        self.until(board, lambda: not board.active)
        self.assertTrue(board.workspace["clean"])
        self.assertIn(session, board.data["sessions"])
        self.assertEqual([m["id"] for m in board.data["messages"]], [message])
        self.assertIn("New messages", session["blocked"])

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
            board._validate_plan(dict(summary="wrong", actions=[dict(type="dispatch_message", message_id=last_message["id"],
                                                                        execution_policy="readonly", reason="Skip FIFO")]), active)
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
        # Hard fail latches scheduler["error"]; board is not idle while messages are pending.
        self.until(board, lambda: not board.active and bool(board.data["scheduler"].get("error")))
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
        # A bad JSON response triggers one corrective pass; a second bad response latches.
        self.config(scheduler_stdout="not JSON")
        board.request(dict(op="schedule"))
        self.until(board, lambda: "rejected twice" in (board.data["scheduler"].get("error") or ""))
        self.assertFalse(board.active)
        self.assertTrue(all(m["state"] == "pending" for m in board.data["messages"]))
        self.config(scheduler_stderr=diagnostic)
        board.request(dict(op="schedule"))
        self.until(board, board.idle)
        self.assertNotEqual(board.data["scheduler"]["session"], old)
        self.assertIsNone(board.data["scheduler"]["error"])
        self.assertIn(diagnostic, board.output(None)["text"])
        self.assertEqual(session["last"]["exit"], "clean")

    def test_scheduler_error_is_not_idle_with_empty_mailbox(self):
        self.config(scheduler_fail=True)
        board = self.board()
        board.request(dict(op="schedule"))
        self.until(board, lambda: bool(board.data["scheduler"].get("error")) and not board.active)
        self.assertFalse(board.data["messages"])
        self.assertFalse(board.idle())
        self.config()
        board.request(dict(op="schedule"))
        self.until(board, board.idle)
        self.assertIsNone(board.data["scheduler"]["error"])

    def test_single_correction_recovers_bad_decision(self):
        # A first invalid decision spawns one corrective pass; if that succeeds,
        # the board resumes normally without user intervention.
        board = self.board()
        self.new(board, "read work")
        self.config(scheduler_stdout="not JSON")
        original_spawn = board._spawn
        corrections = []

        def spawn_with_valid_correction(*args, **kwargs):
            if kwargs.get("correction"):
                corrections.append(kwargs["correction"])
                self.config(scheduler_stdout=json.dumps(dict(summary="Corrected: wait", actions=[])))
            return original_spawn(*args, **kwargs)

        with patch.object(board, "_spawn", side_effect=spawn_with_valid_correction):
            self.until(board, lambda: len(corrections) == 1 and not board.active
                       and board.data["scheduler"].get("error") is None, seconds=10)
        self.assertEqual(len(corrections), 1)
        self.assertTrue(all(m["state"] == "pending" for m in board.data["messages"]))

    def test_repeated_bad_decision_latches_error(self):
        # Two consecutive bad responses latch the error and require manual /schedule.
        board = self.board()
        self.new(board, "read work")
        self.config(scheduler_stdout="not JSON")
        board.request(dict(op="schedule"))
        self.until(board, lambda: "rejected twice" in (board.data["scheduler"].get("error") or ""), seconds=10)
        self.assertFalse(board.active)
        # Board has a latched error; messages remain pending (not silently dropped).
        self.assertTrue(all(m["state"] == "pending" for m in board.data["messages"]))

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
            send("/model worker\r")
            wait(lambda: "Thinking: Inherited" in visible())
            send("\x1bOB\x1bOC\x1bOC ")
            wait(lambda: "fake/model:high@fast" in visible())
            self.assertIsNone(state()["models"]["worker"])
            send("\r")
            wait(lambda: state()["models"]["worker"] == "fake/model:high@fast")
            send("/model worker\r")
            wait(lambda: "fake/model:high@fast" in visible())
            send(" ")
            wait(lambda: "Fast: [ ] off" in visible())
            send("\x1b")
            wait(lambda: "Mu Board" in visible())
            self.assertEqual(state()["models"]["worker"], "fake/model:high@fast")
            send("/model worker\r")
            wait(lambda: "fake/model:high@fast" in visible())
            send("\x1bOB\x1bOD\x1bOC \r")
            wait(lambda: state()["models"]["worker"] == "fake/other")
            send("/model worker\r")
            wait(lambda: "Thinking: No effort options" in visible())
            send("\x1bOA\x1bOA\r")
            wait(lambda: state()["models"]["worker"] is None)
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
            process.wait(timeout=8)
            pump()
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
