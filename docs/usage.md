# Usage reference

[Home](../README.md) · [Design](design.md) · [Development](development.md)

## Run

Requires Linux with pidfds, Python 3.12+, Git, and configured `mu` on PATH
supporting `--automated`, `--no-context`, and `mu retry --approve`. The scheduler uses ordinary Mu
tool calls and `mub` commands rather than a structured final decision.
Installation includes the embedded Rust terminal engine
`par-term-emu-core-rust` (and its Pillow dependency); no shell, GUI toolkit,
terminal server, or system terminal library is needed.

```sh
uv tool install .
mub -C /path/to/worktree
# From the checkout (requires uv; manages dependencies automatically):
./mub -C /path/to/worktree
# Refresh an existing tool installation after updating the checkout:
uv tool install --force .
```

All directories in a Git worktree resolve to the same board at its root. Only
one mub owner may run in that worktree. Mu invocations run at the worktree root.
Existing branches (including an unborn branch) and detached HEADs are left
unchanged. When starting a board outside a Git repository, mub initializes the
selected directory on `master`; the read-only `context` command never
initializes Git. mub does not switch branches or choose a remote's default branch.
Do not run another editing agent against the same checkout outside mub.

Scheduler and worker turns, including retries, use Mu's `--automated` flag:
they leave the shell's `current-session` selection untouched and suppress
completion bells. Their sessions remain journaled and listed by Mu.

## UI

The sidebar lists sessions. The read-only conversation pane displays Mu's native
PTY output: Markdown, tool calls, colors, progress updates, and terminal wrapping.
Saved history is rendered by `mu transcript` on a sized PTY, not reinterpreted
as Markdown. mub adds only the live model/context/cwd header and literal `mu>`
prompt; retry does not duplicate the user prompt. History preparation runs
behind the durable launch gate, so it cannot overlap the new turn's journal.
Independent readers drain every worker PTY, including hidden/headless sessions.
The scheduler's XML snapshot and command feedback stay on a separate
non-terminal path. Scheduler diagnostics, including compaction reports, remain
visible in the scheduler pane. Trap evidence uses complete captured output
rather than a bounded screen snapshot.
The composer is always active, sends to the selected session, and grows with its
draft up to four visible rows. When text is hidden, its divider shows `↑ N more`
and/or `↓ N more`, counting wrapped rows above and below the visible prompt.
The whole draft is sent, including hidden text.
There is no pane focus: typing edits the prompt, Tab completes an open
command panel or switches sessions otherwise,
and output navigation leaves the prompt cursor alone. Dialogs and pickers
temporarily take keyboard input.
Mailbox entries are labeled
pending, in-flight, or interrupted; they are not mistaken for delivered history.
Steering is labeled separately from ordinary queued messages.
Drafts, prompt cursors, and output positions are kept separately for each session
while the UI is open. Scrolling up pauses live following; typing and new output
do not jump back to the bottom. Scroll to the bottom or use Ctrl-End to follow
live output again.

An active invocation keeps its launch-time terminal dimensions. After narrowing
the pane, use `Shift-←` / `Shift-→` to pan horizontally. Idle history
replays when the pane width changes. Height-only changes reveal or hide rows
without rebuilding history; task completion and subsequent turns reuse the live
screen at the same width. Failed/interrupted screens stay available so transient
errors are not lost. Unused cells are not rewrapped by curses. Colors are mapped
to the outer terminal's available palette (RGB colors to the nearest ANSI color).
The display retains 10,000 scrollback rows; Mu journals remain the durable history.
Headless workers use an 80-column, 24-row terminal.

Terminal bells are ignored. OSC 8 hyperlinks remain in the terminal model,
are underlined, and their targets are available with `/links`; nothing opens
automatically. Worker title changes never rename the host terminal. Clipboard
requests, terminal replies, graphics and external notifications are not forwarded.
The engine interprets alternate screens and cursor controls, but mub does not
forward keyboard/mouse input or claim to host arbitrary interactive applications.

