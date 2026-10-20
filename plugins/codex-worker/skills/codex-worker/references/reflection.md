# Reflection: lessons from task records

`clean` keeps each task's record in `~/.codex/claude-tasks/.archive/<slug>-<time>/`: `brief.md`, `messages.jsonl`
(follow-ups), `state.json` (turns, reviews with findings and decisions, checks), `last.md`, `check.log` and
`stderr.log`. Reflection turns a record into a change to the harness — briefs, `AGENTS.md`, standing rules,
checks — only where the record shows why.

## When

After `clean`, when the task showed a signal:

- a fix cycle that did not converge: `escalate`, the round cap, or two rounds on one point;
- a `say` that corrected Codex's work or approach;
- a failed check, a base-test failure or a CI repair round;
- `blocked`, `needs_decision`, `refused` or `budget_exhausted`;
- a diff much larger than the task needed.

A task that finished `done` with a passing check and at most P3 findings needs no reflection.

## How

1. Read `.archive/INDEX.md` first, for earlier lessons with the same cue.
2. Read the record: the brief, the rounds and review findings with their decisions in `state.json`,
   `messages.jsonl`, `last.md` and the check log. Open the Codex thread only for a moment the record does
   not explain.
3. Write at most one lesson to `EXPERIENCE.md` in the archive entry:

   ```
   cue: <when it applies: the repository or task kind and the observable trigger>
   lesson: <what to do differently, in one or two sentences>
   evidence: <the concrete moment: round, finding id, say, check line>
   scope: invariant | domain
   earlier: <an earlier lesson this task tested>: helped | hurt | no observed effect | confounded
   ```

   Write one `earlier` line for each earlier lesson the task tested, and add a `caveats` line for
   anything that weakens the evidence.

   If the record names no concrete moment, write `NO_ENTRY` and stop. A merged result is not evidence that
   a rule helped. When several changes could explain a difference, the verdict is `confounded`.
4. Append one line to `.archive/INDEX.md`: date, entry, scope, cue, and status (`proposed`,
   `applied <where>` or `declined`).

## Where a lesson goes

- `invariant`, how Codex works in any repository: `~/.codex/AGENTS.md`, or the standing rules
  (`references/rules*.md`) when the supervisor's contract changes.
- `domain`, one repository: its `AGENTS.md`, a skill, a script or a check there.
- A lesson about one brief only goes nowhere.

Prefer a form that enforces over text that asks: a check, a guard or a supervisor stop before a skill, a
skill before always-on text. Every line of `AGENTS.md` is read on every turn.

## Applying

Change the harness only for a lesson whose evidence names the moment. One record is weak evidence: apply
the lesson when its cue recurs in a second record, or at once when a single failure was costly, such as a
fix spiral. Propose global edits (`~/.codex/AGENTS.md`, standing rules) to the user with the evidence
before writing them; repository edits follow that repository's merge rules.

## Measuring a harness change

Replay a recorded task under matched conditions:

- the same brief verbatim, the same base commit, the same flags and reviewer;
- a clone whose `origin` is a local bare repository, so nothing is published; start it with
  `--base origin/<branch>` and without `--ci`;
- in that bare repository, point every branch the brief names, usually `main`, at the base commit and
  delete the others. A brief that compares against `origin/main` otherwise sees today's code.

Compare the replay with the record: file and diff sizes per round, findings by severity, rounds and stop
reason, active minutes and weekly percent (`report --slug`). One replay is one sample: a large difference
is a signal, a small one is noise.
