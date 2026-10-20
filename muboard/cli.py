"""TUI owner and small session-oriented control commands."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

from .ipc import call
from .output import conversation, journal_events
from .state import project_root, read_state


def session_id(value):
    return int(value.removeprefix("S").removeprefix("s"))


def run_options(parser):
    parser.add_argument("--mu", default="mu", help="Mu executable")
    parser.add_argument("--scheduler-model")
    parser.add_argument("--worker-model")
    parser.add_argument("--model", help="Override both models for this launch")
    for flag in ("headless", "until-idle"):
        parser.add_argument("--" + flag, action="store_true")


def parser():
    p = argparse.ArgumentParser(prog="mub", description="Run mub without a subcommand to open the session TUI.",
                                epilog="Mutation commands are for the active scheduler. User controls live in the TUI.")
    p.add_argument("-C", "--project", help="Project directory (default: current directory; plain mub initializes Git if needed)")
    run_options(p)
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Run the selected session's next eligible operation")
    run.add_argument("session_id", type=session_id)
    run.add_argument("--trap", choices=("reversible", "destructive", "off"), required=True)
    rename = sub.add_parser("rename", help="Rename an automatically named session")
    rename.add_argument("session_id", type=session_id)
    rename.add_argument("name")
    failed = sub.add_parser("mark-failed", help="Require user intervention for a session")
    failed.add_argument("session_id", type=session_id)
    failed.add_argument("reason")
    note = sub.add_parser("note", help="Set or clear a session's scheduler note")
    note.add_argument("session_id", type=session_id)
    note.add_argument("text", nargs="?")
    note.add_argument("--clear", action="store_true")
    context = sub.add_parser("context", help="Read exact user turns and final responses from a worker journal")
    context.add_argument("session_id", type=session_id)
    context.add_argument("--before", type=int, help="Read only this byte prefix of the journal")
    context.add_argument("--turn", help="Read one Mu turn ID instead of the full conversation")
    context.add_argument("--requests", action="store_true", help="Omit responses when inspecting user scope and restrictions")
    return p


def main():
    args = parser().parse_args()
    try:
        directory = Path(args.project or Path.cwd()).resolve()
        if args.command is None:
            run(directory, args)
            return
        command = args.command
        root = project_root(directory)
        if command == "context":
            session = next((s for s in read_state(root)["sessions"] if s["id"] == args.session_id), None)
            if not session:
                raise ValueError("Unknown session")
            result = conversation(journal_events(root, session["session"], before=args.before)) if session["session"] else dict(omitted_turns=0, turns=[])
            if args.turn:
                result["turns"] = [t for t in result["turns"] if t["turn_id"] == args.turn]
                if not result["turns"]:
                    raise ValueError("Unknown turn in this journal prefix")
            if args.requests:
                for turn in result["turns"]:
                    del turn["response"]
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if command == "note" and args.clear == (args.text is not None):
            raise ValueError("Supply note text or --clear, not both")
        request = dict(op="scheduler_action", action=command, session_id=args.session_id,
                       pass_id=os.environ.get("MUB_PASS_ID"))
        if command == "run":
            request["trap"] = args.trap
        elif command == "rename":
            request["name"] = args.name
        elif command == "mark-failed":
            request["reason"] = args.reason
        else:
            request["note"] = None if args.clear else args.text
        result = call(root, request)
        print(result["text"])
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
    root = project_root(root, initialize=True)
    engine = Engine(root, mu=mu, scheduler_model=args.scheduler_model or args.model,
                    worker_model=args.worker_model or args.model)
    previous = {}
    try:
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
                                               active=s["active"], reason=s["reason"], blocked=s["blocked"],
                                               steering=s.get("steering"), scheduling=s.get("scheduling"))
                                         for s in state["sessions"]],
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
