"""Prompt-first task board and read-only, live Mu output panes."""

from __future__ import annotations

import curses
import sys
import time
import unicodedata
from types import SimpleNamespace


COMMANDS = {
    "/help": "Help and local commands",
    "/models": "Choose PM/worker models and reasoning effort",
    "/quit": "Quit; confirm first if work is active",
}


def _text(value) -> str:
    return "".join(
        "    " if char == "\t" else "�" if char != "\n" and unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in str(value or "")
    )


def _width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _clip(text: str, width: int) -> str:
    result, used = [], 0
    for char in text:
        used += _width(char)
        if used > width:
            break
        result.append(char)
    return "".join(result)


def _lines(value, width: int) -> list[str]:
    """Wrap by terminal cells, preserving log indentation and blank lines."""
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
    def __init__(self, window, engine):
        self.window, self.engine = window, engine
        self.state = engine.state()
        self.selected = None
        self.pane = None
        self.detail_focus = False
        self.detail_percent = 60
        self.draft, self.cursor = "", 0
        self.board_scroll = 0
        self.next_tick = 0.0
        self.ui_error = ""
        self.colors = {}
        curses.raw()
        curses.nonl()
        curses.set_escdelay(25)
        window.keypad(True)
        window.timeout(50)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for index, (name, color) in enumerate((("title", curses.COLOR_CYAN), ("warn", curses.COLOR_YELLOW),
                                                    ("error", curses.COLOR_RED)), 1):
                curses.init_pair(index, color, -1)
                self.colors[name] = curses.color_pair(index) | curses.A_BOLD

    def _add(self, y, x, text, width=None, attr=0):
        height, columns = self.window.getmaxyx()
        if not 0 <= y < height or not 0 <= x < columns:
            return
        width = min(columns - x - 1, width if width is not None else columns)
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

    def _getch(self):
        try:
            key = self.window.get_wch()
            return ord(key) if isinstance(key, str) and (ord(key) < 32 or key == "\x7f") else key
        except curses.error:
            return None

    def _tick(self):
        if time.monotonic() < self.next_tick:
            return
        self.next_tick = time.monotonic() + 0.1
        try:
            self.engine.tick()
        except Exception as error:
            self.ui_error = str(error)
        self.state = self.engine.state()
        tasks = self.state["tasks"]
        if not any(t["id"] == self.selected for t in tasks):
            self._select(None)

    def _request(self, request, *, clear_error=True):
        try:
            result = self.engine.request(request)
            if clear_error:
                self.ui_error = ""
            self.state = self.engine.state()
            return result
        except Exception as error:
            self.ui_error = str(error)
            return None

    def _task(self):
        return next((t for t in self.state["tasks"] if t["id"] == self.selected), None)

    def _move(self, amount):
        tasks = self.state["tasks"]
        if tasks:
            index = next((i for i, t in enumerate(tasks) if t["id"] == self.selected), None)
            index = 0 if index is None else max(0, min(len(tasks) - 1, index + amount))
            self._select(tasks[index]["id"])

    def _select(self, task_id):
        if task_id != self.selected:
            self.pane = self._new_view(task_id) if task_id is not None else None
        self.selected = task_id
        if task_id is None:
            self.detail_focus = False

    def _focus_task(self):
        if self.selected is None:
            self._move(0)
        if self.selected is not None:
            self.detail_focus = not self.detail_focus

    def _header(self, title):
        self._add(0, 1, title, attr=self.colors.get("title", 0))

    def _notice(self):
        task = self._task()
        return self.ui_error or self.state["error"] or (task["execution"]["question"] if task else "") or (
            "PM is working…" if self.state["pm"] else "Worker dispatch paused" if self.state["paused"] else
            "Waiting for PM dispatch" if not self.state["worker"] and self.state["dispatch"] is None
            and any(t["state"] == "queued" for t in self.state["tasks"]) else ""
        )

    def _draw_main(self):
        self.window.erase()
        height, width = self.window.getmaxyx()
        self._header(f"Mu Board · {self.state['root']}")
        left = width
        if self.selected is not None:
            minimum = min(28, (width - 1) // 2)
            left = max(minimum, min(width - 1 - minimum, width * (100 - self.detail_percent) // 100))
            for row in range(1, height - 1):
                self._add(row, left, "│", 1, curses.A_DIM)
            self._draw_view(self.pane, left + 1, 1, width - left - 1, height - 2)

        def add(y, x, text, attr=0):
            self._add(y, x, text, left - x - 1, attr)

        prompt_rows = min(3, max(1, height // 8))
        prompt_y = height - 1 - prompt_rows
        notice_y = prompt_y - 2
        tasks = self.state["tasks"]
        visible = min(max(1, len(tasks)), max(1, (notice_y - 4) // 2))
        chat_y = visible + 4
        chat_rows = max(0, notice_y - chat_y)
        index = next((i for i, t in enumerate(tasks) if t["id"] == self.selected), 0)
        self.board_scroll = max(0, min(self.board_scroll, index))
        if index >= self.board_scroll + visible:
            self.board_scroll = index - visible + 1
        add(1, 1, f"Tasks · {len(tasks)} · ↑↓ select", attr=curses.A_DIM)
        for row, task in enumerate(tasks[self.board_scroll:self.board_scroll + visible], 2):
            state = task["state"].replace("needs_input", "blocked")
            marker = "›" if task["id"] == self.selected else " "
            add(row, 1, f"{marker} T{task['id']} {state} · {task['title']}",
                attr=curses.A_REVERSE if task["id"] == self.selected else 0)
        if not tasks:
            add(2, 2, "No tasks yet. Message the PM below.")
        if chat_rows:
            add(chat_y - 1, 1, "─ PM conversation · F2 history " + "─" * left, attr=curses.A_DIM)
            messages = [m for m in self.state["messages"] if m["task_id"] is None]
            lines = []
            for message in reversed(messages):
                lines[:0] = _lines(f"{message['role']}: {message['content']}", left - 4)
                if len(lines) >= chat_rows:
                    break
            for row, line in enumerate(lines[-chat_rows:], chat_y):
                add(row, 2, line)
        add(notice_y, 1, self._notice(), attr=self.colors.get("warn", 0))
        add(prompt_y - 1, 1, "─ Message PM · Enter send " + "─" * left,
            attr=curses.A_DIM if self.detail_focus else self.colors.get("title", 0))
        draft_lines = _lines(self.draft, left - 5)
        before = _lines(self.draft[:self.cursor], left - 5)
        cursor_row, cursor_col = len(before) - 1, _width(before[-1])
        start = max(0, cursor_row - prompt_rows + 1)
        for row, line in enumerate(draft_lines[start:start + prompt_rows], prompt_y):
            add(row, 1, ">" if row == prompt_y else "·", attr=self.colors.get("title", 0))
            add(row, 3, line)
        if self.detail_focus:
            footer = "Tab PM/commands  Esc close  ↑↓/PgUp/Dn scroll  Home/End  [] runs  F5/F6 size  ^C quit"
        elif self.selected is not None:
            footer = "Enter send  ↑↓ tasks  Tab output  Esc close  /help  F5/F6 size  F2 PM  ^C quit"
        else:
            footer = "Enter send/open  ↑↓ tasks  Tab output  /help commands  F2 PM  ^C quit"
        self._add(height - 1, 0, footer, attr=curses.A_DIM)
        self._cursor(None if self.detail_focus else
                     (prompt_y + cursor_row - start, min(left - 2, 3 + cursor_col)))
        self.window.refresh()

    def _insert(self, text):
        self.draft = self.draft[:self.cursor] + text + self.draft[self.cursor:]
        self.cursor += len(text)

    def _submit(self):
        text = self.draft.strip()
        if not text:
            self._focus_task()
            return
        if text.startswith("/") and not text.startswith("//"):
            self._command(text)
            return
        if self._request(dict(op="reply", task_id=None, text=text[1:] if text.startswith("//") else text)) is not None:
            self.draft, self.cursor = "", 0

    def _command(self, text):
        command, *args = text.split()
        if command not in COMMANDS:
            self.ui_error = "Ask the PM in plain language to manage tasks. Local commands: /help, /models, /quit."
            return
        if args:
            self.ui_error = f"Usage: {command}"
            return
        self.ui_error = ""
        self.draft, self.cursor = "", 0
        if command == "/models":
            self._models()
        elif command == "/quit":
            self._quit()
        else:
            commands = list(COMMANDS)
            choice = self._pick("Ask the PM to prioritize, pause, resume, or approve work in plain language",
                                [f"{name}  {COMMANDS[name]}" for name in commands])
            if choice is not None:
                self.draft = commands[choice]
                self.cursor = len(self.draft)

    def _main(self):
        while not self.engine.done:
            self._tick()
            if self.engine.done:
                break
            self._draw_main()
            key = self._getch()
            if key == 3 or (key == 4 and not self.detail_focus and not self.draft):
                self._quit()
            elif key == 27:
                self._select(None)
            elif key == 9:
                self._focus_task()
            elif key in (curses.KEY_F5, curses.KEY_F6) and self.selected is not None:
                self.detail_percent = max(30, min(75, self.detail_percent + (5 if key == curses.KEY_F6 else -5)))
            elif key == curses.KEY_F2:
                self._view()
            elif self.detail_focus:
                self._view_key(self.pane, key)
            elif key in (13, curses.KEY_ENTER, 19):
                self._submit()
            elif key == 10:
                self._insert("\n")
            elif key == curses.KEY_UP:
                self._move(-1)
            elif key == curses.KEY_DOWN:
                self._move(1)
            elif key == curses.KEY_LEFT:
                self.cursor = max(0, self.cursor - 1)
            elif key == curses.KEY_RIGHT:
                self.cursor = min(len(self.draft), self.cursor + 1)
            elif key in (curses.KEY_HOME, 1):
                self.cursor = self.draft.rfind("\n", 0, self.cursor) + 1
            elif key in (curses.KEY_END, 5):
                end = self.draft.find("\n", self.cursor)
                self.cursor = len(self.draft) if end < 0 else end
            elif key in (curses.KEY_BACKSPACE, 8, 127) and self.cursor:
                self.draft = self.draft[:self.cursor - 1] + self.draft[self.cursor:]
                self.cursor -= 1
            elif key == curses.KEY_DC:
                self.draft = self.draft[:self.cursor] + self.draft[self.cursor + 1:]
            elif key == 21:
                self.draft, self.cursor = "", 0
            elif isinstance(key, str) and key.isprintable():
                self._insert(key)

    def _new_view(self, task_id=None):
        return SimpleNamespace(task_id=task_id, detail=None, follow=True, latest=True,
                               scroll=0, run_index=0, buffers={}, next_fetch=0.0,
                               layout_key=None, lines=[], visible=1, bottom=0)

    def _draw_view(self, view, x, y, width, height):
        """Tail durable output within a pane; never attach input or launch Mu."""
        if time.monotonic() >= view.next_fetch:
            fresh = self._request(dict(op="show", task_id=view.task_id), clear_error=False)
            if fresh is not None:
                view.detail = fresh
            if view.detail is not None:
                runs = view.detail["runs"]
                if view.latest:
                    view.run_index = max(0, len(runs) - 1)
                if runs:
                    run = runs[view.run_index]
                    buffer = view.buffers.setdefault(run["id"], dict(text="", offset=0))
                    chunk = self._request(dict(op="log", run_id=run["id"], offset=buffer["offset"]), clear_error=False)
                    if chunk is not None:
                        buffer["text"] += chunk["text"]
                        buffer["offset"] = chunk["offset"]
                        buffer["source"] = chunk["source"]
            view.next_fetch = time.monotonic() + 0.2

        def add(row, text, attr=0):
            if row < height:
                self._add(y + row, x + 1, text, width - 2, attr)

        if view.detail is None:
            add(0, self.ui_error or "Loading…", self.colors.get("warn", 0))
            return
        detail = view.detail
        runs, task = detail["runs"], detail["task"]
        run = runs[view.run_index] if runs else None
        text = view.buffers.get(run["id"], {}).get("text", "") if run else ""
        if task:
            heading = f"T{task['id']} · {task['title']} · {task['state']}"
            context = f"Task note\n{task['note']}\n"
            messages = [m for m in detail["messages"] if m["role"] != "worker"]
            notice = task["execution"]["question"]
        else:
            heading, context, notice = "PM · conversation and execution", "", self.state["error"]
            messages = detail["messages"]
        context += "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        if run:
            context += f"\n\n── Mu output · run {view.run_index + 1}/{len(runs)} · {run['id']} ──\n"
            text = context + (text or "Waiting for Mu output…")
        else:
            text = context + "\n\nNo worker run yet." if task else context or "No conversation yet. Type a message on the board."
        add(0, heading, self.colors.get("title", 0))
        status = f"{'following' if view.follow else 'scrollback'} · read-only"
        if run:
            status += f" · {run['status']} · {view.buffers.get(run['id'], {}).get('source', 'Mu output')} · {run.get('model') or 'Mu/session default'}"
        add(1, status, curses.A_DIM)
        add(2, self.ui_error or notice, self.colors.get("warn", 0))
        key = (text, width)
        if key != view.layout_key:
            view.lines, view.layout_key = _lines(text, width - 2), key
        view.visible = max(1, height - 3)
        view.bottom = max(0, len(view.lines) - view.visible)
        view.scroll = view.bottom if view.follow else max(0, min(view.scroll, view.bottom))
        for row, line in enumerate(view.lines[view.scroll:view.scroll + view.visible], 3):
            add(row, line)

    def _view_key(self, view, key):
        if key in (curses.KEY_UP, curses.KEY_PPAGE, "k"):
            view.follow = False
            view.scroll = max(0, view.scroll - (view.visible if key == curses.KEY_PPAGE else 1))
        elif key in (curses.KEY_DOWN, curses.KEY_NPAGE, "j"):
            view.scroll = min(view.bottom, view.scroll + (view.visible if key == curses.KEY_NPAGE else 1))
            view.follow = view.scroll == view.bottom
        elif key == curses.KEY_HOME:
            view.follow, view.scroll = False, 0
        elif key == curses.KEY_END:
            view.follow = True
        elif key in ("[", "]") and view.detail and view.detail["runs"]:
            runs = view.detail["runs"]
            view.run_index = max(0, min(len(runs) - 1, view.run_index + (-1 if key == "[" else 1)))
            view.latest, view.follow = view.run_index == len(runs) - 1, True
            view.next_fetch = 0.0

    def _view(self):
        view = self._new_view()
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._draw_view(view, 0, 0, width, height - 1)
            self._add(height - 1, 0, "Esc back/commands  ↑↓/PgUp/PgDn scroll  Home start  End follow  [] runs  ^C quit", attr=curses.A_DIM)
            self._cursor()
            self.window.refresh()
            key = self._getch()
            if key in (27, "q"):
                return
            if key == 3:
                self._quit()
            else:
                self._view_key(view, key)

    def _pick(self, title, options, selected=0):
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._header(title)
            visible = max(1, height - 5)
            start = max(0, selected - visible + 1)
            for index in range(start, min(len(options), start + visible)):
                self._add(2 + index - start, 2, options[index], attr=curses.A_REVERSE if index == selected else 0)
            self._add(height - 2, 1, self.ui_error, attr=self.colors.get("error", 0))
            self._add(height - 1, 0, "↑↓ select  Enter choose  Esc back  ^C quit", attr=curses.A_DIM)
            self._cursor()
            self.window.refresh()
            key = self._getch()
            if key == 27:
                return None
            if key in (10, 13, curses.KEY_ENTER):
                return selected
            if key in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                selected = min(len(options) - 1, selected + 1)
            elif key == 3:
                self._quit()
        return None

    def _models(self):
        response = self._request(dict(op="models"))
        if response is None:
            return
        role = self._pick("Models for…", ["Both PM and workers", "Project manager", "Workers"])
        if role is None:
            return
        roles = (("pm", "worker"), ("pm",), ("worker",))[role]
        current = response["selected"][roles[0]] or ""
        base, _, effort = current.partition(":")
        models = response["available"]
        names = [m["id"] for m in models]
        choice = self._pick("Model · applies to the next invocation", ["Mu/session default", *names],
                            names.index(base) + 1 if base in names else 0)
        if choice is None:
            return
        reference = None
        if choice:
            model = models[choice - 1]
            reference = model["id"]
            efforts = model.get("supported_efforts") or []
            if efforts:
                choice = self._pick(f"Effort · {reference}", ["Default effort", *efforts],
                                    efforts.index(effort) + 1 if reference == base and effort in efforts else 0)
                if choice is None:
                    return
                if choice:
                    reference += ":" + efforts[choice - 1]
        self._request(dict(op="set_models", models={role: reference for role in roles}))

    def _confirm(self, title, message):
        while not self.engine.done:
            self._tick()
            self.window.erase()
            height, width = self.window.getmaxyx()
            self._header(title)
            for row, line in enumerate(_lines(message, width - 4)[:max(1, height - 4)], 2):
                self._add(row, 2, line)
            self._add(height - 1, 0, "y confirm  n/Esc cancel", attr=curses.A_DIM)
            self._cursor()
            self.window.refresh()
            key = self._getch()
            if key in (27, 3, "n", "N"):
                return False
            if key in ("y", "Y"):
                return True
        return False

    def _quit(self):
        # Do not tick before deciding: an idle board must not dispatch work just
        # because the user asked to exit. Queued tasks stay persisted.
        busy = self.state["pm"] or self.state["worker"] or any(t["state"] in ("running", "review") for t in self.state["tasks"])
        if busy and not self._confirm("Stop work and quit?", "Work is running or awaiting review. Stop and quit? Queued tasks, checkout changes, and all output will be kept."):
            return
        if self._request(dict(op="shutdown", mode="stop")) is None:
            return
        while not self.engine.done:
            self._tick()
            self.window.erase()
            self._header("Stopping Mu…")
            self._add(2, 1, "Preserving tasks and output; waiting for owned processes to stop.")
            self._cursor()
            self.window.refresh()
            self._getch()


def run_ui(engine):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Mu Board UI requires an interactive terminal")
    curses.wrapper(lambda window: _UI(window, engine)._main())
