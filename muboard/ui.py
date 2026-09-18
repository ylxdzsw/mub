"""Curses user interface for Mu Board.

The UI deliberately knows only the engine protocol.  In particular, run logs
are obtained through ``engine.request`` rather than by opening their paths.
"""

from __future__ import annotations

import curses
import sys
import textwrap
import time
import unicodedata
from typing import Any


GROUPS = (
    ("Needs you", "needs"),
    ("Inbox", "inbox"),
    ("Running", "running"),
    ("Queued", "queued"),
    ("Review", "review"),
    ("Completed", "completed"),
)


def _text(value: Any) -> str:
    """Make model text safe for curses without interpreting terminal escapes."""
    if value is None:
        return ""
    result: list[str] = []
    for char in str(value):
        category = unicodedata.category(char)
        if char == "\n":
            result.append(char)
        elif char == "\t":
            result.append("    ")
        elif category == "Cc" or category == "Cf" or ord(char) in range(127, 160):
            result.append("�")
        else:
            result.append(char)
    return "".join(result)


def _lines(value: Any, width: int) -> list[str]:
    value = _text(value)
    width = max(1, width)
    result: list[str] = []
    for line in value.splitlines() or [""]:
        result.extend(textwrap.wrap(line, width=width, break_long_words=True,
                                    break_on_hyphens=False) or [""])
    return result


