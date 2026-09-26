"""Curses session UI for Mu Board."""

from __future__ import annotations

import curses
from concurrent.futures import ThreadPoolExecutor
import re
import sys
import time
import unicodedata

from .output import literal as _text, render_blocks


_ANSI = re.compile(r"(\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_]))")


def _width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _clip(text: str, width: int) -> str:
    result, used = [], 0
    for char in text:
        cells = _width(char)
        if used + cells > width:
            break
        result.append(char)
        used += cells
    return "".join(result)


def _lines(value, width: int) -> list[str]:
    """Wrap text to terminal cells while preserving explicit line breaks."""
    result, line, used = [], [], 0
    width = max(2, width)
    for char in _text(value):
        cells = _width(char)
        if char == "\n" or used + cells > width:
            result.append("".join(line))
            line, used = [], 0
        if char != "\n":
            line.append(char)
            used += cells
    result.append("".join(line))
    return result


class _UI:
    COMMANDS = {
        "/new [name]": "Create and select a session",
        "/rename <name>|--auto": "Name the selected session, or let its name evolve automatically",
        "/close": "Close the selected session",
        "/model [session|scheduler|worker|both]": "Show models, or choose a session/role model and effort",
        "/resume [selected]": "Resume the selected session",
        "/schedule": "Explicitly recheck or recover the scheduler",
        "/help": "Show commands and keyboard controls",
        "/quit": "Stop work and quit",
    }

    def __init__(self, window, engine, renderer):
        self.window, self.engine = window, engine
        self.renderer = renderer
        self.render_job = None
        self.rendered = {}
        self.markdown_cache = {}
        self.state = engine.state()
        sessions = self.state["sessions"]
        self.selected = sessions[0]["id"] if sessions else None
        self.drafts = {}
        self.draft, self.cursor = "", 0
        self.command_query = None
        self.command_index = 0
        self.command_dismissed = False
        self.focus = "sidebar" if sessions else "composer"
        self.outputs = {}
        self.scrolls = {}
        self.output_next = 0.0
        self.sidebar_scroll = 0
        self.pending_key = None
        self.ui_error = ""
        self.request_ok = False
        self.colors = {}
        self.ansi_colors = {}
        curses.raw()
        curses.nonl()
        curses.set_escdelay(25)
        window.keypad(True)
        window.timeout(50)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for index, (name, color) in enumerate(
                (("title", curses.COLOR_CYAN), ("warn", curses.COLOR_YELLOW), ("error", curses.COLOR_RED)), 1
            ):
                curses.init_pair(index, color, -1)
                self.colors[name] = curses.color_pair(index) | curses.A_BOLD
            for index, color in enumerate([*range(16), 215], 4):
                if index >= curses.COLOR_PAIRS:
                    break
                curses.init_pair(index, color if color < curses.COLORS else color % 8, -1)
                self.ansi_colors[color] = curses.color_pair(index)

    def _styled_lines(self, text, width):
        """Translate only display styles; never pass terminal controls to curses."""
        lines, used, style, color = [[]], 0, 0, 0
        styles = {1: curses.A_BOLD, 2: curses.A_DIM, 3: getattr(curses, "A_ITALIC", 0),
                  4: curses.A_UNDERLINE, 7: curses.A_REVERSE}
        for part in _ANSI.split(text):
            if part.startswith("\x1b"):
                if part.startswith("\x1b[") and part.endswith("m"):
                    codes = [int(code or 0) for code in part[2:-1].split(";") if code.isdigit() or not code]
                    while codes:
                        code = codes.pop(0)
                        if code == 0:
                            style, color = 0, 0
                        elif code in styles:
                            style |= styles[code]
                        elif code in (22, 23, 24, 27):
                            style &= ~(styles[1] | styles[2] if code == 22 else styles[code - 20])
                        elif code == 39:
                            color = 0
                        elif 30 <= code <= 37 or 90 <= code <= 97:
                            color = self.ansi_colors.get(code - (30 if code < 90 else 82), 0)
                        elif code == 38 and len(codes) >= 2 and codes[0] == 5:
                            color = self.ansi_colors.get(codes[1], 0)
                            del codes[:2]
                continue
            for char in _text(part):
                cells = _width(char)
                if char == "\n" or used + cells > width:
                    lines.append([])
                    used = 0
                if char != "\n":
                    attr = style | color
                    if lines[-1] and lines[-1][-1][1] == attr:
                        previous, _ = lines[-1][-1]
                        lines[-1][-1] = (previous + char, attr)
                    else:
                        lines[-1].append((char, attr))
                    used += cells
        while len(lines) > 1 and not lines[-1]:
            lines.pop()
        return lines

    def _output_lines(self, blocks, width):
        if self.render_job and self.render_job[1].done():
            (selected, source, columns), future = self.render_job
            try:
                rendered = future.result()
            except (OSError, RuntimeError) as error:
                self.ui_error = str(error)
                rendered = "\n\n".join(_text(block["text"]) for block in source)
            self.rendered[selected] = (source, columns, self._styled_lines(rendered, columns))
            self.render_job = None
        cached = self.rendered.get(self.selected)
        if self.render_job is None and (cached is None or cached[:2] != (blocks, width)):
            self.render_job = ((self.selected, blocks, width),
                               self.renderer.submit(render_blocks, self.engine.root, blocks, width,
                                                    self.engine.mu, self.markdown_cache))
        return cached[2] if cached else [[("Rendering…", curses.A_DIM)]]

    def _add(self, y, x, text, width=None, attr=0):
        height, columns = self.window.getmaxyx()
        if not 0 <= y < height or not 0 <= x < columns:
            return
        available = columns - x - (1 if y == height - 1 else 0)
        width = available if width is None else min(available, width)
        if width <= 0:
            return
        try:
            self.window.addstr(y, x, _clip(_text(text).replace("\n", " "), width), attr)
        except curses.error:
            pass

    def _cursor(self, position=None):
        try:
            curses.curs_set(int(position is not None))
            if position is not None:
                self.window.move(*position)
        except curses.error:
            pass

    def _getch_raw(self):
        try:
            key = self.window.get_wch()
            if isinstance(key, int) and key > curses.KEY_MAX:
                return {b"kLFT5": "ctrl-left", b"kRIT5": "ctrl-right",
                        b"kbs5": "ctrl-backspace", b"kent2": 10}.get(curses.keyname(key), key)
            if isinstance(key, str) and len(key) == 1 and (ord(key) < 32 or key == "\x7f"):
                return ord(key)
            return key
        except curses.error:
            return None

    def _getch(self):
        if self.pending_key is not None:
            key, self.pending_key = self.pending_key, None
            return key
        key = self._getch_raw()
        return self._escape_key() if key == 27 else key

    def _tick(self):
        try:
            self.engine.tick()
        except Exception as error:
            self.ui_error = str(error)
        self.state = self.engine.state()
        ids = [session["id"] for session in self.state["sessions"]]
        if self.selected is not None and self.selected not in ids:
            self._select(ids[0] if ids else None)
        if time.monotonic() >= self.output_next:
            fresh = self._request({"op": "output", "session_id": self.selected}, clear_error=False)
            if fresh is not None:
                self.outputs[self.selected] = fresh
            self.output_next = time.monotonic() + 0.2

    def _request(self, request, *, clear_error=True):
        try:
            result = self.engine.request(request)
            self.request_ok = True
            if clear_error:
                self.ui_error = ""
            self.state = self.engine.state()
            return result
        except Exception as error:
            self.request_ok = False
            self.ui_error = str(error)
            return None

    def _session(self, session_id=None):
        session_id = self.selected if session_id is None else session_id
        return next((item for item in self.state["sessions"] if item["id"] == session_id), None)

    def _save_draft(self):
        if self.selected is not None:
            self.drafts[self.selected] = (self.draft, self.cursor)

    def _select(self, session_id):
        if session_id == self.selected:
            return
        self._save_draft()
        self.selected = session_id
        self.draft, self.cursor = self.drafts.get(session_id, ("", 0))
        self.focus = "sidebar" if session_id is not None else "composer"
        self.output_next = 0.0

    def _set_draft(self, text, cursor=None):
        self.draft = text
        self.cursor = len(text) if cursor is None else max(0, min(len(text), cursor))
        self._save_draft()

    def _selected_index(self):
        return next((i for i, session in enumerate(self.state["sessions"]) if session["id"] == self.selected), 0)

    def _move_session(self, amount):
        sessions = self.state["sessions"]
        if sessions:
            index = self._selected_index()
            index = max(0, min(len(sessions) - 1, index + amount))
            self._select(sessions[index]["id"])

    def _pending(self, session_id):
        return [message for message in self.state["messages"] if message["session_id"] == session_id]

    def _scheduler_line(self):
        scheduler = self.state["scheduler"]
        status = "error" if scheduler.get("error") else "active" if scheduler["active"] else "idle"
        return f"Scheduler {status}"

    def _session_status(self, session):
        active = session.get("active")
        if session.get("hold"):
            return "held"
        if active:
            return "stopping" if active.get("stopping") else "reading" if active["mode"] == "readonly" else "writing"
        return session.get("gate") or ("blocked" if session.get("blocked") else "")

    def _draw_sidebar(self, y, width, height):
        if width <= 0 or height <= 0:
            return
        sessions = self.state["sessions"]
        self._add(y, 1, "Sessions", width - 2,
                  self.colors.get("title", 0) if self.focus == "sidebar" else curses.A_DIM)
        capacity = max(0, height - 1)
        index = self._selected_index()
        self.sidebar_scroll = max(0, min(self.sidebar_scroll, max(0, len(sessions) - capacity)))
        if capacity and index < self.sidebar_scroll:
            self.sidebar_scroll = index
        elif capacity and index >= self.sidebar_scroll + capacity:
            self.sidebar_scroll = index - capacity + 1
        for row, session in enumerate(sessions[self.sidebar_scroll:self.sidebar_scroll + capacity], y + 1):
            pending = len(self._pending(session["id"]))
            owner = "◆" if self.state["workspace"]["owner"] == session["id"] else ""
            suffix = f" +{pending}" if pending else ""
            status = self._session_status(session)
            suffix += f" {status}" if status else ""
            prefix = f"S{session['id']}{owner} "
            name = _clip(session["name"], max(0, width - 2 - _width(prefix + suffix)))
            self._add(row, 1, prefix + name + suffix, width - 2,
                      curses.A_REVERSE if session["id"] == self.selected else 0)

    def _draw_conversation(self, session, x, y, width, height):
        if width <= 0 or height <= 0:
            return
        output = self.outputs.get(self.selected, {})
        if session:
            title = f"S{session['id']} · {session['name']}"
            status = self._session_status(session)
            if status:
                title += f" · {status}"
            if self.state["workspace"]["owner"] == session["id"]:
                title += " · ◆"
            queue = self._pending(session["id"])
        else:
            title, queue = "Scheduler", []
        scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
        if not scroll["follow"]:
            title += " · scrolled (End to follow)"
        self._add(y, x + 1, title, width - 2,
                  self.colors.get("title", 0) if self.focus == "conversation" else curses.A_DIM)
        row = y + 1
        if session and row < y + height:
            model = session.get("next_model") or "Mu/session default"
            active = session.get("active")
            label = f"Model: {model} · /model session to change"
            if active and active.get("model") != model:
                label = f"Active: {active.get('model')} · Next: {model}"
            self._add(row, x + 1, label, width - 2, curses.A_DIM)
            row += 1
        if queue:
            visible = min(len(queue), 3, max(0, y + height - row - 1))
            if len(queue) > visible:
                visible = max(0, visible - 1)
            for message in queue[:visible]:
                self._add(row, x + 1, f"{message['state']} · {message['text']}", width - 2, self.colors.get("warn", 0))
                row += 1
            if len(queue) > visible and row < y + height:
                self._add(row, x + 1, f"… {len(queue) - visible} more queued", width - 2, curses.A_DIM)
                row += 1
        blocks = output.get("blocks", [])
        if blocks:
            lines = self._output_lines(blocks, max(2, width - 2))
        else:
            empty = "No output yet." if session or self.state["sessions"] else "Create a session with /new [name]."
            lines = [[(empty, curses.A_DIM)]]
        visible = max(0, y + height - row)
        bottom = max(0, len(lines) - visible)
        scroll["line"] = bottom if scroll["follow"] else max(0, min(scroll["line"], bottom))
        scroll.update(visible=visible, total=len(lines))
        for line_y, spans in enumerate(lines[scroll["line"]:scroll["line"] + visible], row):
            offset = 0
            for text, attr in spans:
                text = _clip(text, max(0, width - 2 - offset))
                self._add(line_y, x + 1 + offset, text, width - 2 - offset, attr)
                offset += _width(text)

    def _notice(self):
        if self.ui_error:
            return self.ui_error
        if error := self.state["scheduler"].get("error"):
            return error
        session = self._session()
        if session:
            if session.get("hold") or session.get("gate"):
                return session.get("reason") or "Session held · /resume to continue"
            return session.get("blocked") or ""
        return self.state["scheduler"].get("reason") or ""

    def _composer_geometry(self, height, width):
        notice_y = height - 1 - bool(self._notice())
        rows = min(len(_lines(self.draft, max(2, width - 5))), 4, max(1, height - 5))
        label_y = notice_y - rows - 1
        top = label_y + 1
        return label_y, top, max(0, notice_y - top), notice_y

    def _draw_composer(self, label_y, top, rows, width):
        if rows <= 0:
            return None
        text_width = max(2, width - 5)
        lines = _lines(self.draft, text_width)
        before = _lines(self.draft[:self.cursor], text_width)
        cursor_row = len(before) - 1
        cursor_col = _width(before[-1])
        start = max(0, cursor_row - rows + 1) if self.focus == "composer" else max(0, len(lines) - rows)
        for offset, line in enumerate(lines[start:start + rows]):
            row = top + offset
            self._add(row, 1, ">" if offset == 0 else "·",
                      attr=self.colors.get("title", 0) if self.focus == "composer" else curses.A_DIM)
            self._add(row, 3, line, width - 5)
        if self.focus != "composer":
            return None
        row = cursor_row - start
        if 0 <= row < rows:
            return row, min(max(0, width - 1), 3 + cursor_col)
        return None

    def _draw_main(self):
        self.window.erase()
        height, width = self.window.getmaxyx()
        if height <= 0 or width <= 0:
            return
        scheduler_text = self._scheduler_line()
        scheduler_x = max(1, width - len(scheduler_text) - 1)
        self._add(0, 1, f"Mu Board · {self.state['root']}", max(0, scheduler_x - 2), curses.A_DIM)
        if width > 1:
            self._add(0, scheduler_x, scheduler_text, attr=curses.A_DIM)

        if height < 6:
            if height > 1:
                self._add(1, 1, "Resize for full session view", width - 2, self.colors.get("warn", 0))
            if height == 4:
                session = self._session()
                self._add(2, 1, f"{session.get('name') if session else 'no session'} · {self.draft}", width - 2)
            elif height > 2:
                self._add(2, 1, f"Selected: {self._session().get('name') if self._session() else 'none'}", width - 2)
            if height == 5:
                self._add(height - 2, 1, self.draft, width - 2)
            if height > 1:
                self._add(height - 1, 0, "Ctrl-P sessions · Ctrl-Q quit", attr=curses.A_DIM)
            self._cursor()
            self.window.refresh()
            return

        label_y, composer_top, composer_rows, notice_y = self._composer_geometry(height, width)
        body_y, body_height = 1, max(0, label_y - 1)
        session = self._session()
        split = width >= 62 and body_height >= 4
        if split:
            sidebar_width = max(20, min(30, width // 3))
            self._draw_sidebar(body_y, sidebar_width, body_height)
            for row in range(body_y, body_y + body_height):
                self._add(row, sidebar_width, "│", 1, curses.A_DIM)
            self._draw_conversation(session, sidebar_width + 1, body_y, width - sidebar_width - 1, body_height)
        else:
            self._draw_conversation(session, 0, body_y, width, body_height)

        if label_y >= 0:
            self._add(label_y, 0, "─" * max(0, width - 1), attr=curses.A_DIM)
        cursor = self._draw_composer(label_y, composer_top, composer_rows, width)
        self._draw_commands(label_y, width)
        if notice := self._notice():
            self._add(notice_y, 1, notice, width - 2, self.colors.get("warn", 0))
        self._add(height - 1, 1, "Tab panes · Ctrl-P sessions · /help", attr=curses.A_DIM)
        self._cursor((composer_top + cursor[0], cursor[1]) if cursor else None)
        self.window.refresh()

    def _command_matches(self):
        if self.draft != self.command_query:
            self.command_query = self.draft
            self.command_index = 0
            self.command_dismissed = False
        if (self.focus != "composer" or self.command_dismissed or self.cursor != len(self.draft)
                or not self.draft.startswith("/") or any(char.isspace() for char in self.draft)):
            return []
        return [(name, description) for name, description in self.COMMANDS.items()
                if name.split()[0].startswith(self.draft)]

    def _draw_commands(self, bottom, width):
        matches = self._command_matches()
        visible = min(5, len(matches), max(0, bottom - 3))
        if not visible or width < 8:
            return
        box_width = min(88, width - 2)
        top = bottom - visible - 2
        start = max(0, min(self.command_index - visible + 1, len(matches) - visible))
        header = f" Commands {self.command_index + 1}/{len(matches)} · ↑↓ select · Tab fill · Enter run · Esc hide "
        self._add(top, 1, "┌" + _clip(header, box_width - 2).ljust(box_width - 2, "─") + "┐", box_width, curses.A_DIM)
        for row, index in enumerate(range(start, start + visible), top + 1):
            name, description = matches[index]
            line = _clip(f" {name}  {description}", box_width - 2)
            self._add(row, 1, "│" + line + " " * (box_width - 2 - _width(line)) + "│", box_width,
                      curses.A_REVERSE if index == self.command_index else 0)
        self._add(bottom - 1, 1, "└" + "─" * (box_width - 2) + "┘", box_width, curses.A_DIM)

    def _command_key(self, key):
        matches = self._command_matches()
        if not matches:
            return False
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            self.command_index = max(0, min(len(matches) - 1, self.command_index + (-1 if key == curses.KEY_UP else 1)))
        elif key in (9, 13, curses.KEY_ENTER):
            name = matches[self.command_index][0].split()[0]
            self._set_draft(name + (" " if key == 9 else ""))
            if key != 9:
                self._submit()
        else:
            return False
        return True

    def _insert(self, text):
        self.draft = self.draft[:self.cursor] + text + self.draft[self.cursor:]
        self.cursor += len(text)
        self._save_draft()

    def _line_bounds(self):
        start = self.draft.rfind("\n", 0, self.cursor) + 1
        end = self.draft.find("\n", self.cursor)
        return start, len(self.draft) if end < 0 else end

    def _word_boundary(self, direction):
        position = self.cursor
        if direction < 0:
            while position and self.draft[position - 1].isspace():
                position -= 1
            while position and not self.draft[position - 1].isspace():
                position -= 1
        else:
            while position < len(self.draft) and not self.draft[position].isspace():
                position += 1
            while position < len(self.draft) and self.draft[position].isspace():
                position += 1
        return position

    def _vertical_cursor(self, direction):
        start, _ = self._line_bounds()
        column = self.cursor - start
        if direction < 0:
            if start == 0:
                return
            previous_end = start - 1
            previous_start = self.draft.rfind("\n", 0, previous_end) + 1
            self.cursor = previous_start + min(column, previous_end - previous_start)
        else:
            _, end = self._line_bounds()
            if end == len(self.draft):
                return
            next_start = end + 1
            next_end = self.draft.find("\n", next_start)
            if next_end < 0:
                next_end = len(self.draft)
            self.cursor = next_start + min(column, next_end - next_start)
        self._save_draft()

    def _submit(self):
        text = self.draft.strip()
        if not text:
            if self.focus != "composer":
                self.focus = "composer"
            return
        if text.startswith("/") and not text.startswith("//"):
            self._command(text)
            return
        if self.selected is None:
            self.ui_error = "No session selected. Create one with /new [name]."
            return
        message = text[1:] if text.startswith("//") else self.draft
        if self._request({"op": "send", "session_id": self.selected, "text": message}) is not None:
            self._set_draft("")

    def _command(self, text):
        parts = text[1:].split(maxsplit=1)
        command = "/" + parts[0] if parts else "/"
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command not in {item.split()[0] for item in self.COMMANDS}:
            self.ui_error = "Unknown command. Use /help for local commands."
            return
        if command not in ("/new", "/rename", "/model", "/resume") and argument:
            self.ui_error = f"Usage: {command}"
            return
        if command == "/model" and argument not in ("", "session", "scheduler", "worker", "both"):
            self.ui_error = "Usage: /model [session|scheduler|worker|both]"
            return
        if command == "/resume" and argument not in ("", "selected"):
            self.ui_error = "Usage: /resume [selected]"
            return
        if command == "/rename" and not argument:
            self.ui_error = "Usage: /rename <name>|--auto"
            return
        self._set_draft("")
        if command == "/new":
            request = {"op": "new"}
            if argument:
                request["name"] = argument
            result = self._request(request)
            if self.request_ok and result is not None:
                self._select(result["session_id"])
                self.focus = "composer"
        elif command == "/rename":
            if self.selected is None:
                self.ui_error = "Select a session to rename."
                return
            self._request({"op": "rename", "session_id": self.selected,
                           "name": None if argument == "--auto" else argument})
        elif command == "/close":
            self._close()
        elif command == "/model":
            if argument:
                self._models(argument)
            else:
                self._info("Selected models", [
                    *([f"S{self.selected} next: {self._session().get('next_model') or 'Mu/session default'}",
                       f"Session override: {self._session().get('model') or 'Inherit worker/Mu selection'}", ""]
                      if self._session() else []),
                    *(f"{role.title()}: {self.state['models'].get(role) or 'Mu/session default'}"
                      for role in ("scheduler", "worker")), "",
                    *(f"{label} active: {active.get('model') or 'Mu/session default'} · {active['mode']}"
                      for label, active in [("Scheduler", self.state["scheduler"]["active"]),
                                            *((f"S{s['id']}", s.get("active")) for s in self.state["sessions"])]
                      if active), "",
                    "Use /model session to change only the selected session.",
                    "Use /model scheduler, /model worker, or /model both to change defaults.",
                    "Changes apply to later invocations; active workers keep their current model.",
                ])
        elif command == "/resume":
            self._resume()
        elif command == "/schedule":
            self._request({"op": "schedule"})
        elif command == "/help":
            self._help()
        elif command == "/quit":
            self._quit()

    def _close(self):
        session = self._session()
        if session is None:
            self.ui_error = "Select a session to close."
            return
        session_id = session["id"]
        messages = self._pending(session_id)
        discard = bool(messages)
        if discard and not self._confirm("Discard pending messages?",
                                         f"Closing {session.get('name') or f'Session {session_id}'} will discard {len(messages)} pending/inflight/interrupted message(s). Continue?"):
            return
        index = self._selected_index()
        self._request({"op": "remove", "session_id": session_id, "discard": discard})
        if not self.request_ok:
            return
        remaining = self.state["sessions"]
        target = remaining[min(index, len(remaining) - 1)]["id"] if remaining else None
        self._select(target)
        self.drafts.pop(session_id, None)
        self.outputs.pop(session_id, None)
        self.rendered.pop(session_id, None)
        self.scrolls.pop(session_id, None)

    def _resume(self):
        session = self._session()
        if session is None:
            self.ui_error = "Select a session to resume."
            return
        if session.get("gate"):
            name = session.get("name") or f"Session {session['id']}"
            if not self._confirm("Retry interrupted turn?",
                                 f"Resuming {name} explicitly authorizes retrying its interrupted turn. Continue?"):
                return
        self._request({"op": "resume", "session_id": session["id"]})

    def _models(self, role):
        session = self._session()
        if role == "session" and session is None:
            self.ui_error = "Select a session first, or create one with /new."
            return
        response = self._request({"op": "models"})
        if response is None:
            return
        roles = ("scheduler", "worker") if role == "both" else (role,)
        chosen = {}
        for current_role in roles:
            selected = session.get("model") if current_role == "session" else response["selected"].get(current_role)
            base, separator, effort = (selected or "").rpartition(":")
            if not separator:
                base, effort = selected or "", ""
            available = response["available"]
            models = [model["id"] for model in available]
            options = ["Inherit worker/Mu selection" if current_role == "session" else "Mu/session default", *models]
            initial = models.index(base) + 1 if base in models else 0
            choice = self._pick(f"{current_role.title()} model · next invocation", options, initial)
            if choice is None:
                return
            reference = None
            if choice:
                model = available[choice - 1]
                reference = model["id"]
                efforts = model.get("supported_efforts") or []
                if efforts:
                    current_effort = efforts.index(effort) + 1 if base == reference and effort in efforts else 0
                    effort_choice = self._pick(f"Effort · {reference}", ["Default effort", *efforts], current_effort)
                    if effort_choice is None:
                        return
                    if effort_choice:
                        reference += ":" + efforts[effort_choice - 1]
            chosen[current_role] = reference
        if role == "session":
            self._request({"op": "set_session_model", "session_id": session["id"], "model": chosen["session"]})
        else:
            self._request({"op": "set_models", "models": chosen})

    def _interrupt(self):
        if self.selected is None:
            self.ui_error = "No selected session to interrupt."
            return
        self._request({"op": "interrupt", "session_id": self.selected})

    def _quit(self):
        active_sessions = [session for session in self.state["sessions"] if session.get("active")]
        scheduler_active = bool(self.state["scheduler"].get("active"))
        confirmed = False
        if active_sessions or scheduler_active:
            count = len(active_sessions) + int(scheduler_active)
            if not self._confirm("Stop work and quit?",
                                 f"{count} agent(s) are active, including the scheduler if running. Stop them and quit?",
                                 quit_shortcut=False):
                return
            confirmed = True
        self._request({"op": "shutdown", "confirmed": confirmed})
        if not self.request_ok:
            return
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._add(0, 1, "Stopping Mu…", width - 2, self.colors.get("title", 0))
            self._add(2, 1, "Waiting for owned agents to stop.", width - 2)
            self._cursor()
            self.window.refresh()
            self._getch()

    def _dialog_global(self, key):
        if key == 17:
            self._quit()
            return True
        return False

    def _pick(self, title, options, selected=0):
        if not options:
            return None
        selected = max(0, min(len(options) - 1, selected))
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._add(0, 1, title, width - 2, self.colors.get("title", 0))
            visible = max(0, height - 4)
            start = max(0, min(selected, len(options) - visible)) if visible else selected
            for index in range(start, min(len(options), start + visible)):
                self._add(2 + index - start, 2, options[index], width - 4,
                          curses.A_REVERSE if index == selected else 0)
            if height >= 2:
                self._add(height - 2, 1, self.ui_error, width - 2, self.colors.get("error", 0))
            self._add(height - 1, 0, "↑↓ select · Enter choose · Q/Esc cancel", attr=curses.A_DIM)
            self._cursor()
            self.window.refresh()
            key = self._getch()
            if self._dialog_global(key):
                continue
            if key in (3, 27, "q", "Q"):
                return None
            if key in (10, 13, curses.KEY_ENTER):
                return selected
            if key in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                selected = min(len(options) - 1, selected + 1)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - max(1, visible))
            elif key == curses.KEY_NPAGE:
                selected = min(len(options) - 1, selected + max(1, visible))
        return None

    def _pick_session(self):
        sessions = self.state["sessions"]
        options = ["Scheduler · decisions and output"]
        for session in sessions:
            active = session.get("active")
            status = f" · {active['kind']} active" if active else f" · {session.get('gate')}" if session.get("gate") else ""
            pending = len(self._pending(session["id"]))
            if pending:
                status += f" · {pending} queued"
            options.append(f"#{session['id']} {session.get('name') or 'Session'}{status}")
        initial = self._selected_index() + 1 if self.selected is not None else 0
        choice = self._pick("Select session · Ctrl-P", options, initial)
        if choice is not None:
            self._select(sessions[choice - 1]["id"] if choice else None)

    def _confirm(self, title, message, *, quit_shortcut=True):
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._add(0, 1, title, width - 2, self.colors.get("warn", 0))
            lines = _lines(message, max(2, width - 4))
            for row, line in enumerate(lines[:max(0, height - 3)], 2):
                self._add(row, 2, line, width - 4)
            self._add(height - 1, 0, "y confirm · n/Q/Esc/Enter cancel", attr=curses.A_DIM)
            self._cursor()
            self.window.refresh()
            key = self._getch()
            if key == 17:
                if quit_shortcut:
                    self._quit()
                else:
                    return False
                continue
            if key in (3, 27, 10, 13, curses.KEY_ENTER, "n", "N", "q", "Q"):
                return False
            if key in ("y", "Y"):
                return True
        return False

    def _help(self):
        lines = ["Commands:", *(f"  {name}  {description}" for name, description in self.COMMANDS.items()), "",
                 "Keyboard:", "  Ctrl-P         Pick a session", "  Tab/Shift-Tab  Cycle sidebar, conversation, composer",
                 "  ↑/↓            Move in sidebar; scroll in conversation; edit in composer",
                 "  → in sidebar   Focus conversation", "  ← in output    Focus sidebar",
                 "  Enter          Focus composer, or queue its message", "  Shift-Enter    Insert a newline (Alt-Enter and Ctrl-J also work)",
                 "  PgUp/PgDn      Scroll conversation", "  Ctrl-C         Clear composer input; interrupt session in other panes", "  Ctrl-Q         Quit; confirms before stopping active agents",
                 "  /              List commands; ↑/↓ select, Tab fill, Enter run, Esc hide",
                 "  Q/Esc          Close information screens or cancel pickers",
                 "  Home/End       Start/end of line", "  Ctrl-←/→       Jump between words",
                 "  Ctrl-Backspace Delete previous word"]
        self._info("Mu Board help", lines)

    def _info(self, title, lines):
        offset = 0
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._add(0, 1, title, width - 2, self.colors.get("title", 0))
            visible = max(0, height - 2)
            wrapped = [line for text in lines for line in _lines(text, max(2, width - 2))]
            offset = min(offset, max(0, len(wrapped) - visible))
            for row, line in enumerate(wrapped[offset:offset + visible], 1):
                self._add(row, 1, line, width - 2)
            self._add(height - 1, 0, "↑↓/PgUp/PgDn scroll · Q/Esc close", attr=curses.A_DIM)
            self._cursor()
            self.window.refresh()
            key = self._getch()
            if self._dialog_global(key):
                continue
            if key in (3, 27, "q", "Q", 10, 13, curses.KEY_ENTER):
                return
            if key in (curses.KEY_UP,):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN,):
                offset = min(max(0, len(wrapped) - visible), offset + 1)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - max(1, visible))
            elif key == curses.KEY_NPAGE:
                offset = min(max(0, len(wrapped) - visible), offset + max(1, visible))

    def _view_scroll(self, amount):
        scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
        visible = scroll.get("visible", 1)
        scroll["line"] = max(0, scroll["line"] + amount)
        scroll["follow"] = False if amount < 0 else scroll["follow"]
        if amount > 0:
            bottom = max(0, scroll.get("total", 0) - visible)
            scroll["line"] = min(scroll["line"], bottom)
            scroll["follow"] = scroll["line"] >= bottom

    def _escape_key(self):
        self.window.timeout(35)
        try:
            key = self._getch_raw()
            if key == "[":
                sequence = ""
                while True:
                    char = self._getch_raw()
                    if not isinstance(char, str) or len(char) != 1:
                        return None
                    sequence += char
                    if "@" <= char <= "~":
                        break
                return {"1;5D": "ctrl-left", "1;5C": "ctrl-right",
                        "13;2u": 10, "27;2;13~": 10,
                        "8;5u": "ctrl-backspace", "127;5u": "ctrl-backspace",
                        "27;5;8~": "ctrl-backspace", "27;5;127~": "ctrl-backspace"}.get(sequence)
            if key in (10, 13, curses.KEY_ENTER):
                return 10
            self.pending_key = key
            return 27
        finally:
            self.window.timeout(50)

    def _cycle_focus(self, reverse=False):
        height, width = self.window.getmaxyx()
        label_y = self._composer_geometry(height, width)[0] if height >= 6 else 0
        body_height = max(0, label_y - 1)
        panes = ["sidebar", "conversation", "composer"] if width >= 62 and body_height >= 4 else ["conversation", "composer"]
        index = panes.index(self.focus) if self.focus in panes else 0
        self.focus = panes[(index + (-1 if reverse else 1)) % len(panes)]

    def _main_key(self, key):
        if key == 3:
            if self.focus == "composer":
                self._set_draft("")
            else:
                self._interrupt()
            return
        if key == 17:
            self._quit()
            return
        if key == 16:
            self._pick_session()
            return
        if self._command_key(key):
            return
        if key == 9:
            self._cycle_focus()
            return
        if key == curses.KEY_BTAB:
            self._cycle_focus(reverse=True)
            return
        if key == 27:
            if self.focus == "composer":
                self.command_dismissed = True
            else:
                self.focus = "sidebar"
            return
        if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            visible = max(1, self.scrolls.get(self.selected, {}).get("visible", 1))
            self._view_scroll(-visible if key == curses.KEY_PPAGE else visible)
            return

        if self.focus == "sidebar":
            if key == curses.KEY_RIGHT:
                self.focus = "conversation"
            elif key == curses.KEY_UP:
                self._move_session(-1)
            elif key == curses.KEY_DOWN:
                self._move_session(1)
            elif key in (10, 13, curses.KEY_ENTER):
                self.focus = "composer"
            return

        if self.focus == "conversation":
            if key == curses.KEY_LEFT:
                self.focus = "sidebar"
            elif key == curses.KEY_UP:
                self._view_scroll(-1)
            elif key == curses.KEY_DOWN:
                self._view_scroll(1)
            elif key == curses.KEY_HOME:
                scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
                scroll.update(follow=False, line=0)
            elif key == curses.KEY_END:
                self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})["follow"] = True
            elif key in (10, 13, curses.KEY_ENTER):
                self.focus = "composer"
            return

        if key in (13, curses.KEY_ENTER):
            self._submit()
        elif key in (curses.KEY_LEFT,):
            self.cursor = max(0, self.cursor - 1)
            self._save_draft()
        elif key in (curses.KEY_RIGHT,):
            self.cursor = min(len(self.draft), self.cursor + 1)
            self._save_draft()
        elif key == curses.KEY_UP:
            self._vertical_cursor(-1)
        elif key == curses.KEY_DOWN:
            self._vertical_cursor(1)
        elif key in ("ctrl-left", "ctrl-right"):
            self.cursor = self._word_boundary(-1 if key == "ctrl-left" else 1)
            self._save_draft()
        elif key == curses.KEY_HOME:
            self.cursor = self.draft.rfind("\n", 0, self.cursor) + 1
            self._save_draft()
        elif key == curses.KEY_END:
            _, self.cursor = self._line_bounds()
            self._save_draft()
        elif key in (curses.KEY_BACKSPACE, 127):
            if self.cursor:
                self.draft = self.draft[:self.cursor - 1] + self.draft[self.cursor:]
                self.cursor -= 1
                self._save_draft()
        elif key == curses.KEY_DC:
            if self.cursor < len(self.draft):
                self.draft = self.draft[:self.cursor] + self.draft[self.cursor + 1:]
                self._save_draft()
        elif key in (8, "ctrl-backspace"):
            begin = self._word_boundary(-1)
            self.draft = self.draft[:begin] + self.draft[self.cursor:]
            self.cursor = begin
            self._save_draft()
        elif key == 10:
            self._insert("\n")
        elif isinstance(key, str) and key.isprintable():
            self._insert(key)

    def _main(self):
        while not self.engine.done:
            self._tick()
            if self.engine.done:
                break
            self._draw_main()
            self._main_key(self._getch())


def run_ui(engine):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Mu Board UI requires an interactive terminal")
    with ThreadPoolExecutor(max_workers=1) as renderer:
        curses.wrapper(lambda window: _UI(window, engine, renderer)._main())
