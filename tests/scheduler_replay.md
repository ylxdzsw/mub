# Real scheduler replay

This opt-in experiment runs the current scheduler policy through real Mu provider
calls and observes its immediate `mub` commands. It is separate from the smoke
suite and incurs provider usage.

The regular test command and CI run only `tests/smoke.py`, which uses fake Mu.
Neither imports or invokes these replay scripts; model calls require an explicit
`scheduler_replay.py run` invocation.

Current production policy retains the original long instructions, examples,
wakeup guidance, and full action replies. Only repeated nested-message session
IDs, internal retry offsets, and handoff revision counters are removed from the
rendered XML. Ongoing comparisons use `copilot/gpt-5.6-luna:high`; other effort
results below are historical experiments.

## Run an experiment

Use a new destination for each command; the harness refuses to replace an existing
experiment directory. From the repository root:

```sh
uv run python tests/scheduler_replay.py recover /root/mote .mu/my-scheduler-lab/corpus
uv run python tests/scheduler_scenarios.py .mu/my-scheduler-lab/corpus .mu/my-scheduler-lab/cases
uv run python tests/scheduler_replay.py run \
  .mu/my-scheduler-lab/cases/browser-scope.json \
  .mu/my-scheduler-lab/runs/luna-browser-scope \
  --model copilot/gpt-5.6-luna:high
```

Replay a failure with the comparison model:

```sh
uv run python tests/scheduler_replay.py run \
  .mu/my-scheduler-lab/cases/omitted-scope.json \
  .mu/my-scheduler-lab/runs/sol-omitted-scope \
  --model codex/gpt-6.1-sol:medium
```

`--timeout` defaults to 240 seconds per scheduler pass. The harness interrupts an
overdue invocation and preserves its error; it does not retry provider failures.
Semantic failures appear in `result.json`; inspect that file rather than treating
the harness process exit code as a test verdict.

The recovery command understands the two JSON snapshot schemas retained in Mote's
scheduler journals. It preserves original snapshots, outputs, journal lines,
timestamps, and hashes; expands unchanged-history references; and identifies
worker journals from visible turns and exact recorded byte boundaries. Empty
active-worker prefixes are resolved through otherwise confirmed board-session
mappings. Unresolved evidence is reported and cannot be silently replayed as empty
history. This is a historical importer, not a general parser for current XML
prompts.

The scenario builder supplies 12 historical cases and 15 explicitly synthetic
cases, including two four-pass handoff sequences. To supply another snapshot,
copy a recovered fixture and edit its `snapshot`, `expectations`, and provenance.
`execution` is recomputed by the current runtime. History requiring a context read
needs an `evidence` entry with a journal path, byte limit, and SHA-256 of that prefix.
The `omitted-scope` scenario demonstrates synthetic canonical journal evidence.

## What is real and what is simulated

Real components:

- Mu model invocation, scheduler policy, XML rendering, and persistent scheduler
  conversation, including history suppression on subsequent passes.
- Production `mub` CLI, Unix socket, pass token, and process-ancestry authentication.
- Runtime operation selection, FIFO admission, writer reservation, holds/recovery
  gates, command validation, and handoff admission/completion logic.
- Fixed-prefix `mub context` reads of copied worker evidence.

Simulated components:

- `_start_operation` records worker launches and occupancy; it never runs worker
  tasks, commits, browsers, or deployments.
- `_workspace` supplies the fixture's Git facts. Calls to it are counted, but it
  does not reconstruct the historical checkout or test real Git races.
- Sequence cases supply scripted worker commit results, then call the production
  handoff-completion method before waking the same scheduler session.

Each run gets a separate Git root under `/tmp/mub-replay-*`. The production
scheduler uses `--no-context`, as it does in normal operation. This is not a Bash
sandbox: tools remain real, and the scheduler is instructed to use them only for
scheduling and necessary readonly evidence. Review `tool-calls.json` for policy
violations. In this experiment every observed call was an allowed scheduling
command or a fixed-prefix context read.