class _UI:
    def __init__(self, window: Any, engine: Any) -> None:
        self.window = window
        self.engine = engine
        self.state: dict[str, Any] = {
            "root": "", "paused": False, "stopping": None, "error": None,
            "hold": None, "pm": None, "worker": None, "tasks": [],
            "messages": [], "decisions": [], "runs": [],
        }
        self.selected: int | None = None
        self.ui_error = ""
        self.last_request_ok = False
        self.next_tick = 0.0
        self.board_scroll = 0
        self.colors: dict[str, int] = {}
        self._setup_curses()

    def _setup_curses(self) -> None:
        curses.raw()  # Deliver Ctrl-S/Ctrl-C to dialogs, rather than tty flow control.
        self.window.keypad(True)
        self.window.timeout(45)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_CYAN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_GREEN, -1)
                curses.init_pair(4, curses.COLOR_RED, -1)
                curses.init_pair(5, curses.COLOR_MAGENTA, -1)
                self.colors = {"title": curses.color_pair(1) | curses.A_BOLD,
                               "warn": curses.color_pair(2) | curses.A_BOLD,
                               "good": curses.color_pair(3),
                               "error": curses.color_pair(4) | curses.A_BOLD,
                               "accent": curses.color_pair(5)}
            except curses.error:
                self.colors = {}

    def _attr(self, name: str, extra: int = 0) -> int:
        return self.colors.get(name, 0) | extra

    def _add(self, y: int, x: int, value: Any, width: int | None = None,
             attr: int = 0) -> None:
        height, columns = self.window.getmaxyx()
        if y < 0 or y >= height or x >= columns or width == 0:
            return
        value = _text(value).replace("\n", " ")
        available = columns - max(0, x) - 1
        if width is None:
            width = available
        width = min(max(0, width), max(0, available))
        if width <= 0:
            return
        try:
            self.window.addnstr(y, max(0, x), value, width, attr)
        except curses.error:
            # A wide glyph at the edge of a small terminal can make curses
            # reject the whole write.  The next refresh will try again.
            try:
                self.window.addstr(y, max(0, x), value[:max(1, width - 1)], attr)
            except curses.error:
                pass

    def _clear(self) -> None:
        try:
            self.window.erase()
        except curses.error:
            pass

    def _tick(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self.next_tick:
            return
        self.next_tick = now + 0.1
        try:
            self.engine.tick()
        except Exception as exc:
            self.ui_error = f"tick: {exc}"
        try:
            value = self.engine.state()
            if isinstance(value, dict):
                self.state = value
        except Exception as exc:
            self.ui_error = f"state: {exc}"
        self._keep_selection()

    def _request(self, request: dict[str, Any]) -> Any:
        self.last_request_ok = False
        try:
            result = self.engine.request(request)
            if isinstance(result, dict) and result.get("error"):
                self.ui_error = _text(result["error"])
            else:
                self.ui_error = ""
                self.last_request_ok = True
            self._tick(force=True)
            return result
        except (ValueError, RuntimeError) as exc:
            self.ui_error = _text(exc)
        except Exception as exc:
            self.ui_error = f"request: {_text(exc)}"
        return None

    def _getch(self) -> Any:
        try:
            key = self.window.get_wch()
            if isinstance(key, str) and (ord(key) < 32 or key == "\x7f"):
                return ord(key)
            return key
        except curses.error:
            return None

    def _tasks(self) -> list[dict[str, Any]]:
        return [task for task in self.state.get("tasks", [])
                if isinstance(task, dict) and isinstance(task.get("id"), int)]

    def _group(self, task: dict[str, Any]) -> str:
        state = task.get("state")
        if state in {"needs_input", "failed"}:
            return "needs"
        if state in {"done", "cancelled"}:
            return "completed"
        return state if state in {"inbox", "running", "queued", "review"} else "inbox"

    def _ordered_tasks(self) -> list[dict[str, Any]]:
        tasks = self._tasks()
        return [task for _, group in GROUPS
                for task in sorted(tasks, key=lambda t: (-t.get("priority", 0), t["id"]))
                if self._group(task) == group]

    def _keep_selection(self) -> None:
        tasks = self._ordered_tasks()
        ids = {task["id"] for task in tasks}
        if self.selected not in ids:
            self.selected = tasks[0]["id"] if tasks else None

    def _selected_task(self) -> dict[str, Any] | None:
        for task in self._tasks():
            if task.get("id") == self.selected:
                return task
        return None

    def _move(self, amount: int) -> None:
        tasks = self._ordered_tasks()
        if not tasks:
            self.selected = None
            return
        current = next((i for i, task in enumerate(tasks)
                        if task.get("id") == self.selected), 0)
        current = max(0, min(len(tasks) - 1, current + amount))
        self.selected = tasks[current]["id"]

    def _header(self, title: str) -> None:
        height, width = self.window.getmaxyx()
        root = self.state.get("root") or "project"
        self._add(0, 0, f" Mu Board  ·  {root}", max(1, width - 1),
                  self._attr("title"))
        self._add(1, 1, title, max(1, width - 2), self._attr("accent"))
        status: list[str] = []
        if self.state.get("paused"):
            status.append("PAUSED")
        if self.state.get("hold") is not None:
            status.append(f"checkout T{self.state["hold"]}")
        if self.state.get("stopping"):
            status.append(f"stopping: {self.state['stopping']}")
        pm = self.state.get("pm")
        worker = self.state.get("worker")
        if pm:
            status.append(f"PM {pm.get('status', 'running') if isinstance(pm, dict) else 'running'}")
        if worker:
            status.append(f"worker {worker.get('status', 'running') if isinstance(worker, dict) else 'running'}")
        self._add(1, max(1, width - min(width - 2, len(" | ".join(status)) + 2)),
                  " | ".join(status), max(1, width - 3), self._attr("warn"))
        models = self.state.get("models", {})
        self._add(2, 1, f"PM: {models.get('pm') or 'Mu/session default'}  ·  Worker: {models.get('worker') or 'Mu/session default'}",
                  max(1, width - 2))

    def _footer(self) -> None:
        height, width = self.window.getmaxyx()
        task = self._selected_task()
        selected = f"selected #{task['id']}: {task.get('title') or task.get('request', '')}" if task else "no task selected"
        error = self.state.get("error") or self.ui_error
        self._add(height - 4, 1, selected, max(1, width - 2))
        if error:
            self._add(height - 3, 1, f"! {error}", max(1, width - 2), self._attr("error"))
        elif self.state.get("hold") is not None:
            self._add(height - 3, 1, f"Checkout belongs to T{self.state["hold"]}; other tasks wait for assessment.",
                      max(1, width - 2), self._attr("warn"))
        else:
            self._add(height - 3, 1, "engine healthy", max(1, width - 2), self._attr("good"))
        self._add(height - 2, 1,
                  "↑/↓ jk select  n new  m PM  M models  space reply  Enter details  p pause  r replan",
                  max(1, width - 2))
        self._add(height - 1, 1,
                  "x cancel  s stop  R resume  a approve  +/- priority  b baseline  q quit",
                  max(1, width - 2), self._attr("accent"))

    def _draw_main(self) -> None:
        self._clear()
        height, width = self.window.getmaxyx()
        self._header("Board")
        rows = []
        selected_row = 0
        tasks = self._ordered_tasks()
        for label, group in GROUPS:
            entries = [task for task in tasks if self._group(task) == group]
            if not entries:
                continue
            rows.append((f"{label} ({len(entries)})", self._attr("accent") | curses.A_BOLD))
            for task in entries:
                selected = task["id"] == self.selected
                if selected:
                    selected_row = len(rows)
                marker = "▶" if selected else " "
                status = task.get("question") or task.get("result") or task.get("brief") or "Awaiting PM"
                if task["state"] == "queued":
                    deps = task.get("depends_on", [])
                    status = "After " + ", ".join(f"T{d}" for d in deps) if deps else "Ready"
                if task["state"] == "running":
                    status = f"Mu turn {task.get('turns', 1)}"
                title = _text(task.get("title", "untitled")).replace("\n", " ")
                summary = _text(status).replace("\n", " ")
                title_width = min(38, max(12, width // 3))
                line = f" {marker} T{task['id']:<4} {title[:title_width]:<{title_width}}  {summary}"
                rows.append((line, curses.A_REVERSE if selected else 0))
            rows.append(("", 0))
        visible = max(1, height - 8)
        if selected_row < self.board_scroll:
            self.board_scroll = selected_row
        elif selected_row >= self.board_scroll + visible:
            self.board_scroll = selected_row - visible + 1
        if not rows:
            rows = [("No tasks yet. Press n to submit work, or m to discuss with the PM.", 0)]
        for index, (line, attr) in enumerate(rows[self.board_scroll:self.board_scroll + visible], 3):
            self._add(index, 1, line, max(1, width - 2), attr)
        self._footer()
        self.window.refresh()

    def _show_project(self) -> None:
        tab, scroll = 0, 10**9
        log, last_fetch = "", 0.0
        while not self.engine.done:
            self._tick()
            self._clear()
            height, width = self.window.getmaxyx()
            self._header("Project manager · fresh sessions, shared decisions")
            self._add(3, 2, "[1] Discussion   [2] Decisions   [3] PM execution", width - 4, self._attr("accent"))
            if tab == 0:
                lines = []
                for message in self.state["messages"]:
                    if message["task_id"] is None:
                        lines += [f"{message['role']} · {message['created']}", message["content"], ""]
                if not lines:
                    lines = ["Press space to discuss a design or ask about progress."]
            elif tab == 1:
                lines = [f"{d['id']}. {d['content']}" for d in self.state["decisions"]] or ["No recorded decisions yet."]
            else:
                runs = [r for r in self.state["runs"] if r["kind"] == "pm"]
                if runs and time.monotonic() - last_fetch > 0.8:
                    response = self._request({"op": "log", "run_id": runs[-1]["id"]})
                    log = response.get("text", "") if isinstance(response, dict) else ""
                    last_fetch = time.monotonic()
                lines = [log or "No PM execution yet."]
            wrapped = [part for line in lines for part in _lines(line, max(1, width - 6))]
            visible = max(1, height - 8)
            scroll = max(0, min(scroll, max(0, len(wrapped) - visible)))
            for index, line in enumerate(wrapped[scroll:scroll + visible], 5):
                self._add(index, 3, line, width - 6)
            self._add(height - 3, 2, self.state.get("error") or "", width - 4, self._attr("error"))
            self._add(height - 2, 2, "Space compose · 1/2/3 tabs · a approve PM · ↑/↓ scroll · Esc return", width - 4, self._attr("accent"))
            self.window.refresh()
            key = self._getch()
            if key == 27:
                return
            if key in (" ", "m", "n"):
                self._editor_request("pm")
                scroll = 10**9
            elif key in ("1", "2", "3"):
                tab, scroll = int(key) - 1, 10**9
            elif key in (curses.KEY_UP, "k"):
                scroll -= 1
            elif key in (curses.KEY_DOWN, "j"):
                scroll += 1
            elif key == curses.KEY_PPAGE:
                scroll -= visible
            elif key == curses.KEY_NPAGE:
                scroll += visible
            elif key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = 10**9
            elif key in (3, "q"):
                self._quit_dialog()
            elif key == "a" and self._confirm("Approve PM retry", "After reviewing the PM command, allow ONE Mu retry with ALL Bash traps off?"):
                self._request({"op": "approve_pm"})
            elif key == "M":
                self._show_models()

    def _pick(self, title: str, options: list[str], selected: int = 0) -> int | None:
        scroll = 0
        while not self.engine.done:
            self._tick()
            self._clear()
            height, width = self.window.getmaxyx()
            self._header(title)
            visible = max(1, height - 8)
            scroll = min(scroll, selected)
            if selected >= scroll + visible:
                scroll = selected - visible + 1
            for index in range(scroll, min(len(options), scroll + visible)):
                self._add(4 + index - scroll, 2, options[index], width - 4,
                          curses.A_REVERSE if index == selected else 0)
            self._add(height - 3, 2, "Changes apply to subsequent invocations; running work is unchanged.", width - 4)
            self._add(height - 2, 2, "↑/↓ jk select · Enter choose · Esc cancel", width - 4, self._attr("accent"))
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
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - visible)
            elif key == curses.KEY_NPAGE:
                selected = min(len(options) - 1, selected + visible)
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = len(options) - 1
            elif key == 3:
                self._quit_dialog()
        return None

    def _show_models(self) -> None:
        response = self._request({"op": "models"})
        if not self.last_request_ok:
            return
        role = self._pick("Choose models for…", ["Both PM and workers", "Project manager", "Workers"])
        if role is None:
            return
        roles = (("pm", "worker"), ("pm",), ("worker",))[role]
        current = response["selected"][roles[0]] or ""
        available = response["available"]
        names = [model["id"] for model in available]
        base, _, effort = current.partition(":")
        selected = names.index(base) + 1 if base in names else 0
        choice = self._pick("Model", ["Mu/session default (no override)", *names], selected)
        if choice is None:
            return
        reference = None
        if choice:
            model = available[choice - 1]
            reference = model["id"]
            efforts = model.get("supported_efforts") or []
            if efforts:
                selected = efforts.index(effort) + 1 if base == reference and effort in efforts else 0
                choice = self._pick(f"Effort · {reference}", ["Default effort", *efforts], selected)
                if choice is None:
                    return
                if choice:
                    reference += ":" + efforts[choice - 1]
        self._request({"op": "set_models", "models": {role: reference for role in roles}})

    def _edit(self, title: str, fields: list[tuple[str, str, bool]]) -> list[str] | None:
        values = [value.split("\n") if multiline else [value.replace("\n", " ")]
                  for _, value, multiline in fields]
        cursors = [[len(lines) - 1, len(lines[-1])] for lines in values]
        active = 0
        scroll = 0
        try:
            curses.curs_set(1)
        except curses.error:
            pass

        def insert(char: str) -> None:
            line, col = cursors[active]
            if not fields[active][2] and char in "\r\n":
                return
            if char == "\n" and fields[active][2]:
                tail = values[active][line][col:]
                values[active][line] = values[active][line][:col]
                values[active].insert(line + 1, tail)
                cursors[active] = [line + 1, 0]
            elif char.isprintable():
                values[active][line] = values[active][line][:col] + char + values[active][line][col:]
                cursors[active][1] += len(char)

        while not getattr(self.engine, "done", False):
            self._tick()
            self._draw_editor(title, fields, values, cursors, active, scroll)
            key = self._getch()
            if key is None:
                continue
            if key == 27:
                return None
            if key == 3:
                self._quit_dialog()
                if getattr(self.engine, "done", False):
                    return None
                continue
            if key == 19:
                return ["\n".join(lines).strip() for lines in values]
            if key in (9,):
                active = (active + 1) % len(fields)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if fields[active][2]:
                    insert("\n")
                elif active + 1 < len(fields):
                    active += 1
                continue
            line, col = cursors[active]
            lines = values[active]
            if key in (curses.KEY_UP,):
                cursors[active][0] = max(0, line - 1)
                cursors[active][1] = min(col, len(lines[cursors[active][0]]))
            elif key in (curses.KEY_DOWN,):
                cursors[active][0] = min(len(lines) - 1, line + 1)
                cursors[active][1] = min(col, len(lines[cursors[active][0]]))
            elif key in (curses.KEY_LEFT,):
                if col:
                    cursors[active][1] -= 1
                elif line:
                    cursors[active] = [line - 1, len(lines[line - 1])]
            elif key in (curses.KEY_RIGHT,):
                if col < len(lines[line]):
                    cursors[active][1] += 1
                elif line + 1 < len(lines):
                    cursors[active] = [line + 1, 0]
            elif key in (curses.KEY_HOME,):
                cursors[active][1] = 0
            elif key in (curses.KEY_END,):
                cursors[active][1] = len(lines[line])
            elif key in (curses.KEY_BACKSPACE, 8, 127):
                if col:
                    lines[line] = lines[line][:col - 1] + lines[line][col:]
                    cursors[active][1] -= 1
                elif line:
                    previous = len(lines[line - 1])
                    lines[line - 1] += lines.pop(line)
                    cursors[active] = [line - 1, previous]
            elif key == curses.KEY_DC:
                if col < len(lines[line]):
                    lines[line] = lines[line][:col] + lines[line][col + 1:]
                elif line + 1 < len(lines):
                    lines[line] += lines.pop(line + 1)
            elif isinstance(key, str):
                for char in key:
                    insert("\n" if char == "\n" else char)
            scroll = max(0, cursors[active][0] - max(1, self.window.getmaxyx()[0] - 9))
        return None

    def _draw_editor(self, title: str, fields: list[tuple[str, str, bool]],
                     values: list[list[str]], cursors: list[list[int]], active: int,
                     scroll: int) -> None:
        self._clear()
        height, width = self.window.getmaxyx()
        self._header(title)
        self._add(3, 2, "Ctrl-S submit · Esc cancel · Tab next field", max(1, width - 4), self._attr("accent"))
        y = 5
        for index, (label, _, multiline) in enumerate(fields):
            self._add(y, 2, label + (" (multiline)" if multiline else ""), max(1, width - 4),
                      self._attr("warn") if index == active else self._attr("accent"))
            y += 1
            if multiline:
                visible = max(1, height - y - 3)
                start = max(0, min(scroll, len(values[index]) - visible))
                horizontal = max(0, cursors[index][1] - max(1, width - 7)) if index == active else 0
                for line_index in range(start, min(len(values[index]), start + visible)):
                    self._add(y, 3, values[index][line_index][horizontal:], max(1, width - 6))
                    y += 1
                if index == active:
                    line, col = cursors[index]
                    rendered = min(len(values[index]), start + visible) - start
                    cursor_y = y - rendered + line - start if line >= start else y
                    cursor_y = max(0, min(height - 2, cursor_y))
                    try:
                        self.window.move(cursor_y, min(width - 1, 3 + col - horizontal))
                    except curses.error:
                        pass
            else:
                horizontal = max(0, cursors[index][1] - max(1, width - 7)) if index == active else 0
                self._add(y, 3, values[index][0][horizontal:], max(1, width - 6))
                if index == active:
                    try:
                        self.window.move(min(height - 2, y), min(width - 1, 3 + cursors[index][1] - horizontal))
                    except curses.error:
                        pass
                y += 1
            y += 1
        if self.ui_error:
            self._add(height - 2, 2, self.ui_error, max(1, width - 4), self._attr("error"))
        self.window.refresh()

    def _editor_request(self, kind: str) -> None:
        task = self._selected_task()
        task_id = task.get("id") if task else None
        if kind == "new":
            result = self._edit("New task", [("Title", "", False), ("Request", "", True)])
            if result is not None and result[1]:
                self._request({"op": "add", "text": result[1], "title": result[0] or None})
        else:
            label = "Discuss with PM" if kind == "pm" else "Reply"
            result = self._edit(label, [("Message", "", True)])
            if result is not None and result[0]:
                self._request({"op": "reply", "task_id": None if kind == "pm" else task_id, "text": result[0]})
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def _confirm(self, heading: str, message: str) -> bool:
        while not getattr(self.engine, "done", False):
            self._tick()
            self._clear()
            height, width = self.window.getmaxyx()
            self._header("Confirmation")
            box_top, box_bottom = 4, max(5, height - 5)
            self._add(box_top, 2, heading, max(1, width - 4), self._attr("warn"))
            y = box_top + 2
            for line in _lines(message, max(1, width - 6)):
                if y >= box_bottom:
                    break
                self._add(y, 3, line, max(1, width - 6))
                y += 1
            self._add(box_bottom - 1, 3, "y confirm · n/Esc cancel", max(1, width - 6), self._attr("accent"))
            if self.ui_error:
                self._add(height - 2, 2, self.ui_error, max(1, width - 4), self._attr("error"))
            self.window.refresh()
            key = self._getch()
            if key == 3:
                self._quit_dialog()
                if getattr(self.engine, "done", False):
                    return False
                continue
            if key in (27, "n", "N"):
                return False
            if key in ("y", "Y", 10, 13):
                return True
        return False

    def _action(self, key: str) -> None:
        task = self._selected_task()
        task_id = task.get("id") if task else None
        if key == "p":
            self._request({"op": "pause", "value": not bool(self.state.get("paused"))})
        elif key == "r":
            self._request({"op": "replan"})
        elif key == "b":
            if self._confirm(
                    "Accept checkout baseline", "After inspecting existing changes, accept them as a safe baseline and release checkout ownership? This does not undo files."):
                self._request({"op": "ack_workspace"})
        elif task_id is None:
            self.ui_error = "Select a task first."
        elif key == "x":
            if self._confirm("Cancel task", "Cancel this task and stop dispatching it?"):
                self._request({"op": "cancel", "task_id": task_id})
        elif key == "s":
            if self._confirm("Stop worker", "Interrupt the worker currently running this task?"):
                self._request({"op": "stop", "task_id": task_id})
        elif key == "R":
            if self._confirm("Resume task", "Retry this stopped or failed task using Mu retry?"):
                self._request({"op": "resume", "task_id": task_id})
        elif key == "a":
            if str(task.get("gate", "")).lower() != "approval":
                self.ui_error = "Approval is not currently requested for this task."
            elif self._confirm(
                    "Approve one retry with trap disabled",
                    "This gives Mu broad permission to retry with --trap off for ONE invocation. "
                    "Only approve if you have reviewed the requested action."):
                self._request({"op": "approve", "task_id": task_id})
        elif key in {"+", "-"}:
            priority = task.get("priority", 0)
            if not isinstance(priority, int):
                priority = 0
            self._request({"op": "priority", "task_id": task_id,
                           "priority": priority + (1 if key == "+" else -1)})

    def _show_details(self, task_id: int) -> None:
        detail = self._request({"op": "show", "task_id": task_id})
        if not isinstance(detail, dict):
            return
        tab = 0
        scroll = 0
        run_index = max(0, len(detail.get("runs", [])) - 1)
        log_cache: dict[str, str] = {}
        last_fetch = time.monotonic()
        while not getattr(self.engine, "done", False):
            self._tick()
            if time.monotonic() - last_fetch > 0.8:
                fresh = self._request({"op": "show", "task_id": task_id})
                if isinstance(fresh, dict):
                    detail = fresh
                log_cache.clear()
                last_fetch = time.monotonic()
            task = detail.get("task") if isinstance(detail.get("task"), dict) else self._selected_task() or {}
            messages = detail.get("messages") if isinstance(detail.get("messages"), list) else []
            runs = detail.get("runs") if isinstance(detail.get("runs"), list) else []
            if runs:
                run_index = max(0, min(run_index, len(runs) - 1))
            if tab == 2 and runs:
                run = runs[run_index]
                run_id = run.get("id") if isinstance(run, dict) else None
                if isinstance(run_id, str) and run_id not in log_cache:
                    response = self._request({"op": "log", "run_id": run_id})
                    log_cache[run_id] = _text(response.get("text", "") if isinstance(response, dict) else "")
            content = self._detail_lines(tab, task, messages, runs, run_index, log_cache)
            scroll = self._draw_details(task, tab, content, scroll, len(runs))
            key = self._getch()
            if key in (27,):
                return
            if key in ("q", "Q", 3):
                self._quit_dialog()
                if getattr(self.engine, "done", False):
                    return
            elif key == " ":
                self._editor_request("reply")
            elif key in ("1", "2", "3"):
                tab = int(key) - 1
                scroll = 0
            elif key in (9, curses.KEY_RIGHT):
                tab = (tab + 1) % 3
                scroll = 0
            elif key == curses.KEY_LEFT:
                tab = (tab - 1) % 3
                scroll = 0
            elif key in (curses.KEY_UP, "k"):
                scroll = max(0, scroll - 1)
            elif key in (curses.KEY_DOWN, "j"):
                scroll += 1
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - max(1, self.window.getmaxyx()[0] // 2))
            elif key == curses.KEY_NPAGE:
                scroll += max(1, self.window.getmaxyx()[0] // 2)
            elif key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = 10**9
            elif key == "[" and runs:
                run_index = max(0, run_index - 1)
                scroll = 0
            elif key == "]" and runs:
                run_index = min(len(runs) - 1, run_index + 1)
                scroll = 0

    def _detail_lines(self, tab: int, task: dict[str, Any], messages: list[Any],
                      runs: list[Any], run_index: int, logs: dict[str, str]) -> list[str]:
        if tab == 0:
            dependencies = ", ".join(str(item) for item in task.get("depends_on", [])) or "none"
            lines = [
                f"#{task.get('id', '?')}  {task.get('title') or 'untitled'}",
                f"state: {task.get('state', '?')}   priority: {task.get('priority', '?')}   turns: {task.get('turns', 0)}",
                f"depends on: {dependencies}   gate: {task.get('gate') or 'none'}",
                f"session: {task.get('session') or 'none'}   updated: {task.get('updated') or 'unknown'}",
                "", "Request", _text(task.get("request")), "", "Brief", _text(task.get("brief")),
            ]
            if task.get("question"):
                lines += ["", "Question", _text(task["question"])]
            if task.get("result"):
                lines += ["", "Result", _text(task["result"])]
            return lines
        if tab == 1:
            output = ["Discussion"]
            for message in messages:
                if not isinstance(message, dict):
                    continue
                stamp = message.get("created", "")
                role = message.get("role", "")
                output += [f"[{stamp}] {role}", _text(message.get("content")), ""]
            return output or ["No discussion yet."]
        output = ["Execution", "[ ] previous/next run", ""]
        if not runs:
            return output + ["No runs yet."]
        for index, run in enumerate(runs):
            if not isinstance(run, dict):
                continue
            marker = "▶" if index == run_index else " "
            output.append(f"{marker} run {run.get('id', '?')} · {run.get('kind', '')} · "
                          f"{run.get('status', '')} · exit {run.get('exit_code', '')}")
        run = runs[run_index] if isinstance(runs[run_index], dict) else {}
        output += ["", f"Log for run {run.get('id', '?')}", logs.get(run.get("id"), "(loading or no output)")]
        return output

    def _draw_details(self, task: dict[str, Any], tab: int, content: list[str],
                      scroll: int, run_count: int) -> int:
        self._clear()
        height, width = self.window.getmaxyx()
        self._header(f"Task #{task.get('id', '?')} details")
        tabs = "  ".join(f"[{i + 1}] {name}" for i, name in enumerate(("Brief", "Discussion", "Execution")))
        self._add(3, 2, tabs, max(1, width - 4), self._attr("accent"))
        self._add(3, max(1, width - 19), f"runs: {run_count}", 16)
        view_height = max(1, height - 7)
        wrapped: list[str] = []
        for line in content:
            wrapped.extend(_lines(line, max(1, width - 6)))
        scroll = max(0, min(scroll, max(0, len(wrapped) - view_height)))
        for index, line in enumerate(wrapped[scroll:scroll + view_height], 5):
            self._add(index, 3, line, max(1, width - 6))
        self._add(height - 2, 2, "Esc close · Tab/1-3 tabs · ↑/↓ scroll · [] choose run · q quit",
                  max(1, width - 4), self._attr("accent"))
        self.window.refresh()
        return scroll

    def _quit_dialog(self) -> None:
        while not getattr(self.engine, "done", False):
            self._tick()
            self._clear()
            height, width = self.window.getmaxyx()
            self._header("Quit Mu Board")
            self._add(4, 3, "k / Esc  keep running (cancel)", max(1, width - 6))
            self._add(5, 3, "f        finish the current task, then exit", max(1, width - 6))
            self._add(6, 3, "s        stop now and exit", max(1, width - 6))
            self._add(8, 3, "Your choice:", max(1, width - 6), self._attr("warn"))
            if self.ui_error:
                self._add(height - 2, 2, self.ui_error, max(1, width - 4), self._attr("error"))
            self.window.refresh()
            try:
                key = self._getch()
            except KeyboardInterrupt:
                continue
            if key in (27, "k", "K", "n", "N", "q", "Q"):
                return
            if key in ("f", "F", "s", "S"):
                mode = "finish" if str(key).lower() == "f" else "stop"
                if self._confirm("Confirm shutdown", f"Request shutdown mode '{mode}'?"):
                    self._request({"op": "shutdown", "mode": mode})
                    if self.last_request_ok:
                        self._wait_for_shutdown(mode)
                    return

    def _wait_for_shutdown(self, mode: str) -> None:
        while not getattr(self.engine, "done", False):
            self._tick()
            self._clear()
            height, width = self.window.getmaxyx()
            self._header("Shutting down")
            self._add(4, 3, f"Shutdown requested ({mode}). Waiting for the engine…",
                      max(1, width - 6), self._attr("warn"))
            self._add(6, 3, "s / Ctrl-C: stop now instead of waiting", max(1, width - 6))
            self.window.refresh()
            if self._getch() in ("s", 3):
                self._request({"op": "shutdown", "mode": "stop"})
                mode = "stop"

    def _main(self) -> None:
        self._tick(force=True)
        while not getattr(self.engine, "done", False):
            try:
                self._tick()
                self._draw_main()
                key = self._getch()
                if key is None:
                    continue
                if key in (3, "q", "Q"):
                    self._quit_dialog()
                elif key in (curses.KEY_UP, "k"):
                    self._move(-1)
                elif key in (curses.KEY_DOWN, "j"):
                    self._move(1)
                elif key in (curses.KEY_LEFT,):
                    self._move(-1)
                elif key in (curses.KEY_RIGHT,):
                    self._move(1)
                elif key == "n":
                    self._editor_request("new")
                elif key == "m":
                    self._show_project()
                elif key == "M":
                    self._show_models()
                elif key == " ":
                    self._editor_request("reply")
                elif key in (10, 13, curses.KEY_ENTER):
                    if self.selected is not None:
                        self._show_details(self.selected)
                    else:
                        self.ui_error = "Select a task first."
                elif key in {"p", "P", "r", "x", "s", "R", "a", "b", "+", "-"}:
                    self._action("p" if key == "P" else key)
            except KeyboardInterrupt:
                self._quit_dialog()


def _curses_main(window: Any, engine: Any) -> None:
    app = _UI(window, engine)
    app._main()


def run_ui(engine: Any) -> None:
    """Run Mu Board until ``engine.done`` becomes true.

    A board is intentionally rejected when its parent is not attached to a
    terminal: curses cannot provide a useful or safe fallback in that case.
    """
    if not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty() \
            or not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        raise RuntimeError("Mu Board UI requires an interactive terminal")
    try:
        curses.wrapper(_curses_main, engine)
    except KeyboardInterrupt:
        # Ctrl-C normally arrives as a key in cbreak mode.  If the terminal
        # instead delivers SIGINT, curses.wrapper still restores the tty.
        raise
