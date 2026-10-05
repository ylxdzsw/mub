"""Curses session UI for Mu Board."""

from __future__ import annotations

import curses
import json
import os
import sys
import unicodedata
from collections import deque

from .output import literal as _text
from .output import live_prompt, prompt_bytes
from .terminal import Screen


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
    CELL_FLAGS = (("bold", curses.A_BOLD), ("dim", curses.A_DIM),
                  ("italic", getattr(curses, "A_ITALIC", 0)), ("underline", curses.A_UNDERLINE),
                  ("reverse", curses.A_REVERSE), ("blink", curses.A_BLINK),
                  ("strikethrough", getattr(curses, "A_STRIKEOUT", 0)))

    COMMANDS = {
        "/new [name]": "Create and select a session",
        "/rename <name>|--auto": "Name the selected session, or let its name evolve automatically",
        "/close": "Close the selected session",
        "/model [session|scheduler|worker|both]": "Show models, or choose a session/role model and effort",
        "/interrupt": "Interrupt and hold the selected session",
        "/resume [selected]": "Resume the selected session",
        "/schedule": "Explicitly recheck or recover the scheduler",
        "/scheduler": "Select the scheduler's decisions and output",
        "/links": "Show hyperlink targets from the selected terminal",
        "/help": "Show commands and keyboard controls",
        "/quit": "Stop work and quit",
    }

    def __init__(self, window, engine):
        self.window, self.engine = window, engine
        self.terminal_colors = {}
        self.cell_colors = {}
        self.row_runs = {}
        self.next_row_runs = {}
        self.prompt_key = self.prompt_screen = None
        self.drawn = None
        self.state = engine.state()
        sessions = self.state["sessions"]
        self.selected = sessions[0]["id"] if sessions else None
        self.drafts = {}
        self.draft, self.cursor = "", 0
        self.command_query = None
        self.command_index = 0
        self.command_dismissed = False
        self.scrolls = {}
        self.sidebar_scroll = 0
        self.session_hits = []
        self.output_rect = None
        self.pending_keys = deque()
        self.ui_error = ""
        self.request_ok = False
        self.colors = {}
        curses.raw()
        curses.nonl()
        curses.set_escdelay(25)
        # Finish each physical frame even if more input arrives during refresh.
        curses.typeahead(-1)
        window.keypad(True)
        window.timeout(50)
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)
        curses.mouseinterval(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for index, (name, color) in enumerate(
                (("title", curses.COLOR_CYAN), ("warn", curses.COLOR_YELLOW), ("error", curses.COLOR_RED)), 1
            ):
                curses.init_pair(index, color, -1)
                self.colors[name] = curses.color_pair(index) | curses.A_BOLD

    def _cell_style(self, screen, fg, bg, attrs):
        style = 0
        for name, flag in self.CELL_FLAGS:
            if getattr(attrs, name):
                style |= flag
        if attrs.hyperlink_id is not None:
            style |= curses.A_UNDERLINE
        if curses.has_colors():
            key = fg, bg
            if key not in self.cell_colors:
                colors = tuple(c if c == -1 else c % curses.COLORS
                               for c in (screen.color(fg), screen.color(bg, background=True)))
                if colors not in self.terminal_colors and len(self.terminal_colors) + 4 < curses.COLOR_PAIRS:
                    pair = len(self.terminal_colors) + 4
                    curses.init_pair(pair, *colors)
                    self.terminal_colors[colors] = curses.color_pair(pair)
                if len(self.cell_colors) >= 4096:
                    self.cell_colors.clear()
                self.cell_colors[key] = self.terminal_colors.get(colors, 0)
            style |= self.cell_colors[key]
        return style

    def _runs(self, screen, cells, left, width):
        key = id(cells), left, width
        if key in self.row_runs:
            cached = self.row_runs[key]
        else:
            runs, text, end, style, start = [], [], -1, 0, 0
            for column in range(left, min(len(cells), left + width)):
                char, fg, bg, attrs = cells[column]
                if attrs.wide_char_spacer or (attrs.wide_char and column + 1 >= left + width):
                    continue
                if attrs.hidden:
                    char = "  " if attrs.wide_char else " "
                cell_style = self._cell_style(screen, fg, bg, attrs)
                if column != end or cell_style != style:
                    if text:
                        runs.append((start - left, "".join(text), style))
                    text, start, style = [], column, cell_style
                text.append(char)
                end = column + (2 if attrs.wide_char else 1)
            if text:
                runs.append((start - left, "".join(text), style))
            # Keep the cells alive so identity keys cannot alias recycled lists.
            cached = cells, runs
        self.next_row_runs[key] = cached
        return cached[1]

    def _terminal_frame(self, screen, x, row, width, height, scroll):
        start, total, lines = screen.frame(scroll["line"], height, follow=scroll["follow"])
        scroll.update(line=start, total=total, visible=height)
        left = max(0, min(scroll.get("left", 0), max(0, screen.cols - width)))
        scroll["left"] = left
        for y, cells in enumerate(lines, row):
            for column, text, style in self._runs(screen, cells, left, width):
                try:
                    self.window.addstr(y, x + column, text, style)
                except curses.error:
                    pass
        return len(lines)

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

    def _refresh(self, cursor=None):
        self.drawn = None
        # Unsupported terminals ignore this private mode. Keep the transaction
        # around physical output only, never engine work or a blocking input read.
        fd = sys.stdout.fileno()
        os.write(fd, b"\x1b[?2026h")
        try:
            self._cursor(cursor)
            self.window.refresh()
        finally:
            os.write(fd, b"\x1b[?2026l")

    def _getch_raw(self):
        try:
            key = self.window.get_wch()
            if key == curses.KEY_MOUSE:
                _, x, y, _, buttons = curses.getmouse()
                return ("mouse", x, y, buttons)
            key = {curses.KEY_SR: "shift-up", curses.KEY_SF: "shift-down",
                   curses.KEY_SLEFT: "shift-left", curses.KEY_SRIGHT: "shift-right"}.get(key, key)
            if isinstance(key, int) and key > curses.KEY_MAX:
                return {b"kLFT5": "ctrl-left", b"kRIT5": "ctrl-right",
                        b"kUP2": "shift-up", b"kDN2": "shift-down",
                        b"kLFT2": "shift-left", b"kRIT2": "shift-right",
                        b"kHOM5": "ctrl-home", b"kEND5": "ctrl-end",
                        b"kbs5": "ctrl-backspace", b"kent2": 10}.get(curses.keyname(key), key)
            if isinstance(key, str) and len(key) == 1 and (ord(key) < 32 or key == "\x7f"):
                return ord(key)
            return key
        except curses.error:
            return None

    def _getch(self):
        if self.pending_keys:
            return self.pending_keys.popleft()
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
        self.drafts[self.selected] = (self.draft, self.cursor)

    def _select(self, session_id):
        if session_id == self.selected:
            return
        self._save_draft()
        self.selected = session_id
        self.draft, self.cursor = self.drafts.get(session_id, ("", 0))
        self.command_query = None
        self.command_index = 0
        self.command_dismissed = False

    def _set_draft(self, text, cursor=None):
        self.draft = text
        self.cursor = len(text) if cursor is None else max(0, min(len(text), cursor))
        self._save_draft()

    def _selected_index(self):
        return next((i for i, session in enumerate(self.state["sessions"]) if session["id"] == self.selected), 0)

    def _cycle_session(self, amount):
        sessions = self.state["sessions"]
        if sessions:
            index = self._selected_index() if self.selected is not None else (-1 if amount > 0 else 0)
            self._select(sessions[(index + amount) % len(sessions)]["id"])

    def _pending(self, session_id):
        return [message for message in self.state["messages"] if message["session_id"] == session_id]

    def _scheduler_line(self):
        scheduler = self.state["scheduler"]
        status = "error" if scheduler.get("error") else "active" if scheduler["active"] else "idle"
        return f"Scheduler {status}"

    def _scheduler_usage_lines(self):
        usage = self.state["scheduler"].get("last_usage")
        if not usage:
            return []
        return ["Scheduler last pass:",
                f"  Input {usage.get('input_tokens', 'unreported')} · cached {usage.get('cache_read_input_tokens', 'unreported')}",
                f"  Output {usage.get('output_tokens', 'unreported')} · reasoning {usage.get('reasoning_output_tokens', 'unreported')} (included in output)",
                f"  {usage['seconds']:.2f}s · {usage.get('requests', '?')} requests · {usage.get('compactions', '?')} compactions",
                f"  Context {usage.get('context_tokens')} / {usage.get('context_window')} ({usage.get('context_usage_source')})", ""]

    def _statistics_line(self):
        sessions = self.state["sessions"]
        running = sum(bool(session.get("active")) for session in sessions)
        queued = sum(message["state"] == "pending" for message in self.state["messages"])
        ready = sum(not any(session.get(key) for key in ("active", "hold", "gate", "blocked"))
                    for session in sessions)
        return f"{running} running · {queued} queued · {ready} ready"

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
        self._add(y, 1, "Sessions", width - 2, curses.A_DIM)
        capacity = max(0, height - 1)
        index = self._selected_index()
        self.sidebar_scroll = max(0, min(self.sidebar_scroll, max(0, len(sessions) - capacity)))
        if capacity and index < self.sidebar_scroll:
            self.sidebar_scroll = index
        elif capacity and index >= self.sidebar_scroll + capacity:
            self.sidebar_scroll = index - capacity + 1
        for row, session in enumerate(sessions[self.sidebar_scroll:self.sidebar_scroll + capacity], y + 1):
            self.session_hits.append((row, width, session["id"]))
            self._add(row, 1, self._session_label(session), width - 2,
                      curses.A_REVERSE if session["id"] == self.selected else 0)

    def _session_icon(self, session):
        active = session.get("active")
        if active:
            return "■" if active.get("stopping") else "≋" if active["mode"] == "readonly" else "✎"
        if session.get("gate") in ("failed", "trapped"):
            return "×"
        if session.get("hold") or session.get("gate") == "interrupted":
            return "■"
        if session.get("blocked"):
            return "…"
        last = session.get("last")
        if last:
            return "●" if last["mode"] == "readonly" else "○"
        return "·"

    def _session_label(self, session):
        return f"S{session['id']} {self._session_icon(session)} {session.get('name') or 'Session'}"

    def _prepare_output(self, session, width, height):
        queue = self._pending(session["id"]) if session else []
        queued = min(len(queue), 3, max(0, height - 2))
        if len(queue) > queued:
            queued = max(0, queued - 1)
        queue_rows = queued + (len(queue) > queued and queued < height - 1)
        visible = max(0, height - 1 - queue_rows)
        columns = max(2, width - 2)
        self.engine.terminal_size = (columns, max(2, visible))
        try:
            screen = self.engine.display(self.selected, *self.engine.terminal_size)
        except (OSError, RuntimeError, ValueError) as error:
            self.ui_error = str(error)
            screen = self.engine.screens.get(self.selected)
        if screen and screen.error:
            self.ui_error = screen.error
        prompt = None
        pending = next((message for message in queue if message["state"] == "pending"), None)
        scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
        if (session and not session.get("active") and (pending or not self._session_status(session))
                and scroll["follow"] and (screen or not session.get("session"))):
            key = (columns, pending["text"] if pending else "", self.state["root"],
                   session.get("next_model") or "Mu/session default")
            if key != self.prompt_key:
                self.prompt_screen = Screen(columns, 2)
                self.prompt_screen.feed(prompt_bytes(live_prompt(key[1], key[2], {
                    "model": {"canonical": key[3]},
                }), pending=True))
                self.prompt_key = key
            prompt = self.prompt_screen
        return screen, prompt, queue, queued, visible

    def _draw_conversation(self, session, x, y, width, height, output):
        if width <= 0 or height <= 0:
            return
        screen, prompt, queue, queued, visible = output
        self.output_rect = (x, y, width, height)
        if session:
            title = f"S{session['id']} · {session['name']}"
            status = self._session_status(session)
            if status:
                title += f" · {status}"
            if self.state["workspace"]["owner"] == session["id"]:
                title += " · ◆"
        else:
            title = "Scheduler"
        scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
        hint = " · History · Ctrl-End for live" if not scroll["follow"] else ""
        row = y + 1
        if queue:
            for message in queue[:queued]:
                self._add(row, x + 1, f"{message['state']} · {message['text']}", width - 2, self.colors.get("warn", 0))
                row += 1
            if len(queue) > queued and row < y + height:
                self._add(row, x + 1, f"… {len(queue) - queued} more queued", width - 2, curses.A_DIM)
                row += 1
        columns = max(2, width - 2)
        prompt_height = min(visible, prompt.frame(0, 0)[1]) if prompt else 0
        output_height = visible - prompt_height
        used = 0
        if screen:
            used = self._terminal_frame(screen, x + 1, row, columns, output_height, scroll)
            if screen.cols > columns:
                hint += " · Shift-←/→ pan"
        elif not prompt:
            message = "Loading Mu history…" if (session and session.get("session")) or (not session and self.state["scheduler"].get("session")) else "No output yet."
            self._add(row, x + 1, message, columns, curses.A_DIM)
        if prompt:
            self._terminal_frame(prompt, x + 1, row + used, columns, prompt_height,
                                 {"follow": True, "line": 0})
        self._add(y, x + 1, _clip(title, max(0, width - 2 - _width(hint))) + hint,
                  width - 2, self.colors.get("title", 0))

    def _notice(self):
        if self.ui_error:
            return self.ui_error
        if error := self.state["scheduler"].get("error"):
            return error
        session = self._session()
        if session:
            if session.get("gate") == "trapped" and not session.get("hold") and session.get("blocked"):
                return session["blocked"]
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
        start = max(0, cursor_row - rows + 1)
        hidden = (("↑", start), ("↓", max(0, len(lines) - start - rows)))
        overflow = " · ".join(f"{arrow} {count} more" for arrow, count in hidden if count)
        if label_y >= 0:
            self._add(label_y, 0, "─" * max(0, width - 1), attr=curses.A_DIM)
            session = self._session()
            recipient = f"To S{session['id']} · {session['name']}" if session else "New session · type a message to start"
            label_width = width - 3
            if overflow:
                overflow_x = max(1, width - _width(overflow) - 3)
                label_width = overflow_x - 2
                self._add(label_y, overflow_x, f" {overflow} ", width - overflow_x - 1, curses.A_DIM)
            self._add(label_y, 1, f" {recipient} ", label_width, self.colors.get("title", 0))
        for offset, line in enumerate(lines[start:start + rows]):
            row = top + offset
            self._add(row, 1, ">" if offset == 0 else "·", attr=self.colors.get("title", 0))
            self._add(row, 3, line, width - 5)
        row = cursor_row - start
        if 0 <= row < rows:
            return row, min(max(0, width - 1), 3 + cursor_col)
        return None

    def _view_key(self, size, screen_stamp):
        # Engine state contains mutable dictionaries shared with the scheduler.
        # Serialize the snapshot rather than retaining aliases for comparison.
        return (size, screen_stamp, json.dumps(self.state), self.selected, self.draft, self.cursor,
                self.command_index, self.command_dismissed, self.sidebar_scroll,
                tuple(self.scrolls.get(self.selected, {}).items()), self.ui_error)

    def _draw_main(self):
        height, width = self.window.getmaxyx()
        if height <= 0 or width <= 0:
            return
        session = self._session()
        output = None
        geometry = None
        if height >= 6:
            geometry = self._composer_geometry(height, width)
            label_y, composer_top, composer_rows, notice_y = geometry
            body_y, body_height = 1, max(0, label_y - 1)
            split = width >= 62 and body_height >= 4
            sidebar_width = max(20, min(30, width // 3)) if split else -1
            output = self._prepare_output(session, width - sidebar_width - 1, body_height)
        screen = output[0] if output else None
        screen_stamp = (screen, screen.revision) if screen else None
        size = height, width, geometry
        if self._view_key(size, screen_stamp) == self.drawn:
            return
        self.window.erase()
        self.session_hits = []
        self.output_rect = None
        self.next_row_runs = {}
        title = f"Mu Board · {self.state['root']}"
        scheduler_text = self._scheduler_line()
        statistics = self._statistics_line()
        summary = f"{statistics} · {scheduler_text}"
        if _width(title) + _width(summary) + 4 <= width:
            scheduler_text = summary
        scheduler_x = max(1, width - _width(scheduler_text) - 1)
        self._add(0, 1, title, max(0, scheduler_x - 2), curses.A_DIM)
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
                self._add(height - 1, 0, "Tab sessions · /scheduler · /quit", attr=curses.A_DIM)
            self._refresh()
            self.row_runs = {}
            self.drawn = self._view_key(size, screen_stamp)
            return

        if split:
            self._draw_sidebar(body_y, sidebar_width, body_height)
            for row in range(body_y, body_y + body_height):
                self._add(row, sidebar_width, "│", 1, curses.A_DIM)
            self._draw_conversation(session, sidebar_width + 1, body_y, width - sidebar_width - 1, body_height, output)
        else:
            self._draw_conversation(session, 0, body_y, width, body_height, output)

        cursor = self._draw_composer(label_y, composer_top, composer_rows, width)
        self._draw_commands(label_y, width)
        if notice := self._notice():
            self._add(notice_y, 1, notice, width - 2, self.colors.get("warn", 0))
        self._add(height - 1, 1, "Tab/Shift-Tab sessions · PgUp/PgDn output · /scheduler · /help", attr=curses.A_DIM)
        self._refresh((composer_top + cursor[0], cursor[1]) if cursor else None)
        self.row_runs = self.next_row_runs
        self.drawn = self._view_key(size, screen_stamp)

    def _command_matches(self):
        if self.draft != self.command_query:
            self.command_query = self.draft
            self.command_index = 0
            self.command_dismissed = False
        if (self.command_dismissed or self.cursor != len(self.draft)
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
        header = f" Commands {self.command_index + 1}/{len(matches)} · ↑↓ select · Tab/→ fill · Enter run · Esc hide "
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
        elif key in (9, curses.KEY_RIGHT, 13, curses.KEY_ENTER):
            name = matches[self.command_index][0].split()[0]
            fill = key in (9, curses.KEY_RIGHT)
            self._set_draft(name + (" " if fill else ""))
            if not fill:
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
            return
        if text.startswith("/") and not text.startswith("//"):
            self._command(text)
            return
        message = text[1:] if text.startswith("//") else self.draft
        if self.selected is None:
            result = self._request({"op": "new", "text": message})
            if result is not None:
                self._set_draft("")
                self._select(result["session_id"])
        elif self._request({"op": "send", "session_id": self.selected, "text": message}) is not None:
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
                    *self._scheduler_usage_lines(),
                    "Use /model session to change only the selected session.",
                    "Use /model scheduler, /model worker, or /model both to change defaults.",
                    "Changes apply to later invocations; active workers keep their current model.",
                ])
        elif command == "/interrupt":
            self._interrupt()
        elif command == "/resume":
            self._resume()
        elif command == "/schedule":
            self._request({"op": "schedule"})
        elif command == "/scheduler":
            self._select(None)
        elif command == "/links":
            screen = self.engine.screens.get(self.selected)
            self._info("Terminal links · targets only; nothing opens automatically",
                       screen.links() or ["No links"] if screen else ["No terminal output yet"])
        elif command == "/help":
            self._help()
        elif command == "/quit":
            self._quit()

    def _close(self):
        session = self._session()
        if session is None:
            self.ui_error = "Select a session to close."
            return
        if session.get("active"):
            self.ui_error = "Session is not idle; wait for it to finish or use /interrupt before closing."
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
        self.scrolls.pop(session_id, None)

    def _resume(self):
        session = self._session()
        if session is None:
            self.ui_error = "Select a session to resume."
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
                                 f"{count} agent(s) are active, including the scheduler if running. Stop them and quit?"):
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
            self._refresh()
            self._getch()

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
            self._refresh()
            key = self._getch()
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

    def _confirm(self, title, message):
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._add(0, 1, title, width - 2, self.colors.get("warn", 0))
            lines = _lines(message, max(2, width - 4))
            for row, line in enumerate(lines[:max(0, height - 3)], 2):
                self._add(row, 2, line, width - 4)
            self._add(height - 1, 0, "y confirm · n/Q/Esc/Enter cancel", attr=curses.A_DIM)
            self._refresh()
            key = self._getch()
            if key in (3, 27, 10, 13, curses.KEY_ENTER, "n", "N", "q", "Q"):
                return False
            if key in ("y", "Y"):
                return True
        return False

    def _help(self):
        lines = ["Commands:", *(f"  {name}  {description}" for name, description in self.COMMANDS.items()), "",
                 "Session icons:", "  ✎ writing · ≋ reading · ○ idle after writing · ● idle after reading",
                 "  · new/idle · × failed/trapped · ■ held/interrupted/stopping · … blocked", "",
                 "Keyboard:",
                 "  Ctrl-N         Create and select a session, preserving the current draft",
                 "  Tab/Shift-Tab  Next/previous session (Tab fills an open command panel)",
                 "  ↑/↓/←/→        Edit the prompt; typing always goes to the composer",
                 "  Shift-↑/↓      Scroll output one line", "  Shift-←/→      Pan wider output after resize",
                 "  Ctrl-Home/End  Oldest retained output / follow live output",
                 "  Enter          Queue the prompt", "  Shift-Enter    Insert a newline (Alt-Enter and Ctrl-J also work)",
                 "  PgUp/PgDn      Scroll output by a page with overlap",
                 "  Mouse wheel    Scroll output under the pointer; click a sidebar session to select it",
                 "  Shift-drag     Native terminal selection in terminals supporting this bypass",
                 "  Ctrl-C         Clear the draft only; /interrupt stops and holds the session",
                 "  Ctrl-D         Close the selected idle session; quit if no sessions remain",
                 "  /              List commands; ↑/↓ select, Tab/→ fill, Enter run, Esc hide",
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
            self._refresh()
            key = self._getch()
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
        if scroll["follow"]:
            scroll["line"] = max(0, scroll.get("total", 0) - visible)
        scroll["line"] = max(0, scroll["line"] + amount)
        scroll["follow"] = False if amount < 0 else scroll["follow"]
        if amount > 0:
            bottom = max(0, scroll.get("total", 0) - visible)
            scroll["line"] = min(scroll["line"], bottom)
            scroll["follow"] = scroll["line"] >= bottom

    @staticmethod
    def _wheel(key):
        if isinstance(key, tuple) and key[0] == "mouse":
            if key[3] & curses.BUTTON4_PRESSED:
                return -3
            if key[3] & curses.BUTTON5_PRESSED:
                return 3
        return 0

    def _mouse_key(self, key):
        _, x, y, buttons = key
        if amount := self._wheel(key):
            if self.output_rect:
                left, top, width, height = self.output_rect
                if left <= x < left + width and top <= y < top + height:
                    self._view_scroll(amount)
        elif buttons & curses.BUTTON1_PRESSED:
            for row, width, session_id in self.session_hits:
                if y == row and 0 <= x < width:
                    self._select(session_id)
                    break

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
                        "1;2A": "shift-up", "1;2B": "shift-down",
                        "1;2D": "shift-left", "1;2C": "shift-right",
                        "1;5H": "ctrl-home", "1;5F": "ctrl-end",
                        "1;5~": "ctrl-home", "7;5~": "ctrl-home",
                        "4;5~": "ctrl-end", "8;5~": "ctrl-end",
                        "13;2u": 10, "27;2;13~": 10,
                        "8;5u": "ctrl-backspace", "127;5u": "ctrl-backspace",
                        "27;5;8~": "ctrl-backspace", "27;5;127~": "ctrl-backspace"}.get(sequence)
            if key in (10, 13, curses.KEY_ENTER):
                return 10
            if key is not None:
                self.pending_keys.appendleft(key)
            return 27
        finally:
            self.window.timeout(50)

    def _main_key(self, key):
        if isinstance(key, tuple) and key[0] == "mouse":
            self._mouse_key(key)
            return
        if key == 3:
            self._set_draft("")
            return
        if key == 4:
            if self.state["sessions"]:
                self._close()
            else:
                self._quit()
            return
        if key == 14:
            result = self._request({"op": "new"})
            if result is not None:
                self._select(result["session_id"])
            return
        if self._command_key(key):
            return
        if key == 9:
            self._cycle_session(1)
            return
        if key == curses.KEY_BTAB:
            self._cycle_session(-1)
            return
        if key == 27:
            self.command_dismissed = True
            return
        if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = max(1, self.scrolls.get(self.selected, {}).get("visible", 1) - 2)
            self._view_scroll(-step if key == curses.KEY_PPAGE else step)
            return
        if key in ("shift-up", "shift-down"):
            self._view_scroll(-1 if key == "shift-up" else 1)
            return
        if key in ("shift-left", "shift-right"):
            scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
            scroll["left"] = max(0, scroll.get("left", 0) + (-8 if key == "shift-left" else 8))
            return
        if key in ("ctrl-home", "ctrl-end"):
            scroll = self.scrolls.setdefault(self.selected, {"follow": True, "line": 0})
            scroll.update(follow=key == "ctrl-end", line=0)
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

    def _scroll_key(self, key):
        return bool(self._wheel(key)) or key in (
            curses.KEY_PPAGE, curses.KEY_NPAGE, "shift-up", "shift-down",
            "shift-left", "shift-right", "ctrl-home", "ctrl-end")

    def _coalesce_scroll(self):
        # Bound the batch so continuous wheel/key repeat cannot starve ticks.
        try:
            for _ in range(31):
                self.window.timeout(0)
                key = self._getch()
                if key is None:
                    break
                if not self._scroll_key(key):
                    self.pending_keys.appendleft(key)
                    break
                self._main_key(key)
        finally:
            self.window.timeout(50)

    def _main(self):
        self._draw_main()
        while not self.engine.done:
            self._tick()
            if self.engine.done:
                break
            self._draw_main()
            key = self._getch()
            self._main_key(key)
            if self._scroll_key(key):
                self._coalesce_scroll()


def run_ui(engine):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Mu Board UI requires an interactive terminal")
    curses.wrapper(lambda window: _UI(window, engine)._main())
