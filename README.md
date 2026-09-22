# Mu Board

A conversational worker supervisor and ordered work queue for
[Mu](https://github.com/ylxdzsw/mu). **One checkout, one editing worker.**

The PM understands user messages and decides what happens next. Workers do the
technical work. The orchestrator owns processes and remembers the board.

## Roles

The PM has two jobs:

1. **Supervise workers.** Assess progress and actual completion, request focused
   follow-ups, choose retries and justified traps-off recovery, and ask the user
   about genuine blockers. Normally ask the worker to commit its task changes
   before handing the checkout to another task. Waive this when there are no
   changes or the next task can safely continue without an isolated baseline;
   record the reason.
2. **Manage the queue.** Interpret status questions, new requests, clarifications,
   priority changes, cancellations, and pause/resume requests. A message can
   request multiple tasks. Infer prerequisites and arrange the queue accordingly;
   otherwise preserve submission order.

The PM inspects evidence proportionately; substantial investigation, technical
design, implementation, checks, and commits belong to workers. Discussion is not
permission to implement. There is no autonomous roadmap, separate project-wide
knowledge base, or expectation that the PM independently solves tasks.

The orchestrator launches and monitors processes, enforces checkout ownership,
handles persistence and recovery, and bounds retries. It does not judge semantic
completion or understand task dependencies. PMs and workers are sibling Mu
processes, not recursively owned agents.

## Tasks and dispatch

A task has an ID, display title, lifecycle state, worker session reference, and a
**free-form PM note**. The note preserves the request, relevant clarifications,
prerequisites, progress, and handoff considerations without required headings.
Supervisor bookkeeping is grouped separately under `execution`; it is not
PM-editable planning metadata.

The task list is the queue order. There are **no dependency pointers or numeric
priorities**. The PM reasons about prerequisites from task notes and outcomes.
Ordering alone is not proof that a prerequisite succeeded.

Dispatch is an explicit, single-use PM authorization for the first unfinished
queued task. The engine never drains the queue automatically or skips a blocked
head. After a worker outcome or relevant user input, the PM reassesses before
another worker invocation. It can move independent work ahead, but cannot give
another task a checkout still owned by the current worker. Reordering does not
interrupt running work.

A clean worker exit goes to **Review**, not Done. The PM may request more work,
ask a question, or accept completion with an explicit checkout-handoff
explanation. A worker retains ownership through review, clarification, and repair
turns. Commits are made by the worker when asked, not automatically by the engine.

The PM stages a small JSON plan through `mub plan`:

```json
{
  "reply": "The endpoint needs to come before its client.",
  "tasks": [
    {"id": "endpoint", "title": "Endpoint", "state": "queued", "note": "Implement the requested endpoint."},
    {"id": "client", "title": "Client", "state": "queued", "note": "Implement its client after the endpoint succeeds. Reconsider if that work fails."}
  ],
  "order": ["endpoint", "client"],
  "dispatch": "endpoint"
}
```

Fields are optional except task patch IDs. New tasks need a title and note;
notes replace their previous text in full. `order` moves listed tasks to the
front and preserves the relative order of omitted tasks. Omitted or null
`dispatch` means wait. A completion patch needs `handoff`, explaining the verified
commit/clean checkout or why a clean baseline is unnecessary. Handoff and
recovery explanations are saved with the conversation or run, not as new planning
fields. Plans apply only after a clean PM exit; stale plans are rejected.

## Run

Requires **Linux 5.3+ (pidfds), Python 3.12+, Git, and configured `mu` on PATH**.
There are no Python runtime dependencies.

```sh
./mub -C /path/to/project
```

Or install the entry point:

```sh
uv tool install .
mub -C /path/to/project
```

Without `-C`, the current working directory is the board's project; the board
does not search parent directories. Mu retains its configuration, instructions,
skills, traps, compaction, and journals. If Mu reports no project scope, the CLI
calls `mu init`.

Use **`/models`** in the TUI to select a PM model, worker model, or both. Choices
persist and apply on the next invocation, including continuations. Selecting
**Mu/session default** removes the override. Listing models makes no model
requests. CLI options override saved choices for the current launch:

```sh
mub --pm-model codex/gpt-5.6-luna:medium --worker-model codex/gpt-5.6-luna:high
```

Use `--model` to set both. Provider-qualified names avoid provider fallback.

## TUI

Type ordinary messages to the PM: “Build these two features,” “What is blocking
T2?”, “Prioritize T3,” or “Pause after this worker.” Mention task IDs when useful.
Letters are not board shortcuts while the prompt has focus.

The task list sits above the PM conversation. Selecting a task opens its output
in a right pane; clearing the selection restores the full-width conversation.

| Key | Action |
| --- | --- |
| Type, then `Enter` | Send a message |
| `Ctrl-J` | Insert a newline; `Ctrl-S` also submits |
| `←` / `→`, `Home` / `End`, Backspace / Delete | Edit prompt; `Ctrl-U` clears it |
| `↑` / `↓` | Select a task without leaving the prompt |
| `Tab`, or `Enter` on an empty prompt | Focus task output; `Tab` returns to the prompt |
| `Esc` | Clear selection and collapse the task pane |
| `F5` / `F6` | Shrink / widen the right pane |
| `F2` | PM conversation and execution history |
| `/help`, `/models`, `/quit` | Local UI commands |
| `Ctrl-C`, or `Ctrl-D` on an empty prompt | Quit; confirm once if work or review is active |

With output focused, arrows and `PgUp` / `PgDn` scroll, `Home` shows the beginning,
`End` follows new output, and `[` / `]` select previous/next runs. Your prompt
draft is preserved. Prefix a message with `//` to send a literal leading `/`.

Output is read-only: opening it neither launches another agent nor forwards
keystrokes to a worker. While the owner is open, invocation output is captured
in temporary streams. **After reopening, history replays the referenced Mu
session, including all its turns**, not an exact copy of an individual invocation's
terminal output. The pane labels this distinction. Old migrated runs can still
use their archived logs. Dialogs continue process supervision.

## CLI and headless use

Mutations go to the running owner over a private Unix socket. Only one owner may
run per project. Status, task history, and Mu transcript replay also work offline.

```sh
mub add 'Add session expiration' --title 'Session expiration'
mub reply T1 'Use a fixed lifetime of seven days.'
mub discuss 'Prioritize T1 after its prerequisites are ready.'
mub discuss 'Pause worker dispatch.'
mub discuss 'Stop T2 for now.'
mub discuss 'Yes, go ahead with the retry you described.'
mub show T1
mub status
mub logs RUN_ID
mub quit --finish

mub run --headless
mub run --headless --until-idle
```

`--until-idle` exits when no work is actionable, including blocked or paused work,
missing dispatch authorization, or an error. Inspect task states; owner exit is
not proof that all tasks succeeded. New submissions require an open owner, not
an implicit background daemon.

Low-level `stop`, `cancel`, `resume`, `approve --yes`, `accept-baseline --yes`,
`pause`, `unpause`, and `replan` controls remain for recovery and automation.
Resume/approval still trigger PM queue reassessment before dispatch. Owned
agents may inspect state and the PM may stage plans, but user controls reject
owned-agent callers based on socket peer process identity.

## Recovery and safety

- Startup reconciles the snapshot with actual processes, Mu session locks, and
  Git before dispatch. Live descendants of an old invocation prevent reopening
  with a replacement worker. Interrupted work requires user input to resume.
- Existing checkout changes without an owner stop dispatch. The PM may accept
  them only with user authorization after inspection. Failed/cancelled work is
  not silently passed to another task. There is no automatic stash, reset,
  branch switch, push, or merge.
- Worker exit code 3 goes to PM review. The PM inspects the complete trapped
  command and stdin, then decides whether it is routine work already authorized
  by the user's request or requires a user decision. Worker claims are evidence,
  not authorization.
- An approved recovery is **one `mu retry --trap off` invocation**: all Bash traps
  are disabled for that invocation, not just the displayed command. The PM must
  judge that broader scope. Later normal turns restore configured traps. An
  unclean traps-off retry requires fresh user authorization before another retry.
- Interrupted sessions use `mu retry`; clean sessions receive a new prompt in
  the same session. Retry completes the old turn before new instructions can be
  delivered. A changed request is not permission to execute a rejected old command.
- Unblocking requires a relevant subsequent user message and a PM explanation.
  The engine checks source/freshness; the PM interprets meaning. New relevant
  messages invalidate pending approved retries and dispatch authorization.
- The PM cannot approve its own trap override. A failed/trapped PM normally
  restarts in a fresh session after user input. The explicit user-only
  `approve-pm --yes` escape hatch permits one traps-off retry.
- Quitting stops supervision and interrupts owned processes after confirmation.
  `quit --finish` finishes only the current task, including review/follow-up,
  stopping if intervention is needed. For remote use, keep the TUI in a
  persistent terminal.

The PM is trusted, not sandboxed. Its prompt delegates edits to workers, but
unrestricted Bash can bypass that convention. Do not run another implementation
agent in the same checkout.

### Loop bounds

These are engine checks, not just PM instructions:

- **32 PM/worker invocations per grant** (`--max-runs`), including failed launches.
  Counts survive restart and ordinary messages. Only user `mub replan` grants a
  new board batch; it does not reset task limits or cooldowns.
- **Eight worker invocations per task grant** (`--max-turns`). More require fresh
  user evidence, which cannot be reused for later grants.
- Two consecutive unsuccessful worker invocations block for user input. Two
  unsuccessful PM plans pause automatic PM retries. A PM invocation accepts at
  most three plan submissions, including corrections.
- Retry cooldowns begin at 30 seconds and increase with the failure streak.
  They survive restart and user replies.
- **Inactivity timeout:** `--idle-timeout` (alias `--timeout`) defaults to 3600
  seconds. Captured output, Mu journal activity, and owned-process CPU/I/O or
  process changes count as activity; board polling and unrelated edits do not.
- **Absolute fallback:** `--max-runtime` defaults to 86400 seconds, even for
  active/noisy runs. Timeout type and stop detail are saved. Stops send SIGINT,
  then SIGKILL to remaining owned process-session members after five seconds.

Inspect the cause before granting more work. An invocation is not an API request;
Mu can perform many model/tool calls within it. The board neither meters tokens
nor guarantees useful progress or a dollar ceiling. Use provider spending limits
where available.

## Persistence

**`.mu/mub.json` is the single durable board snapshot.** It contains ordered tasks
and notes, user/PM conversation and pending events, pause/dispatch state, checkout
ownership, session/run references and recovery bookkeeping, and saved model
choices. Run outcomes include bounded excerpts for PM assessment, not full
transcript copies. Mu journals remain the source of detailed agent history; new boards do
not write separate durable run logs, prompt files, or a decisions database.

```text
.mu/
  mub.json       # durable board snapshot (private, Git-ignored)
  mub.lock       # stable owner lock, separate from the replaced snapshot
  mub.sock       # private control socket while the owner runs
  sessions/      # Mu's existing journals, not board-owned copies
```

The owner alone writes the snapshot, using a flushed temporary file and atomic
replacement. A PM requests updates through plans, never by editing the snapshot.
The owner adds targeted board entries to `.mu/.gitignore`, preserving existing
entries; the rest of `.mu/` can contain tracked configuration and instructions.
Previously tracked files are not untracked by ignore rules.

On first opening an old board, `.mub/state.json` is imported into the new format:
old priority/dependency order becomes list order, requests/briefs/prerequisites
become notes, and project decisions are retained as an imported conversation
entry for the PM to carry into relevant task notes. Existing conversation,
execution history, ownership, and budgets survive. The legacy owner lock is
honored to prevent simultaneous old/new owners. **The old `.mub/` files are left
untouched as an archive**; do not resume the same project with an older mub binary.
After import, `.mu/mub.json` is authoritative. Reopening always requires fresh
PM dispatch authorization rather than replaying a saved launch decision.

## Checks

```sh
python -m compileall -q muboard
python tests/smoke.py
```

Smoke checks use temporary Git projects and a fake Mu executable, with no model
requests. For real-model checks, use an isolated temporary `MU_CONFIG_DIR` with
only the intended provider/model; Mu deep-merges provider configuration. Keep
credential copies private and remove them afterward.
