"""Curses session UI for Mu Board."""

from __future__ import annotations

import curses
import sys
import time
import unicodedata


def _text(value) -> str:
    return "".join(
        "    " if char == "\t" else "�" if char != "\n" and unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in str(value or "")
    )


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
        "/close": "Close the selected session",
        "/model [scheduler|worker|both]": "Show selected models, or choose models and effort for a role",
        "/resume [selected]": "Resume the selected session",
        "/schedule": "Explicitly recheck or recover the scheduler",
        "/help": "Show commands and keyboard controls",
        "/quit": "Stop work and quit",
    }

    def __init__(self, window, engine):
        self.window, self.engine = window, engine
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
            if isinstance(key, str) and len(key) == 1 and (ord(key) < 32 or key == "\x7f"):
                return ord(key)
            return key
        except curses.error:
            return None

    def _getch(self):
        if self.pending_key is not None:
            key, self.pending_key = self.pending_key, None
            return key
        return self._getch_raw()

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
            if self.selected is not None:
                scheduler_output = self._request({"op": "output", "session_id": None}, clear_error=False)
                if scheduler_output is not None:
                    self.outputs[None] = scheduler_output
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
        selected = self.state["models"].get("scheduler") or "session default"
        active = scheduler["active"]
        if active:
            return f"Scheduler active · actual {active.get('model') or 'session default'} · default {selected}"
        if scheduler.get("error"):
            return f"Scheduler error · default {selected}"
        return f"Scheduler idle · default {selected}"

    def _draw_sidebar(self, y, width, height):
        if width <= 0 or height <= 0:
            return
        sessions = self.state["sessions"]
        self._add(y, 1, f"Sessions · {len(sessions)}", width - 2,
                  self.colors.get("title", 0) if self.focus == "sidebar" else curses.A_DIM)
        capacity = max(0, height - (4 if height >= 5 else 2))
        index = self._selected_index()
        self.sidebar_scroll = max(0, min(self.sidebar_scroll, max(0, len(sessions) - capacity)))
        if capacity and index < self.sidebar_scroll:
            self.sidebar_scroll = index
        elif capacity and index >= self.sidebar_scroll + capacity:
            self.sidebar_scroll = index - capacity + 1
        for row, session in enumerate(sessions[self.sidebar_scroll:self.sidebar_scroll + capacity], y + 1):
            pending = len(self._pending(session["id"]))
            active = session.get("active")
            status = ("stopping" if active.get("stopping") else "reading" if active["mode"] == "readonly" else "writing") if active else "idle"
            status = "held" if session.get("hold") else session.get("gate") or ("blocked" if session.get("blocked") else status)
            owner = "◆" if self.state["workspace"]["owner"] == session["id"] else ""
            suffix = f" +{pending}" if pending else ""
            name = session.get("name") or f"Session {session['id']}"
            self._add(row, 1, f"S{session['id']} {status}{owner}{suffix} · {name}", width - 2,
                      curses.A_REVERSE if session["id"] == self.selected else 0)
        scheduler = self.state["scheduler"]
        active = scheduler["active"]
        default = self.state["models"].get("scheduler") or "session default"
        actual = active.get("model") or "session default" if active else "not active"
        if height >= 5:
            self._add(y + height - 3, 1, f"Scheduler · {'active' if active else 'idle'}", width - 2, curses.A_DIM)
            self._add(y + height - 2, 1, f"Actual: {actual} · default: {default}", width - 2, curses.A_DIM)
            text = self.outputs.get(None, {}).get("text", "")
            preview = _lines(text, max(2, width - 9))[-1] if text else scheduler.get("error") or "No scheduler output"
            self._add(y + height - 1, 1, f"Output: {preview}", width - 2, curses.A_DIM)
        elif height > 1:
            self._add(y + height - 1, 1, f"Sched: {actual} · def {default}", width - 2, curses.A_DIM)

    def _draw_conversation(self, session, x, y, width, height):
        if width <= 0 or height <= 0:
            return
        output = self.outputs.get(self.selected, {})
        scheduler_mode = session is None
        if session:
            name = session.get("name") or f"Session {session['id']}"
            self._add(y, x + 1, f"{name} · #{session['id']}", width - 2,
                      self.colors.get("title", 0) if self.focus == "conversation" else 0)
            active = session.get("active")
            role = active.get("kind") if active else "worker"
            default = self.state["models"].get(role) or "session default"
            actual = active.get("model") or "session default" if active else "not active"
            info = f"{role} actual: {actual} · selected default: {default}"
            if active:
                info += f" · {active['mode']}"
            if self.state["workspace"]["owner"] == session["id"]:
                info += " · workspace owner"
            if session.get("hold"):
                info += " · held"
            if session.get("gate"):
                info += f" · {session['gate']}"
            if session.get("blocked"):
                info += f" · {session['blocked']}"
            if height > 1:
                self._add(y + 1, x + 1, info, width - 2, curses.A_DIM)
            queue = self._pending(session["id"])
        else:
            scheduler = self.state["scheduler"]
            active = scheduler["active"]
            default = self.state["models"].get("scheduler") or "session default"
            actual = active.get("model") or "session default" if active else "not active"
            self._add(y, x + 1, "Scheduler output", width - 2,
                      self.colors.get("title", 0) if self.focus == "conversation" else 0)
            if height > 1:
                self._add(y + 1, x + 1, f"scheduler actual: {actual} · selected default: {default}", width - 2, curses.A_DIM)
            queue = []

        row = y + min(2, height)
        if queue and row < y + height:
            self._add(row, x + 1, f"Mailbox · {len(queue)}", width - 2, self.colors.get("warn", 0))
            row += 1
            available = max(0, y + height - row - 1)
            visible = min(len(queue), available, 4)
            for message in queue[-visible:] if visible else []:
                label = f"{message['state']} #{message['id']}: "
                self._add(row, x + 1, label + message["text"], width - 2, curses.A_DIM)
                row += 1
            hidden = len(queue) - visible
            if hidden and row < y + height:
                self._add(row, x + 1, f"… {hidden} earlier pending", width - 2, curses.A_DIM)
                row += 1

        source = output.get("source") or "output"
        if row < y + height:
            self._add(row, x + 1, f"{source} · PgUp/PgDn scroll", width - 2, curses.A_DIM)
            row += 1
        text = output.get("text") or ("Waiting for scheduler output…" if scheduler_mode and self.state["scheduler"]["active"] else
                                       "No scheduler output yet." if scheduler_mode else "Waiting for session output…")
        lines = _lines(text, max(2, width - 2))
        visible = max(0, y + height - row)
        scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
        bottom = max(0, len(lines) - visible)
        if scroll["follow"]:
            scroll["line"] = bottom
        else:
            scroll["line"] = max(0, min(scroll["line"], bottom))
        scroll["visible"] = visible
        scroll["total"] = len(lines)
        for line_y, line in enumerate(lines[scroll["line"]:scroll["line"] + visible], row):
            self._add(line_y, x + 1, line, width - 2)

    def _composer_geometry(self, height, width):
        notice_y = height - 2
        rows = max(1, min(4, (height - 5) // 5))
        label_y = notice_y - rows - 1
        top = label_y + 1
        return label_y, top, max(0, notice_y - top), notice_y

    def _draw_composer(self, label_y, top, rows, width):
        if label_y >= 0:
            focus_attr = self.colors.get("title", 0) if self.focus == "composer" else curses.A_DIM
            prompt = "Message · Enter queue · Alt-Enter newline" if self.selected is not None else "No session · /new or Ctrl-P · Alt-Enter newline"
            self._add(label_y, 1, prompt, width - 2, focus_attr)
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
            self._add(row, 1, ">" if offset == 0 else "·", attr=self.colors.get("title", 0))
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
        self._add(0, 1, f"Mu Board · {self.state['root']}", max(0, width // 2 - 1), self.colors.get("title", 0))
        if width > 1:
            self._add(0, max(1, width // 2), scheduler_text, max(0, width - max(1, width // 2) - 1), curses.A_DIM)

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
        notice = self.ui_error
        if not notice:
            if session:
                notice = (session.get("reason") or session.get("blocked") or
                          self.state["scheduler"].get("error") or self.state["scheduler"].get("reason") or "")
            else:
                scheduler = self.state["scheduler"]
                notice = scheduler.get("error") or scheduler.get("reason") or ""
        self._add(notice_y, 1, notice, width - 2, self.colors.get("error" if self.ui_error else "warn", 0))
        ctrl_c = "clear input" if self.focus == "composer" else "interrupt"
        self._add(height - 1, 0, f"Tab/Shift-Tab panes · Enter focus/queue · Ctrl-P sessions · Ctrl-C {ctrl_c} · Ctrl-Q quit · /help",
                  attr=curses.A_DIM)
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
        message = text[1:] if text.startswith("//") else text
        if self._request({"op": "send", "session_id": self.selected, "text": message}) is not None:
            self._set_draft("")

    def _command(self, text):
        parts = text[1:].split(maxsplit=1)
        command = "/" + parts[0] if parts else "/"
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command not in {item.split()[0] for item in self.COMMANDS}:
            self.ui_error = "Unknown command. Use /help for local commands."
            return
        if command not in ("/new", "/model", "/resume") and argument:
            self.ui_error = f"Usage: {command}"
            return
        if command == "/model" and argument not in ("", "scheduler", "worker", "both"):
            self.ui_error = "Usage: /model [scheduler|worker|both]"
            return
        if command == "/resume" and argument not in ("", "selected"):
            self.ui_error = "Usage: /resume [selected]"
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
        elif command == "/close":
            self._close()
        elif command == "/model":
            if argument:
                self._models(argument)
            else:
                self._info("Selected models", [
                    *(f"{role.title()}: {self.state['models'].get(role) or 'Mu/session default'}"
                      for role in ("scheduler", "worker")), "",
                    "Use /model scheduler, /model worker, or /model both to change models and effort.",
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
        response = self._request({"op": "models"})
        if response is None:
            return
        roles = ("scheduler", "worker") if role == "both" else (role,)
        chosen = {}
        for current_role in roles:
            selected = response["selected"].get(current_role)
            base, separator, effort = (selected or "").rpartition(":")
            if not separator:
                base, effort = selected or "", ""
            available = response["available"]
            models = [model["id"] for model in available]
            options = ["Mu/session default", *models]
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
                 "  Enter          Focus composer, or queue its message", "  Alt-Enter      Insert a newline (Ctrl-J also inserts one)",
                 "  PgUp/PgDn      Scroll conversation", "  Ctrl-C         Clear composer input; interrupt session in other panes", "  Ctrl-Q         Quit; confirms before stopping active agents",
                 "  /              List commands; ↑/↓ select, Tab fill, Enter run, Esc hide",
                 "  Q/Esc          Close information screens or cancel pickers",
                 "  Ctrl-A/E       Start/end of line", "  Ctrl-U/K       Delete to start/end of line", "  Ctrl-W         Delete previous word"]
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

    def _alt_enter(self):
        self.window.timeout(35)
        try:
            key = self._getch_raw()
        finally:
            self.window.timeout(50)
        if key in (10, 13, curses.KEY_ENTER):
            self._insert("\n")
        elif key is not None:
            self.pending_key = key

    def _cycle_focus(self, reverse=False):
        height, width = self.window.getmaxyx()
        label_y = self._composer_geometry(height, width)[0] if height >= 6 else 0
        body_height = max(0, label_y - 2)
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
                self._alt_enter()
            else:
                self.focus = "sidebar"
            return
        if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            visible = max(1, self.scrolls.get(self.selected, {}).get("visible", 1))
            self._view_scroll(-visible if key == curses.KEY_PPAGE else visible)
            return

        if self.focus == "sidebar":
            if key == curses.KEY_UP:
                self._move_session(-1)
            elif key == curses.KEY_DOWN:
                self._move_session(1)
            elif key in (10, 13, curses.KEY_ENTER):
                self.focus = "composer"
            return

        if self.focus == "conversation":
            if key == curses.KEY_UP:
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
        elif key in (curses.KEY_HOME, 1):
            self.cursor = self.draft.rfind("\n", 0, self.cursor) + 1
            self._save_draft()
        elif key in (curses.KEY_END, 5):
            _, self.cursor = self._line_bounds()
            self._save_draft()
        elif key in (curses.KEY_BACKSPACE, 8, 127):
            if self.cursor:
                self.draft = self.draft[:self.cursor - 1] + self.draft[self.cursor:]
                self.cursor -= 1
                self._save_draft()
        elif key == curses.KEY_DC:
            if self.cursor < len(self.draft):
                self.draft = self.draft[:self.cursor] + self.draft[self.cursor + 1:]
                self._save_draft()
        elif key == 21:
            start, _ = self._line_bounds()
            self.draft = self.draft[:start] + self.draft[self.cursor:]
            self.cursor = start
            self._save_draft()
        elif key == 11:
            _, end = self._line_bounds()
            self.draft = self.draft[:self.cursor] + self.draft[end:]
            self._save_draft()
        elif key == 23:
            start, _ = self._line_bounds()
            begin = self.cursor
            while begin > start and self.draft[begin - 1].isspace():
                begin -= 1
            while begin > start and not self.draft[begin - 1].isspace():
                begin -= 1
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
    curses.wrapper(lambda window: _UI(window, engine)._main())
