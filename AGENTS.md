# Agent instructions

codex-worker runs agents in git worktrees as an installed Python command, with a thin Claude Code plugin.

The package and standing rules live in `src/codex_worker/`, with documentation in `src/codex_worker/docs/`. The plugin contains only `.claude-plugin/plugin.json` and `skills/codex-worker/SKILL.md`. The repository is public: write commit messages, PR titles and PR descriptions in English.

Worker prompts and `src/codex_worker/docs/lead-guide.md` do not name the lead Claude.

## Code

- Python 3.9+, standard library only.
- `board/` is a separate app with its own dependencies (FastAPI, Vue); the worker package never imports it, and the rule above applies to the worker package only.
- `src/codex_worker/__main__.py` is only the entry point; the code lives in the package `src/codex_worker/`. Follow the module rules in its `__init__.py`; `tests/test_module_layout.py` enforces them.
- Tests patch a function on the module that owns it, for example `worker.task_state.save`.
- Every CLI option must be documented in `SKILL.md` or `src/codex_worker/docs/*.md`; `tests/test_options.py` checks this.
- Connect new work executors through `executors.py`.

## Checks

- The full suite is `python3 -I tests/test_rotation.py`, about 90 s with its default parallelism. Run it at most twice per task.
- Do not set `CODEX_TEST_JOBS=1` and do not time the suite: other agents' tasks load this machine.
- For a behavior-preserving refactor, prove that tests are unchanged with `/usr/bin/python3 -I tests/compare_rotation_assertions.py --base origin/main`, and list every reported change in the PR.

## Safety

- Never run the real `codex`, `claude` or `security`, and never write to the real `~/.codex`, `~/.codex-accounts`, `~/.claude` or `~/.claude-accounts`. Tests use fakes and temporary homes.

## Releases

- Bump `__version__` in `src/codex_worker/__init__.py` in every PR that changes the tool or plugin: minor for new behavior, patch for fixes and documentation. `pyproject.toml` reads that version dynamically; keep `plugins/codex-worker/.claude-plugin/plugin.json` equal to it.
