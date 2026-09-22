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

Enter **`/models`** in the TUI prompt to choose a model and reasoning effort for the PM, workers,
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

The main screen keeps the task list above the PM conversation and prompt. The
list grows with the number of tasks, up to half the available space, then scrolls;
the conversation fills the rest. Nothing is selected initially. Selecting a task
opens its live output in a right column; clearing the selection collapses it and
gives the PM the full width again. The right column starts wider than the left and
can be resized with `F5` / `F6` (subject to minimum column widths).

Just start typing: ordinary letters are never board shortcuts while the PM prompt has focus.
Tell the PM what to build, discuss a design, or answer a question (mention `T1`,
`T2`, etc. when referring to a task). The last line shows the available keys.

| Key | Action |
| --- | --- |
| Type, then `Enter` | Send a message to the PM |
| `Ctrl-J` | Insert a newline; `Ctrl-S` also submits |
| `←` / `→`, `Home` / `End`, Backspace / Delete | Edit the prompt; `Ctrl-U` clears it |
| `↑` / `↓` | Select a task and show its right pane without leaving the prompt |
| `Tab`, or `Enter` with an empty prompt | Focus task output (select the first task if needed); `Tab` switches back to the PM |
| `Esc` | Clear the task selection and collapse the right pane |
| `F5` / `F6` | Shrink / widen the right pane |
| `F2` | PM conversation, decisions, and execution history |
| `/help`, then `Enter` | List slash commands; choose one to insert it in the prompt |
| `Ctrl-C` | Quit immediately when idle; otherwise confirm stop-and-quit once |
| `Ctrl-D` on an empty PM prompt | Quit with the same confirmation behavior as `Ctrl-C` |

Manage tasks by talking to the PM, for example:

- “Prioritize the login fix before the dashboard work.”
- “Pause the queue after the current worker finishes.”
- “Stop T2 for now.”
- “Yes, go ahead with the retry you described.”

The PM interprets intent, arranges the queue, and handles worker recovery. You do
not need numeric priorities, approval commands, or special approval phrases.
Dependencies still run before their dependents, and reordering never takes the
checkout away from its current worker. The task list reflects the PM's order
with prerequisites placed first.

Only local UI commands remain: `/help`, `/models`, and `/quit`. Prefix a message
with `//` to send a literal leading `/` instead of invoking a local command.

Task output opens at the latest run and follows new output automatically, like a
read-only terminal pane. Mu uses **concise output** for normal turns and retries.
With task output focused, `↑` / `↓` and `PgUp` / `PgDn` scroll; `Home` goes to the beginning, including the
task brief and discussion; `End` resumes following. `[` / `]` select previous or
next runs. `Tab` returns to the PM prompt while keeping the task visible; `Esc`
clears the selection and closes the pane. Both preserve your unfinished prompt.
Finished tasks stay in the list and open the same saved output after a restart.

This is a live view of durable output, not a tmux attachment or an interactive
shell. Opening it neither launches another Mu process nor forwards keystrokes to
the worker. Output is loaded incrementally, including older scrollback rather
than just a truncated log tail. Dialogs continue process supervision.

Messages are saved immediately. The PM can turn explicit requests into tasks,
clarify, add prerequisites, split work, or update existing tasks. Dependencies
require successful completion, not cancellation. A task is **blocked** only when
it needs a user decision or permission; answer the PM in ordinary language.
Replies reach a subsequent Mu turn, not a running tool call. The CLI's `mub add` still creates an inbox task
immediately when you want explicit submission instead of conversation.

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
mub discuss 'Prioritize T1 once its prerequisites are finished.'
mub discuss 'Pause worker dispatch.'
mub discuss 'Resume the queue.'
mub show T1
mub status
mub logs RUN_ID
mub discuss 'Stop T1 for now.'
mub reply T1 'Continue with the approach you described.'
mub quit --finish
```

`status`, `show`, and `logs` also work while the owner is closed. JSON status is
an inspection interface; do not edit the snapshot while a board is running.
New work requires an open board, not an implicit background daemon. Low-level
CLI controls remain available for emergency recovery and automation; normal task
management uses `discuss` and `reply`.

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
* At startup, existing Git changes stop worker dispatch. Ask the PM to inspect
  them; it can accept the baseline when your message authorizes that decision.
  The PM cannot release a running worker's checkout.
* A worker owns the checkout across review, clarification, and repair turns.
  Marking its reviewed task done accepts its changes as the next task's baseline;
  that does not require a commit. Failed/cancelled work is not silently passed to
  another task. Ask the PM to inspect abandoned changes before releasing ownership.
* A worker's Mu exit code 3 returns the trapped command to **PM review**. The PM
  inspects it and can authorize routine work already within your request. If the
  action genuinely needs your decision, the PM marks the task **blocked**, explains
  the action and scope, and waits for your natural-language answer. Refusals,
  changed scope, and unrelated replies are not approval.
* An approved worker retry is **one `mu retry --trap off` invocation**: all Bash
  traps are disabled for that invocation, not just the displayed command. The PM
  must consider that broader scope, not merely the isolated command. Later normal
  turns return to configured traps. The recovery reason and any authorizing user
  message are saved with the run. A new relevant message invalidates an approved
  retry that has not started yet, so the PM must assess it again.
* A blocked task cannot resume just because a worker claims permission. The PM
  must cite a subsequent user message and explain its decision. The engine checks
  the message's source and freshness; the PM interprets its meaning. A changed
  blocking question requires a new answer. Interrupted/user-stopped work and
  extra turn-budget grants likewise require user input.
* Interrupted Mu sessions use `mu retry`; clean sessions use a new prompt in the
  same session. A retry completes the interrupted turn before new instructions
  can be delivered: a changed brief is not permission to execute an old trapped
  command. Routine retries do not reset the worker-turn budget.
* The PM cannot grant itself a trap override. If the PM traps or fails, inspect
  **F2 PM** and send a message to start a fresh PM turn under configured traps.
  It can choose another allowed approach instead of repeating the blocked action.
* Closing the TUI stops its supervision. Quitting is immediate when no agent is
  running and no task is awaiting review; queued tasks remain saved for next time.
  Otherwise a single confirmation interrupts owned processes and preserves all
  records and checkout changes. The CLI's `mub quit --finish` still finishes only
  the current task (including review/repair), stopping if it needs intervention.
  For persistent remote use, run the TUI in a persistent terminal.
* Reopening checks unfinished runs against process identities and Mu's session
  locks. If an old worker or its process-session descendants remain alive, `mub`
  refuses to start a replacement. Inspect and stop those processes first.

The PM is a **trusted planning and worker-approval agent**, not an OS sandbox.
It judges authorization from conversation; the board does not hard-code approval
phrases or independently infer their meaning. Its prompt delegates edits to
workers, but unrestricted Bash can bypass that convention. Likewise,
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
PM exit. Task revisions and user-message watermarks reject plans made stale by
new input or worker results. PM order is persisted and dependency validation
rejects cycles; dispatch always requires successful prerequisites. Inbox events arriving during a PM turn
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
