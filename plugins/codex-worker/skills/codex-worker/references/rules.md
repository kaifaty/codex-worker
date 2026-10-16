## How to work

- Work only in this worktree, on branch `{branch}`. Do not touch other branches, worktrees, or repositories. Follow the repository's `AGENTS.md` or `CLAUDE.md`, if present.
- No protocols, manifests, audits, or intermediate reports. Do not create subagents to read reports.
- Run checks for this task only: build and test the affected module, plus one suitable benchmark or end-to-end test. Do not run the full regression suite unless the task explicitly asks for it.
- If a check (test environment, test, or end-to-end test) fails twice after changes, stop and report the numbers and what you tried. Build errors while writing code do not count as checks: fix them and continue. If the task is the test environment or scenario itself, debugging its setup (positions, synchronization, signals) is also part of the work; stop when the actual mechanism fails twice with the setup correct.
- Commit as you go. At the end, push the branch and open a PR against `{base}`. If `gh` cannot see this remote, just push the branch and name it in your report. Do not merge the PR or deploy anything: Claude handles review and merging.
- Do not include Co-Authored-By or "Generated with" lines in commits or PRs.
- Finish with a short report in plain English: what changed, how you checked it (numbers), the PR link, and anything that failed or remains open.
