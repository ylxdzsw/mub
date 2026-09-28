"""Read-only terminal screens. Parsing and emulation belong to the Rust core."""

import errno
import os
import pty
import select
import subprocess
import tempfile
import termios
import threading

from par_term_emu_core_rust import Terminal, rgb_to_ansi_256


_ANSI_COLORS = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
                (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
                (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
                (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]


class Screen:
    def __init__(self, cols=80, rows=24):
        self.cols, self.rows = cols, rows
        self.lock = threading.RLock()
        self.core = Terminal(cols, rows, scrollback=10000)
        self.core.set_allow_clipboard_read(False)
        self.core.set_max_osc_data_length(65536)
        self.core.set_max_clipboard_sync_events(0)
        self.core.set_max_clipboard_sync_history(0)
        self.core.set_max_inline_images(0)
        self.core.set_sixel_limits(64, 64, 64)
        self.core.set_sixel_graphics_limit(1)
        self.core.set_max_transfer_size(0)
        self.core.set_bold_brightening(False)
        # get_line_cells resolves indexed colors to standard ANSI RGB, not the
        # engine's screenshot theme palette. Map these back to host palette slots.
        self.palette = {rgb: i for i, rgb in enumerate(_ANSI_COLORS)}
        self.error = None
        self.finished = False
        self.session = None

    def feed(self, data):
        with self.lock:
            self.core.process(data)
            self.core.poll_events()
            # No terminal-generated input, clipboard actions, graphics, or host
            # notifications escape this read-only view. OSC 8 links remain data.
            self.core.drain_bell_events()
            self.core.drain_responses()
            self.core.drain_notifications()
            self.core.clear_notification_events()
            self.core.clear_graphics()

    def links(self):
        with self.lock:
            return list(dict.fromkeys(link[0] for link in self.core.get_all_hyperlinks()))

    def frame(self, start, count, *, follow=False):
        with self.lock:
            core = self.core
            history = 0 if core.is_alt_screen_active() else core.scrollback_len()
            used = self.rows if core.is_alt_screen_active() else max(
                core.cursor_position()[1] + 1,
                max((row + 1 for row in range(self.rows) if core.get_line(row).strip()), default=0))
            total = history + used
            start = max(0, total - count) if follow else max(0, min(start, max(0, total - count)))
            lines = [core.scrollback_line(row) if row < history else core.get_line_cells(row - history)
                     for row in range(start, min(total, start + count))]
            return start, total, lines

    def color(self, rgb, *, background=False):
        rgb = tuple(rgb)
        if rgb == ((0, 0, 0) if background else (192, 192, 192)):
            return -1
        return self.palette[rgb] if rgb in self.palette else rgb_to_ansi_256(rgb)


class Capture:
    """Drain a PTY even while the owner is busy or its UI is not viewing it."""
    def __init__(self, screen):
        self.screen = screen
        self.master, self.slave = pty.openpty()
        try:
            termios.tcsetwinsize(self.slave, (screen.rows, screen.cols))
            self.raw = tempfile.TemporaryFile(buffering=0)
        except OSError:
            os.close(self.master)
            os.close(self.slave)
            raise
        self.thread = None
        self.error = None
        self.ending = threading.Event()

    def start(self):
        os.close(self.slave)
        self.slave = None
        self.thread = threading.Thread(target=self._drain, name="mub-pty")
        self.thread.start()

    def _drain(self):
        try:
            while True:
                if not select.select([self.master], [], [], 0.05)[0]:
                    # Only the owner, after reaping the invocation and its known
                    # descendants, ends collection. Drain queued bytes first;
                    # don't wait forever for an unrelated holder of a slave fd.
                    if self.ending.is_set():
                        break
                    continue
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                remaining = memoryview(chunk)
                while remaining:
                    remaining = remaining[self.raw.write(remaining):]
                if not self.screen.error:
                    try:
                        self.screen.feed(chunk)
                    except Exception as error:
                        # A display failure must not lose evidence or fill the PTY.
                        self.screen.error = f"Terminal display: {error}"
        except OSError as error:
            self.error = str(error)
        finally:
            os.close(self.master)

    def finish(self):
        if self.thread:
            self.ending.set()
            self.thread.join()
        else:
            os.close(self.master)
            if self.slave is not None:
                os.close(self.slave)
                self.slave = None


def replay_screen(root, session, cols, rows, mu="mu", stop=None):
    """Replay before dispatch, or in a UI background job while the session is idle."""
    screen = Screen(cols, rows)
    screen.session = session
    capture = Capture(screen)
    process = None
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([mu, "transcript", "-s", session, "-o", "concise"], cwd=root,
                                       stdin=subprocess.DEVNULL, stdout=capture.slave, stderr=errors,
                                       env=terminal_env())
            capture.start()
            if stop is not None:
                while process.poll() is None:
                    if stop.wait(0.05):
                        raise RuntimeError("History replay cancelled")
            code = process.wait()
            capture.finish()
            if code:
                errors.seek(0)
                raise RuntimeError(errors.read().decode("utf-8", "replace").strip() or "Mu replay failed")
            if capture.error or screen.error:
                raise RuntimeError(capture.error or screen.error)
        screen.finished = True
        return screen
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if capture.thread is None:
            capture.finish()
        elif capture.thread.is_alive():
            capture.finish()
        capture.raw.close()


def terminal_env():
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
    env.pop("NO_COLOR", None)
    return env
