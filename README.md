# mub — Mu session scheduler

A terminal UI for [Mu](https://github.com/ylxdzsw/mu) sessions with scheduled
message delivery. Navigate conversations like a terminal multiplexer, but queue
input at any time—even while a session is working.

**Every message goes through the scheduler.** Messages remain FIFO within each
session; the scheduler chooses between sessions. Sessions are conversations, not
tasks with a completion lifecycle.

## Run

Requires Linux with pidfds, Python 3.12+, Git, and configured `mu` on PATH. There
are no Python runtime dependencies.

```sh
./mub -C /path/to/worktree
# or
uv tool install .
mub
```

All directories in a Git worktree resolve to the same board at its root. Only
one mub owner may run in that worktree. Mu invocations run at the worktree root.
Do not run another editing agent against the same checkout outside mub.

## UI

The sidebar lists sessions. The conversation pane shows Mu history and live
output. The composer sends to the selected session. Mailbox entries are labeled
pending, in-flight, or interrupted; they are not mistaken for delivered history.
Drafts are kept separately for each session while the UI is open.

Create a session with `/new [name]`, then type its first message. Use `Ctrl-P` to
pick a session or inspect the scheduler's decisions and output.

| Shortcut | Action |
| --- | --- |
| `Tab` / `Shift-Tab` | Move between sidebar, conversation, and composer |
| `↑` / `↓` in sidebar | Select a session |
| `Enter` in sidebar/conversation | Focus the composer |
| `Enter` in composer | Queue a message |
| `Alt-Enter` or `Ctrl-J` | Insert a newline |
| `PgUp` / `PgDn` | Scroll conversation |
| `Home` / `End` in conversation | Beginning / follow output |
| `Ctrl-P` | Session picker, including scheduler output |
| `Ctrl-C` | Interrupt and hold the selected session |
| `Ctrl-Q` | Quit; confirm before stopping active agents |
| `Ctrl-A/E`, `Ctrl-U/K`, `Ctrl-W` | Ordinary line/word editing |

Local commands:

- `/new [name]`: create and select a session.
- `/close`: detach an idle session; confirm discarding any queued messages.
- `/models [scheduler|worker|both]`: select models and reasoning effort.
- `/resume`: release a session hold and explicitly authorize continuation of its
  interrupted turn, if any.
- `/schedule`: recheck the workspace and scheduling. After a scheduler error,
  explicitly start a fresh scheduler session instead of retrying its old turn.
- `/help`, `/quit`.

Use `//` to send a literal leading slash. Viewing output never sends keystrokes
to a worker. Dialogs keep process supervision running.

A workspace-owner marker (`◆`) can remain on an idle or held session. That
session still owns uncommitted changes and prevents another writer from starting.

## Scheduling

A persistent Mu scheduler wakes on message submissions and worker exits. It can
run alongside workers, but only one scheduler invocation runs at a time. Events
arriving during a scheduler turn are retained for another pass. Waiting does not
poll the model.

The scheduler sees pending messages, active workers, Git cleanliness, workspace
ownership, latest worker responses, exit reasons, and complete trap evidence.
It may dispatch several readers and at most one writer, or explain why nothing
should run. It returns a small JSON decision as its final answer; runtime code
validates it against current state before acting.

The scheduler reasons about prerequisites from messages and outcomes; there is
no dependency graph, numeric priority system, task decomposition, or PM review.
A clean exit means the turn returned—not that mub verified its implementation.

### Readers and the writer

- Multiple readonly workers may run alongside one readwrite worker.
- A session can have only one invocation at a time.
- Readers observe the **live checkout**, not an isolated snapshot. The scheduler
  can defer reads that need a stable baseline.
- A new writer requires a clean checkout, or must be the session that already
  owns its dirty changes.
- An idle, trapped, failed, or held session retains ownership while its changes
  remain uncommitted. Independent readers may still run.
- Existing changes without a known owner block writer dispatch. Resolve them
  manually, then use `/schedule` to recheck.

Git checks include staged, unstaged, and non-ignored untracked files. mub never
automatically stashes, resets, switches branches, rolls back, pushes, or accepts
abandoned changes as another worker's baseline.

### Traps and handoffs

Readonly workers use `--trap reversible`. These are Mu's **model-declared risk
traps, not a sandbox**. A trapped reader may be promoted to writer only when the
writer slot and workspace ownership permit it.

The scheduler may resolve a trap within the user's authorized work. Trap
relaxation applies to the **rest of that turn**, not one command. Ordinary writes
start with `--trap destructive`; the scheduler can choose `off` when that broader
permission is justified. A subsequent normal turn gets a fresh access/trap
policy. Single-command approval is not implemented.

For a dirty handoff, the scheduler may ask the owning session to commit only its
completed, task-owned changes or explain why it cannot. This is a visibly
scheduler-authored request, which may precede queued user messages. It is not an
implementation follow-up or permission to commit incomplete/unrelated work.
An unsuccessful handoff does not cause repeated commit requests without new user
input.

The scheduler does not investigate implementation quality, repair failures,
troubleshoot provider issues, or work around Mu bugs. Failures are exposed and
may block dependent messages. They are not automatically retried.

### Interruption and shutdown

Interrupting a session records a user hold before signaling its processes. The
hold blocks all automatic work, including queued messages, retries, and commit
requests. Selecting a session, scrolling, or changing models does not release it.

A new message releases the user hold for scheduling, but **does not authorize
retrying an interrupted turn**. `mu retry` continues the old turn and cannot
accept changed instructions. Use `/resume` explicitly after inspecting it; new
messages remain queued behind the interrupted work.

Quitting with active workers or a scheduler asks for confirmation, defaulting to
Cancel. Confirmation stops every owned invocation and records interruptions
before exiting. Workers are not left detached. Reopening does not silently
restart user-interrupted sessions.

There are no mub execution deadlines, inactivity watchdogs, or retry cooldowns;
Mu owns execution timeouts. Explicit stopping uses SIGINT, followed by SIGKILL
for remaining owned processes after a short shutdown grace period. A second
interrupt forces termination immediately.

## CLI and headless use

An open TUI or headless owner accepts mutations over a private local socket.
Submissions do not implicitly start a daemon.

```sh
mub new --name API 'Implement the API endpoint'
mub send S1 'Use cursor-based pagination'
mub new --name Investigation 'Read the existing authentication flow'
mub status
mub logs S1
mub interrupt S1
mub resume S1
mub remove S2                   # add --discard to discard pending messages
mub models
mub models --scheduler provider/model:low --worker provider/model:high
mub models --worker default
mub schedule
mub quit --yes                  # explicitly stop running agents

mub run --headless
mub run --headless --until-idle
```

`send` also reads text from stdin. `status` and `logs` work without an open owner.
`--until-idle` exits when no invocation or actionable scheduling event remains;
held, blocked, or failed sessions may still have pending messages.

`--scheduler-model`, `--worker-model`, and `--model` override models for the current
launch. UI/CLI model selections persist and apply only to later invocations,
including existing sessions. “Mu/session default” removes an override.

## Persistence

**`.mu/mub.json` is the only mub-owned durable state file.** It contains session
references, mailboxes, in-flight invocation records, holds and outcomes, workspace
ownership, pending scheduling events, and model choices. It is atomically replaced
and locally Git-ignored via Git's `info/exclude` without editing tracked ignore
configuration.

Mu journals own delivered conversations. mub does not copy transcripts or archive
terminal logs. Live output and full trap evidence are transient; history replays
from Mu. Only bounded recent outcome excerpts are kept for scheduling. A stable
owner lock and a live control socket are runtime artifacts in `.mu/`.

A journal offset records each delivery boundary, allowing mub to distinguish a
completed message from an invocation that never accepted its input. A crash
leaves uncertain in-flight work held for explicit inspection and continuation;
messages are not blindly resubmitted. Live processes from a previous
owner prevent a replacement owner from starting. Removing a session only detaches
it from mub; it never deletes the Mu journal or undoes edits.

Old task-board snapshots are **not automatically converted**. Opening one fails
without overwriting it. Archive the old `.mu/mub.json` explicitly before starting
a new board; Mu journals remain untouched.

## Checks

```sh
python -m compileall -q muboard tests
python tests/smoke.py
```

Smoke checks use temporary Git worktrees and fake Mu processes, with no provider
calls. They exercise mailbox delivery, reader/writer concurrency, clean handoffs,
traps, failures, interruption holds, stale decisions, persistence, and a real
PTY-driven UI workflow.
