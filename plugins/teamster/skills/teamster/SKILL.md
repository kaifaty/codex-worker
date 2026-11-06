---
name: teamster
description: Use teamster to delegate implementation work to Codex (OpenAI's coding agent, using the user's ChatGPT plan) and stay in control of it - Claude plans, writes the brief, reviews the result and merges; Codex writes the code in its own git worktree. Use it to save the Claude subscription on any sizeable, well-specified coding task (a feature step, a fix with a clear "done when", engine/C++ work, benchmarks, mechanical changes) that the user's or the repository's instructions do not keep for Claude, when the user says to give work to Codex, and to steer or check a Codex thread already open in the Codex desktop app - also in Russian (поручи кодексу, отдай codex, пусть кодекс сделает, делегируй, кодекс исполнитель, проверь кодекс, что там у кодекса).
---

# Teamster

The lead plans, writes the brief, reviews the result and merges. The worker implements
in its own git worktree; Codex and Claude are available as workers and reviewers.
Claude Code is a supported lead.

First run `teamster --version`; version 1.51.0 or newer is required.
If the command is missing or older, tell the user to run
`uv tool install git+https://github.com/kaifaty/codex-worker` or
`uv tool upgrade teamster`, respectively. Do not install or upgrade it yourself.

Before the first task in a session, read `teamster docs lead-guide`.
It contains delegation rules, brief structure, commands, budgets, the work loop,
stop rules and links to the detailed references.

In Claude Code:

- Run `teamster wait <slug>` as a background Bash command, which reports
  completion when the whole review/fix cycle ends.
- Write briefs in the session scratchpad, outside the repository, so they cannot
  enter a checkout commit. Saved check scripts must live in a stable directory.
- Use `/code-review` for big or risky PRs if available, following the repository's rules.
