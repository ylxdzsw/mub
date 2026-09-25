"""TUI owner and small session-oriented control commands."""

import argparse
import json
from pathlib import Path
import shutil
import signal
import sys
import time

from .ipc import ControlServer, call
from .output import replay
from .state import project_root, read_state


def session_id(value):
    return int(value.removeprefix("S").removeprefix("s"))


def text_arg(value):
    if value is not None:
        return value
    if sys.stdin.isatty():
        raise ValueError("Supply a message as an argument or on stdin")
    return sys.stdin.read()


def run_options(parser, suppressed=False):
    default = argparse.SUPPRESS if suppressed else None
    parser.add_argument("--mu", default=argparse.SUPPRESS if suppressed else "mu", help="Mu executable")
    parser.add_argument("--scheduler-model", default=default)
    parser.add_argument("--worker-model", default=default)
    parser.add_argument("--model", default=default, help="Override both models for this launch")
    for flag in ("headless", "until-idle"):
        parser.add_argument("--" + flag, action="store_true", default=argparse.SUPPRESS if suppressed else False)


def parser():
    p = argparse.ArgumentParser(prog="mub", description="Mu sessions with scheduled message delivery")
    p.add_argument("-C", "--project", help="Directory in the Git worktree (default: current directory)")
    run_options(p)
    sub = p.add_subparsers(dest="command")
    run_options(sub.add_parser("run", help="Open the session TUI (default)"), True)
    new = sub.add_parser("new", help="Create a session, optionally queue its first message")
    new.add_argument("text", nargs="?")
    new.add_argument("--name")
    send = sub.add_parser("send", help="Queue a message for a session")
    send.add_argument("session_id", type=session_id)
    send.add_argument("text", nargs="?")
    for name, help_text in (("interrupt", "Stop a session and hold all automatic work"),
                            ("resume", "Release a hold; explicitly authorize continuation of the old turn"),
                            ("remove", "Detach an idle session; never delete its Mu journal"),
                            ("logs", "Read Mu history and current live output")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("session_id", type=session_id)
        if name == "remove":
            command.add_argument("--discard", action="store_true", help="Discard queued/interrupted messages")
    sub.add_parser("status", help="Read board state, including when the owner is closed")
    sub.add_parser("schedule", help="Recheck scheduling; replace a failed scheduler session, never retry it")
    models = sub.add_parser("models", help="List models or save scheduler/worker choices")
    models.add_argument("--scheduler")
    models.add_argument("--worker")
    quit_parser = sub.add_parser("quit", help="Stop all agents and close mub")
    quit_parser.add_argument("--yes", action="store_true", help="Confirm stopping running agents")
    return p


def main():
    args = parser().parse_args()
    try:
        root = project_root(Path(args.project or Path.cwd()).resolve())
        command = args.command or "run"
        if command == "run":
            run(root, args)
            return
        if command == "status":
            try:
                result = call(root, dict(op="status"))
            except RuntimeError:
                result = dict(read_state(root), root=str(root), owner_running=False)
        elif command == "logs":
            try:
                result = call(root, dict(op="output", session_id=args.session_id))
            except RuntimeError:
                session = next((s for s in read_state(root)["sessions"] if s["id"] == args.session_id), None)
                if not session:
                    raise ValueError("Unknown session")
                result = dict(text=replay(root, session["session"], args.mu) if session["session"] else "",
                              source="Mu session journal")
            print(f"[{result['source']}]", file=sys.stderr)
            print(result["text"], end="")
            return
        else:
            request = dict(op=command)
            if hasattr(args, "session_id"):
                request["session_id"] = args.session_id
            if command == "new":
                request["name"] = args.name
                if args.text is not None or not sys.stdin.isatty():
                    text = text_arg(args.text)
                    if text.strip():
                        request["text"] = text
            elif command == "send":
                request["text"] = text_arg(args.text)
            elif command == "remove":
                request["discard"] = args.discard
            elif command == "models":
                choices = {role: None if value == "default" else value
                           for role in ("scheduler", "worker") if (value := getattr(args, role)) is not None}
                if choices:
                    request.update(op="set_models", models=choices)
            elif command == "quit":
                state = call(root, dict(op="status"))
                busy = state["scheduler"]["active"] or any(s["active"] for s in state["sessions"])
                confirmed = args.yes
                if busy and not args.yes:
                    if not sys.stdin.isatty() or input("Stop running agents and quit? [y/N] ").lower() != "y":
                        raise ValueError("Quit cancelled; use --yes to confirm stopping running agents")
                    confirmed = True
                request.update(op="shutdown", confirmed=confirmed)
            result = call(root, request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError) as error:
        print(f"mub: {error}", file=sys.stderr)
        raise SystemExit(1) from None


def run(root, args):
    if not args.headless and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ValueError("The TUI needs a terminal; use --headless")
    if args.until_idle and not args.headless:
        raise ValueError("--until-idle requires --headless")
    mu = shutil.which(args.mu)
    if not mu:
        raise ValueError(f"Mu executable not found: {args.mu}")
    from .engine import Engine
    engine = Engine(root, mu=mu, scheduler_model=args.scheduler_model or args.model,
                    worker_model=args.worker_model or args.model)
    previous = {}
    try:
        engine.server = ControlServer(root)

        def shutdown(signum, frame):
            engine.request(dict(op="shutdown", confirmed=True))

        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, shutdown)
        if args.headless:
            previous[signal.SIGINT] = signal.signal(signal.SIGINT, shutdown)
            last = None
            while not engine.done:
                engine.tick()
                state = engine.state()
                summary = dict(sessions=[dict(id=s["id"], name=s["name"], hold=s["hold"], gate=s["gate"],
                                               active=s["active"], blocked=s["blocked"]) for s in state["sessions"]],
                               queued=len(state["messages"]), scheduler=state["scheduler"], workspace=state["workspace"])
                encoded = json.dumps(summary, ensure_ascii=False)
                if encoded != last:
                    print(encoded, flush=True)
                    last = encoded
                if args.until_idle and engine.idle():
                    break
                time.sleep(0.05)
        else:
            from .ui import run_ui
            run_ui(engine)
    finally:
        engine.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