Type your first message to automatically create a session, or use `/new [name]`
to create one explicitly. Use `Tab` / `Shift-Tab` to switch sessions and
`/scheduler` to inspect the scheduler's decisions and output.
The conversation pane shows the session's model even before its first message.
Use `/model session` to choose a model, thinking effort, and fast mode for that
session only, whether it is new or already has history. In one picker, use
`↑` / `↓` to select a model, `←` / `→` to adjust its effort slider, and Space
to toggle fast mode. The picker previews the full model reference; Enter applies
it and Esc cancels. Fast on adds `@fast` (priority, which may cost more); fast
off omits the suffix and uses the upstream default. The effort slider includes
default and each model's supported levels. Selecting inheritance leaves both
effort and fast mode to the inherited model selection.
Mu handles provider support; native Anthropic Messages rejects explicit
processing preferences. CLI model options also accept `@standard`, for example
`--scheduler-model 'openai/gpt-5:high@standard'`.
Choose explicit scheduler/worker models for predictable automation; otherwise
Mu retains its normal session and `current-session` model inheritance.
Scheduler status appears once in the top bar. Errors, holds, and blocking reasons
appear only when relevant; model defaults and active models are available through
`/model`, and the full keyboard reference through `/help`.

Session lists show only the ID, one status icon, and the title:
`✎` writing, `≋` reading, `○` idle after writing, `●` idle after reading,
`⇥` committing to close,
`⇢` steering, `·` new/idle, `×` failed or trapped, `■` held/interrupted/stopping,
and `…` blocked. Idle icons reflect the last run's mode,
not whether its output has been viewed. Queue details remain in the conversation pane.

| Shortcut | Action |
| --- | --- |
| `Tab` / `Shift-Tab` | Next / previous session, wrapping around; skips scheduler output. Tab fills the selected command when the command panel is open |
| `↑` / `↓` / `←` / `→` | Move the prompt cursor (or select/fill a visible slash command) |
| `Enter` | Queue a message |
| `Shift-Enter`, `Alt-Enter`, or `Ctrl-J` | Insert a newline |
| `PgUp` / `PgDn` | Scroll output by a page with two lines of overlap |
| `Shift-↑` / `Shift-↓` | Scroll output one line |
| `Ctrl-Home` / `Ctrl-End` | Oldest retained output / follow live output |
| `Shift-←` / `Shift-→` | Pan a wider terminal left / right |
| Mouse wheel over output | Scroll output without moving the prompt cursor |
| Click a sidebar session | Select it, keeping input in the composer |
| `Ctrl-N` | Create and select a new session, preserving the previous session's draft |
| `Ctrl-C` | Clear the draft only, even if already empty; use `/interrupt` to stop work |
| `Ctrl-D` (EOF) | Close the selected idle session; repeat while its dirty-workspace warning is displayed to commit and close; quit if no sessions are open |
| `Q` / `Esc` in dialogs | Close information screens or cancel a picker |
| `Home` / `End` in composer | Start / end of line |
| `Ctrl-←` / `Ctrl-→` in composer | Jump between words |
| `Ctrl-Backspace` in composer | Delete previous word |

Modified keys depend on terminal support. Mouse reporting does not request motion
events; use your terminal's native-selection bypass (often Shift-drag) to select
and copy text.

Local commands:

- `/new [name]`: create and select a session.
- `/rename <name>`: name the selected session and protect its name from automatic changes.
- `/rename --auto`: let the scheduler name and rename the selected session again.
- `/discard`: discard all ordinary pending messages for the selected session,
  without closing it or changing workspace files or ownership. Works even while
  the session is active, held, or owns uncommitted changes. In-flight and
  interrupted messages, pending steering redirects, and Mu history are preserved;
  this does not interrupt or resume an unfinished turn.
- `/close`: detach an idle session; confirm discarding any queued messages.
  If it owns a dirty workspace, show a warning first. Repeat `/close` or `Ctrl-D`
  while that warning is displayed to ask the session to commit its completed,
  task-owned changes and close automatically once the commit succeeds and the
  workspace is clean. Switching sessions, opening a dialog, or otherwise hiding
  or replacing the warning cancels that confirmation; the next close only shows
  the warning again. Held sessions and interrupted turns require `/resume` first.
  Refusal, failure, or remaining changes keep the session open, without repeated
  commit attempts. Queued messages are discarded only on successful close after
  confirmation; new messages arriving during the commit prevent automatic close.
- `/model`: show the selected scheduler/worker defaults and active invocation models.
- `/model scheduler|worker|both`: select models, thinking effort, and fast mode.
- `/model session`: select the current session's model, effort, and fast mode, or remove its override.
- `/links`: show hyperlink targets from the selected terminal without opening them.
- `/interrupt`: interrupt and hold the selected session.
- `/resume`: release a session hold and explicitly authorize continuation of its
  unfinished turn, if any. This asks the scheduler to reconsider; it does not
  immediately launch a retry or grant permission for out-of-scope work. The UI
  distinguishes authorization, a running retry, and a renewed blocking reason.
