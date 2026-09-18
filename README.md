# Mu Board

A persistent terminal inbox and serial development queue for [Mu](https://github.com/ylxdzsw/mu).
Submit rough tasks, discuss designs, answer questions, and inspect workers without
managing their processes yourself.

**One checkout, one editing worker.** A fresh PM session triages incoming requests
and assesses each worker turn. Tasks keep their own Mu sessions for follow-ups;
project decisions and discussion survive independently of any PM's context.

## Run

Requires **Linux 5.3+ (pidfds), Python 3.12+, Git, and a configured `mu` on PATH**. No Python
runtime dependencies. Linux process-session tracking is used for safe recovery
and stopping worker descendants.

From this checkout:

```sh
./mub -C /path/to/project
```

Or install the `mub` entry point with your preferred Python package installer:

```sh
uv tool install .
mub -C /path/to/project
```

With no `-C`, the current working directory (`PWD`) is the project: `.mub` is
created right there, without searching parent directories. `-C` is an explicit
directory override and does not search parents either.
Mu keeps its usual configuration, instructions, skills, traps, compaction, and
session journals. If the directory has no Mu project scope, `mub` calls `mu init`.
No Mu configuration or instructions are otherwise rewritten.

Press **`M`** in the TUI to choose a model and reasoning effort for the PM, workers,
or both. The picker lists your configured models via `mu status --include-models`
without making model requests. Selections persist in `.mub` and apply to the next
invocation, including continuations; running processes are not interrupted.
Choose **Mu/session default** to stop overriding Mu's normal model selection.

With no saved selection, models inherit Mu's configuration. CLI options override
saved selections for the current launch:

```sh
mub --pm-model codex/gpt-5.6-luna:medium --worker-model codex/gpt-5.6-luna:high
```

Use `--model` to set both. A provider-qualified model avoids provider fallback.
Each task gets at most eight worker invocations before asking for another grant;
`--max-turns` changes that limit. `--timeout` bounds each invocation in seconds
(default 1800). Two unsuccessful PM plans pause automatic PM retries.

## The TUI

| Key | Action |
| --- | --- |
| `n` | New task dialog; optional title and multiline request |
| `m` | Project discussion, decisions, and latest PM execution |
| `M` | Choose PM/worker models and reasoning effort |
| `↑` / `↓`, `j` / `k` | Select a task |
| `Space` | Reply to the selected task; compose in the PM view |
| `Enter` | Task brief, discussion, and execution tabs |
| `1` / `2` / `3`, `Tab` | Switch detail tabs |
| `[` / `]` | Previous/next worker run in Execution |
| `PgUp` / `PgDn`, `Home` / `End` | Scroll details |
| `p` | Pause/unpause worker dispatch (the PM can still answer) |
| `r` | Ask a fresh PM to reconsider; clear a PM error |
| `+` / `-` | Raise/lower task priority |
| `s` | Interrupt the selected worker |
| `x` | Cancel the selected task |
| `R` | Explicitly resume a stopped/failed task or grant more turns |
| `a` | Review confirmation for a trapped-command retry |
| `b` | Accept the current checkout as a safe baseline |
| `q`, `Ctrl-C` | Quit dialog: keep running, finish current task, or stop now |

Editors use **Ctrl-S to submit**, Enter for a newline, Tab to switch fields, and
Esc to cancel. Dialogs do not stop process supervision. Live Mu output is captured
in Execution; opening a task does not start an extra editing process.

Requests appear in **Inbox** immediately. The PM can clarify, queue, add
prerequisites, split work, or update an existing task. Higher priority runs first,
then task ID. Dependencies require successful completion, not cancellation.
Questions appear in **Needs you**. Replies are delivered at a subsequent Mu turn,
not injected into a running tool call.

Project discussion is not automatically permission to implement. The PM records
agreed decisions and queues explicit requests. A worker's successful exit goes
to **Review**; the next PM accepts it, requests another turn, or asks a question.

## CLI alongside the TUI

Mutations go to the running TUI over a private local Unix socket. There is one
owner per project; another terminal can submit work without waiting for the PM.
The owner checks the socket peer's process identity: owned agents can inspect
state and the PM can submit plans, but interactive controls require user input.

```sh
mub add 'Add session expiration' --title 'Session expiration'
printf '%s\n' 'Use a fixed lifetime of seven days.' | mub reply T1
mub discuss 'What is blocking the release?'
mub priority T1 10
mub pause
mub unpause
mub replan
mub show T1
mub status
mub logs RUN_ID
mub stop T1
mub resume T1
mub quit --finish
```

`status`, `show`, and `logs` also work while the owner is closed. JSON status is
an inspection interface; do not edit the snapshot while a board is running.
New work requires an open board, not an implicit background daemon.

For automation, the same owner can run without curses:

```sh
mub run --headless
mub run --headless --until-idle   # drain existing actionable work, then exit
```

Headless output is newline-delimited JSON status. `--until-idle` stops for blocked
work, approval, paused dispatch, or a PM error as well as an empty queue; inspect
task status rather than treating owner exit as proof that every task succeeded.

## Checkout and approval safety

* There is no automatic stash, reset, branch switch, commit, push, or merge.
  Workers follow your project's Git conventions and task instructions.
* At startup, existing Git changes stop worker dispatch until you inspect and
  accept them with `b` or `mub accept-baseline --yes`.
* A worker owns the checkout across review, clarification, and repair turns.
  Marking its task done means the PM has accepted its changes as the next task's
  baseline; that does not require a commit. Failed/cancelled work is not silently
  passed to another task. You can explicitly release ownership after inspecting
  the checkout with `b`.
* Mu exit code 3 is a command-approval gate, not an automatic retry. Inspect the
  exact trapped command in Execution first. `a`, or `mub approve T1 --yes`, runs
  **one `mu retry --trap off` invocation**: this allows all Bash calls in that
  invocation, not just the displayed command. The following normal turn returns
  to your configured trap policy. Decline by cancelling the task.
  A trapped PM pauses triage too: inspect `m` → `3`, then press `a` there (or
  `mub approve-pm --yes`). The approved retry keeps the original plan's revision
  checks; all subsequent PM invocations are fresh again.
* Interrupted Mu sessions use `mu retry`; clean sessions use a new prompt in the
  same session. The PM cannot approve commands or clear a recovery gate.
* Closing the TUI stops its supervision. Finish-and-exit continues only the
  current task (including review/repair), stopping if it needs your intervention.
  Stop-now interrupts owned processes and preserves all records. For persistent
  remote use, run the TUI in a persistent terminal.
* Reopening checks unfinished runs against process identities and Mu's session
  locks. If an old worker or its process-session descendants remain alive, `mub`
  refuses to start a replacement. Inspect and stop those processes first.

The PM is a **trusted planning agent**, not an OS sandbox. Its prompt delegates
edits to workers, but unrestricted Bash can bypass that convention. Likewise,
ordinary `mu`, an editor, or an unrelated process can edit the checkout outside
the queue. Do not run another implementation agent in the same checkout.

## Persistence and architecture

All board state lives under `.mub/`, whose own `.gitignore` contains `*`. This
ignores the ignore file itself and every other entry: Git has no file to track
from the directory, without a rule in the outer repository. Previously tracked
files are not made untracked by any ignore rule.

```text
.mub/
  .gitignore       # *
  state.json       # tasks, discussion, decisions, event inbox, run metadata
  owner.lock       # OS lock, released when the owner exits
  control.sock     # private socket, present while the owner runs
  runs/
    <id>.prompt    # immutable execution brief
    <id>.log       # captured Mu output
```

The owner is the only state writer. Each update uses a flushed atomic snapshot
replacement; requests and worker outcomes are saved before subsequent work is
launched. Mu transcripts remain in Mu's own store and can be opened with
`mu transcript -s SESSION_ID` in the project.

PMs receive a bounded recent discussion plus current tasks and decisions. They
can inspect a task's full history with `mub show`. A PM stages a structured plan
using `mub plan` on stdin; the owner checks it and applies it only after a clean
PM exit. Task revisions reject plans made stale by user input or worker results.
Dependency validation rejects cycles. Inbox events arriving during a PM turn
remain pending for the next fresh PM. No agent recursively owns another Mu
process: the TUI launches both PM and workers as siblings.

The control protocol is intentionally local and small; there is no server to
install, scheduler daemon, worktree pool, remote executor, or agent framework.

## Checks

```sh
python -m compileall -q muboard
python tests/smoke.py
```

The smoke checks use temporary projects and a fake Mu executable; they make no
model requests. Real-model testing should use a temporary `MU_CONFIG_DIR` with
only the intended provider and model, not merely a project overlay (Mu deep-merges
provider configuration). Keep credential copies private and remove them afterward.
