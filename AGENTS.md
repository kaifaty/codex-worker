# Agent instructions

codex-worker is a Claude Code plugin. The skill lives in `plugins/codex-worker/skills/codex-worker/`: `SKILL.md`, `references/` and `scripts/`. The repository is public: write commit messages, PR titles and PR descriptions in English.

## Code

- Python 3.9+, standard library only.
- `scripts/codex_task.py` is only the entry point; the code lives in the package `scripts/codex_worker/`. Follow the module rules in its `__init__.py`; `tests/test_module_layout.py` enforces them.
- Tests patch a function on the module that owns it, for example `worker.task_state.save`.
- Every CLI option must be documented in `SKILL.md` or `references/*.md`; `tests/test_options.py` checks this.

## Checks

- The full suite is `python3 -I tests/test_rotation.py`, about 90 s with its default parallelism. Run it at most twice per task.
- Do not set `CODEX_TEST_JOBS=1` and do not time the suite: other agents' tasks load this machine.
- For a behavior-preserving refactor, prove that tests are unchanged with `/usr/bin/python3 -I tests/compare_rotation_assertions.py --base origin/main`, and list every reported change in the PR.

## Safety

- Never run the real `codex`, `claude` or `security`, and never write to the real `~/.codex`, `~/.codex-accounts`, `~/.claude` or `~/.claude-accounts`. Tests use fakes and temporary homes.

## Releases

- Bump `version` in `plugins/codex-worker/.claude-plugin/plugin.json` in every PR that changes the plugin: minor for new behavior, patch for fixes and documentation.