- `/steer <text>`: directly admit a redirect at Mu's next safe boundary, or submit
  a turn if the selected session is idle and clean. It needs no scheduler decision
  when no policy promotion or dirty handoff is needed, and works while the
  scheduler is held. A redirect inherits the prior access mode (readonly if there
  is none) but gets fresh normal traps: reversible for readonly, destructive for
  readwrite; `off` is never inherited. It remains within the one-invocation limit
  and obeys writer and dirty-owner restrictions. Ordinary messages stay FIFO and
  never interrupt the current invocation; they remain queued behind the redirect.
  Old pending tool calls are superseded, not retried, and the redirect remains in
  Mu history; neither implies task success. Holds and recovery gates still apply.
  For an inactive interrupted, trapped, or failed session, the UI confirms that
  old pending calls will not be retried. The TUI's `/steer` command itself is
  explicit authorization; no extra flag is needed.
- `/schedule`: recheck the workspace and scheduling. After a scheduler error,
  explicitly start a fresh scheduler session instead of retrying its old turn.
- `/scheduler`: select the scheduler's decisions and output without triggering a
  scheduling pass. Use `Tab` / `Shift-Tab` to return to worker sessions.
- `/help`, `/quit`.

Typing `/` in the composer opens a bounded command list, filtered as you type.
Use `↑` / `↓` to select, `Tab` or `→` at the end of the draft to fill the command before
adding arguments, `Enter` to run it, or `Esc` to hide the list. At most five commands are shown;
the list scrolls with the selection. Tab switches sessions only when the command
list is closed; Shift-Tab always switches to the previous session.

Use `//` to send a literal leading slash. Viewing output never sends keystrokes
to a worker. Dialogs keep process supervision running.

A workspace-owner marker (`◆`) in the conversation header can remain on an idle or held session. That
session still owns uncommitted changes and prevents another writer from starting.

## CLI and headless use

Plain `mub` launches the TUI. An open TUI or headless owner accepts scheduler
commands over a private local socket; commands do not implicitly start an owner.
The root startup flags remain available for automation and testing:

```sh
mub -C /path/to/worktree
mub -C /path/to/worktree --headless
mub -C /path/to/worktree --headless --until-idle
mub -C /path/to/worktree --headless --scheduler-model provider/model
```

The scheduler has four immediate CLI actions and no JSON decision protocol:

```sh
mub run S12 --trap reversible
mub run S12 --trap destructive
mub run S12 --trap off
mub run S12 --trap reversible --approve
mub run S12 --trap destructive --approve
mub rename S11 "API pagination"
mub mark-failed S10 "Cannot proceed within the user's authorization"
mub note S13 "Waiting for S10 to implement the endpoint"
mub note S13 --clear
```

`run` requires one of the three trap settings. The runtime chooses dispatch or
continuation and returns immediately after admission or handoff initiation.
Optional `--approve` is only accepted for a trapped retry with command evidence;
it grants a single-command exception, not recovery authorization or lasting approval.
`rename`, `mark-failed`, and `note` likewise return a plain-text result. The
scheduler pass identifier is carried in `MUB_PASS_ID` when the scheduler uses
these commands; the owner validates it along with the caller.

The only other CLI subcommand is the read-only history inspector:

```sh
mub context S1                              # exact requests and final responses
mub context S1 --requests                   # inspect scope and restrictions only
mub context S1 --before 12345 --turn t3     # one turn from a journal prefix
```

`context` returns JSON and can read history without an open owner. There are no
CLI status, logs, polling, model, shutdown, message, steering, hold, or session
lifecycle controls. Those remain TUI user controls, including `/new`, `/model`,
`/interrupt`, `/resume`, `/steer`, `/schedule`, `/scheduler`, and `/quit`.
`--until-idle` exits when no invocation or actionable scheduling event remains;
held, blocked, or failed sessions may still have pending messages. A scheduler
failure keeps the owner alive even with an empty mailbox until the user starts a
fresh pass with `/schedule`.

`--scheduler-model`, `--worker-model`, and `--model` override models for the
current owner launch. Model selection and the other interactive controls remain
available in the TUI; active invocations retain their launch-time model.
