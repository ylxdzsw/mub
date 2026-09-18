import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .ipc import ControlServer, call
from .state import read_state


def text_arg(value):
    if value is not None:
        return value
    if sys.stdin.isatty():
        raise ValueError("Supply text as an argument or on stdin")
    return sys.stdin.read()


def task_id(value):
    return int(value.removeprefix("T").removeprefix("t"))


def run_options(parser, *, suppressed=False):
    defaults = argparse.SUPPRESS if suppressed else None
    parser.add_argument("--mu", default=argparse.SUPPRESS if suppressed else "mu", help="Mu executable")
    parser.add_argument("--model", default=defaults, help="Override both models for this launch (otherwise use saved choices or Mu defaults)")
    parser.add_argument("--pm-model", default=defaults, help="Override the PM model for this launch")
    parser.add_argument("--worker-model", default=defaults, help="Override the worker model for this launch")
    parser.add_argument("--max-turns", type=int, default=argparse.SUPPRESS if suppressed else 8)
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS if suppressed else 1800,
                        help="Seconds per Mu invocation (default 1800)")
    for flag, help_text in [("paused", "Start with worker dispatch paused"),
                            ("headless", "Run the same owner without curses"),
                            ("until-idle", "Exit the headless owner when no work is actionable")]:
        parser.add_argument("--" + flag, action="store_true", default=argparse.SUPPRESS if suppressed else False, help=help_text)


def parser():
    p = argparse.ArgumentParser(prog="mub", description="Mu Board — a project inbox, fresh PM turns, and one editing worker.")
    p.add_argument("-C", "--project", help="Project directory (default: current working directory)")
    run_options(p)
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Own the board and open its TUI (default)")
    run_options(run, suppressed=True)
    add = sub.add_parser("add", help="Submit a task to the running board")
    add.add_argument("text", nargs="?")
    add.add_argument("--title")
    discuss = sub.add_parser("discuss", help="Send a project-level message to a fresh PM")
    discuss.add_argument("text", nargs="?")
    reply = sub.add_parser("reply", help="Reply to a task; PM arranges its continuation")
    reply.add_argument("task_id", type=task_id)
    reply.add_argument("text", nargs="?")
    sub.add_parser("status", help="Read board state as JSON, including when the TUI is closed")
    show = sub.add_parser("show", help="Read a task's brief, discussion, and run history")
    show.add_argument("task_id", type=task_id)
    logs = sub.add_parser("logs", help="Read captured Mu output")
    logs.add_argument("run_id")
    for name in ("cancel", "stop", "resume", "approve"):
        command = sub.add_parser(name)
        command.add_argument("task_id", type=task_id)
        if name == "approve":
            command.add_argument("--yes", action="store_true", help="Allow one Mu retry with ALL Bash traps off")
    for name in ("pause", "unpause", "replan"):
        sub.add_parser(name)
    pm_approval = sub.add_parser("approve-pm", help="Allow one trapped PM turn to retry with all Bash traps off")
    pm_approval.add_argument("--yes", action="store_true")
    priority = sub.add_parser("priority")
    priority.add_argument("task_id", type=task_id)
    priority.add_argument("priority", type=int)
    baseline = sub.add_parser("accept-baseline", help="Accept existing checkout changes and release workspace ownership")
    baseline.add_argument("--yes", action="store_true")
    shutdown = sub.add_parser("quit")
    shutdown.add_argument("--finish", action="store_true", help="Finish the current task instead of interrupting")
    sub.add_parser("plan", help="PM-only: stage a JSON plan from stdin")
    return p