This harness isolates scheduler decisions. It does not establish correctness of
worker execution, live process races, actual commits, or arbitrary filesystem
inspection. Use the existing smoke suite and separate live worker experiments for
those questions.

## Evidence and grading

Each run records:

- `fixture.json`, `snapshot.json`, and `prompt.txt`: provenance, reconstructed
  runtime facts, and the model-facing prompt.
- `result.json`: admission results, attempted commands, virtual launches,
  handoff reservation, model usage, commit ID, source/policy hashes, and expectation
  failures. `elapsed_seconds` measures a scheduler pass from production `_spawn`
  entry through completion/status inspection; it includes Mu startup, provider
  exchanges, tool execution, and owner processing, but excludes fixture setup,
  transcript export, evidence archival, and worker execution. IPC requests also
  record `seconds_since_start` for time to the first scheduling action.
- `tool-calls.json` and `transcript.txt`: actual tool arguments and Mu transcript.
- `journals/`, `objects/`, and `replay.json`: preserved Mu evidence and a fixture
  whose history paths refer to the archived local copies. Replaying it does not
  need the original Mote journals or temporary run root. If moving the directory,
  update its absolute `evidence.*.journal` paths.
- `followup-N/`: per-pass evidence for persistent-session sequences.

Positive expectations require an admitted action; negative expectations also
check rejected attempts. This distinguishes scheduler mistakes caught by runtime
from incorrect admissions. These checks are narrow oracles, not a complete
semantic judge: manually inspect explanations, context reads, and all tool calls.
Existing adequate waiting notes need not be rewritten.

For model comparisons, keep input evidence and expectations fixed, repeat the
failing case with fresh sessions, and report both attempted violations and actual
admissions. Do not compare only final explanations: a model can explain a blocker
correctly after first issuing an invalid command.

## 2026-10-20 experiment

Tested scheduler commit: `63b643d4f5abda60009beee723c354d6f2e0c77d`.
Recovered 282 snapshots from 31 Mote scheduler journals. The selected suite has
21 scenarios; two use four passes each. Including a pilot and repeated comparisons,
42 real scheduler passes ran: 36 Luna and six Sol.

All 12 selected historical cases passed their first Luna replay. Both persistent
handoff sequences passed all four stages. Two synthetic edge cases exposed failures:

| Case | Luna failures | Sol failures | Runtime result |
| --- | ---: | ---: | --- |
| Previously unsuccessful handoff | 3/5 | 0/3 | Invalid retry requests rejected; no worker launched |
| Omitted earlier scope restriction | 5/5 | 0/3 | Luna received writer admission; Sol fetched history and chose readonly |

These are small, deliberately selected samples, not general reliability estimates.
Production policy and model defaults were not changed. One existing smoke
assertion still expected the old opening words; it now checks that a fresh session
receives the current full policy. The other 45 smoke tests passed on the initial
run, and the corrected test passed its focused rerun. A final full run passed
45/46: the concurrency smoke test intermittently missed its expected three-worker
overlap with one-second worker lifetimes. It passed its isolated rerun; this
timing-sensitive validation failure is recorded separately from model failures.

The local experiment is under `.mu/scheduler-lab/`: `REPORT.md` explains the cases,
`assessments.json` contains reviewed grades, `corpus/` holds all recovered snapshots,
and `bad-cases/` contains runnable failure examples and comparison manifests.
The original result files remain unchanged when reviewing an oracle correction;
`assessment.json` records the reviewed verdict separately.

## Structured admission and xhigh follow-up

The subsequent experiment implements structured `execution.reader` and
`execution.writer` objects with `status`/`reason` and optional handoff source.
The policy explicitly disallows probing a reported blocker with `run`; runtime
continues to validate fresh admission independently. History omission and scope
evidence retrieval behavior are unchanged.

Six new cases cover competing writers in one pass, explicit priority over FIFO,
an independent reader during a reserved handoff, unauthorized interrupted
recovery, explicit expansion of scope, and a legitimately authorized short
approval. All six passed their first Luna xhigh run. All 27 unique scenarios
passed their numbered first trial, but repetitions of the known failures exposed
instability:

