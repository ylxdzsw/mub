# Development

[Home](../README.md) · [Usage](usage.md) · [Design](design.md)

## Code map

- `muboard/cli.py` and `__main__.py`: command parsing and owner startup.
- `muboard/engine.py`: scheduling, mailboxes, subprocess supervision, and recovery.
- `muboard/state.py`: worktree discovery, owner lock, and durable state.
- `muboard/ipc.py`: local control socket for the running owner.
- `muboard/ui.py`: curses interface, composer, and output navigation.
- `muboard/terminal.py` and `output.py`: PTY capture, terminal emulation,
  journal delivery boundaries, and transcript replay.

See [design and scheduling](design.md) for the runtime invariants.

## Build and install

Run these from the repository root. Real runs require Linux with pidfds,
Python 3.12+, Git, and a configured Mu on `PATH`.

```sh
uv sync
uv build
uv tool install .
# Refresh after updating the checkout:
uv tool install --force .
```

The checkout launcher (`./mub`) uses uv and does not require a separate build.

## Checks

```sh
uv run python -m compileall -q muboard tests
uv run python tests/smoke.py
```

Smoke checks use temporary Git worktrees and fake Mu processes, with no provider
calls. They exercise mailbox delivery, reader/writer concurrency, clean handoffs,
traps, failures, interruption holds, stale scheduler commands, persistence, and a real
PTY-driven UI workflow. Terminal checks cover cursor/erase updates, color,
split UTF-8, wide cells, alternate screens, hyperlinks, ignored bells,
and draining large output while preserving complete trap evidence.

Real-model scheduler replays are separate, opt-in experiments described in
[tests/scheduler_replay.md](../tests/scheduler_replay.md). They require an explicit
replay command and are not run by the smoke suite or CI.