def main():
    args = parser().parse_args()
    try:
        root = Path(args.project or Path.cwd()).resolve()
        command = args.command or "run"
        if command == "run":
            run(root, args)
            return
        if command in ("status", "show", "logs"):
            state = read_state(root)
            if command == "status":
                try:
                    result = call(root, dict(op="status"))
                except RuntimeError:
                    result = dict(state, root=str(root), owner_running=False)
            elif command == "show":
                task = next((t for t in state["tasks"] if t["id"] == args.task_id), None)
                if not task:
                    raise ValueError("Unknown task")
                result = dict(task=task, messages=[m for m in state["messages"] if m["task_id"] == args.task_id],
                              runs=[r for r in state["runs"] if r["task_id"] == args.task_id])
            else:
                record = next((r for r in state["runs"] if r["id"] == args.run_id), None)
                if not record:
                    raise ValueError("Unknown run")
                print(Path(record["log_path"]).read_text(), end="")
                return
        else:
            req = dict(op=command)
            if command in ("add", "reply", "discuss"):
                req["text"] = text_arg(args.text)
            if hasattr(args, "task_id"):
                req["task_id"] = args.task_id
            if command == "add":
                req["title"] = args.title
            elif command == "discuss":
                req.update(op="reply", task_id=None)
            elif command == "priority":
                req["priority"] = args.priority
            elif command in ("pause", "unpause"):
                req.update(op="pause", value=command == "pause")
            elif command == "approve" and not args.yes:
                raise ValueError("Inspect the trapped command, then use --yes to allow ONE Mu retry with all Bash traps off")
            elif command == "approve-pm":
                if not args.yes:
                    raise ValueError("Inspect the PM execution log, then use --yes to allow ONE Mu retry with all Bash traps off")
                req["op"] = "approve_pm"
            elif command == "accept-baseline":
                if not args.yes:
                    raise ValueError("Inspect existing changes, then pass --yes to accept them as the next task's baseline")
                req["op"] = "ack_workspace"
            elif command == "quit":
                req.update(op="shutdown", mode="finish" if args.finish else "stop")
            elif command == "plan":
                req.update(plan=json.loads(text_arg(None)), token=os.environ.get("MUB_PM_TOKEN"))
            result = call(root, req)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        print(f"mub: {error}", file=sys.stderr)
        raise SystemExit(1) from None


def run(root, args):
    if not args.headless and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ValueError("The TUI needs a terminal; use --headless for scripting")
    if args.until_idle and not args.headless:
        raise ValueError("--until-idle requires --headless")
    if args.max_turns < 1 or args.timeout <= 0:
        raise ValueError("Turn limit and timeout must be positive")
    mu = shutil.which(args.mu)
    if not mu:
        raise ValueError(f"Mu executable not found: {args.mu}")
    scope = subprocess.run([mu, "status", "--json"], cwd=root, capture_output=True, text=True, timeout=20)
    if scope.returncode:
        raise RuntimeError(scope.stdout.strip() or scope.stderr.strip())
    if json.loads(scope.stdout).get("project_root") is None:
        initialized = subprocess.run([mu, "init", "--path", str(root)], capture_output=True, text=True, timeout=20)
        if initialized.returncode:
            raise RuntimeError(initialized.stdout.strip() or initialized.stderr.strip())
    from .engine import Engine
    engine = Engine(root, mu=mu, pm_model=args.pm_model or args.model,
                    worker_model=args.worker_model or args.model, max_turns=args.max_turns,
                    timeout=args.timeout, paused=args.paused)
    previous = {}
    try:
        engine.server = ControlServer(root)

        def shutdown(signum, frame):
            if not engine.stopping:
                engine.request(dict(op="shutdown", mode="stop"))

        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, shutdown)
        if args.headless:
            previous[signal.SIGINT] = signal.signal(signal.SIGINT, shutdown)
            last = None
            while not engine.done:
                engine.tick()
                state = engine.state()
                summary = dict(tasks=[dict(id=t["id"], title=t["title"], state=t["state"], question=t["question"]) for t in state["tasks"]],
                               pm=state["pm"]["id"] if state["pm"] else None,
                               worker=state["worker"]["id"] if state["worker"] else None,
                               error=state["error"], paused=state["paused"])
                if summary != last:
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
                    last = summary
                if args.until_idle and engine.idle():
                    break
                time.sleep(0.1)
        else:
            from .ui import run_ui
            run_ui(engine)
    finally:
        engine.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