| Case | High successes | Xhigh successes | High median | Xhigh median |
| --- | ---: | ---: | ---: | ---: |
| Failed handoff | 2/5 | 2/5 | 7.234 s | 11.858 s |
| Omitted scope | 0/5 | 1/5 | 4.447 s | 6.066 s |

There were 51 real passes: 41 xhigh (34 passed) and ten high controls (two passed).
The high controls deliberately contain only the two difficult cases, so their
aggregate success rate is not comparable to the whole xhigh suite. The runs were
interleaved with at most three simultaneous scheduler invocations. Xhigh's
overall median was 6.592 seconds, p90 12.408 seconds. Timings are observed wall
times, not isolated model inference benchmarks. All 46 smoke tests passed.

The follow-up artifacts are under `.mu/scheduler-lab-xhigh/`: `REPORT.md`,
`CASES.md`, `timings.csv` (one row per pass), `summary.json`, source snapshots,
provider journals, and updated `bad-cases/` bundles. The source hash distinguishes
this uncommitted implementation from the earlier experiment at the same Git HEAD.

## Simplified policy and medium comparison

The structured admission experiment was reverted. Current reader/writer fields
are the original descriptive strings. The policy was consolidated, follow-up
event prose removed in favor of the XML events, and successful `run` replies
shortened. The rendered XML omits nested message session IDs and runtime-only
run IDs, revisions, and retry offsets; full trap evidence replaces its duplicated
abbreviated diagnostic. Runtime bookkeeping and admission checks are unchanged.
User requests, history excerpts, and full trap evidence retain their prior limits.

The matched follow-up ran 27 cases at each of medium/high/xhigh, with five trials
per effort for failed handoff and omitted scope: 105 runs, 123 passes. Raw strict
success counts were 28/41, 29/41, and 28/41 respectively; median pass times were
4.571, 6.148, and 6.997 seconds. All three efforts missed the omitted restriction
in 5/5 trials. Medium also retried three browser traps at the same blocking
threshold. This is a smaller contract, not an observed reliability improvement.

Five passes directly inspected the temporary Git checkout, which does not
reconstruct historical file contents. Those passes and one later pass sharing
their evidence are flagged as fixture limitations. Excluding them gives medium
27/39, high 29/40, and xhigh 28/38 strict successes. Do not interpret contradictory
filesystem evidence as a clean model benchmark. Readonly inspection itself is
allowed by the production policy.

Full results and archived failures are under `.mu/scheduler-lab-simple/`:
`REPORT.md`, `CASES.md`, `timings.csv`, `assessments.json`, and `bad-cases/`.
All 46 smoke tests passed after the simplification. Model defaults are unchanged.

## Restored long policy with conservative pruning

After reviewing the high-effort failures, the long policy, examples, event wakeups,
action-result reminders, run IDs, and short trap diagnostics were restored. Brief
diagnostics remain alongside full trap evidence because they may make the blocker
more salient. Only nested messages' repeated session ID, internal retry offsets,
and the handoff revision counter remain pruned; internal runtime state is untouched.

The high-only follow-up ran 51 fresh scenarios / 69 passes, repeating six cases
five times. All 12 historical cases passed. Browser contention, writer contention,
and ownerless dirty work each passed 5/5. An already-failed handoff and omitted
scope both failed 5/5; persistent unsuccessful-handoff sequences passed 3/5.
Total strict passes were 57/69, median 5.008 seconds, p90 9.012 seconds. On the same
41-pass selection as the shortened high experiment, results were 30/41 versus
29/41; this is not evidence of a broad reliability fix. No run inspected the
unreconstructed Git checkout, and all 46 smoke tests passed.

The follow-up artifacts, pruning rationale, and timings are in
`.mu/scheduler-lab-restored/REPORT.md`, `CASES.md`, `timings.csv`, and
`summary.json`. Prior reports and raw results remain preserved.
