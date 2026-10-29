"""Workspace sandbox rules and filter checks."""
import json
import subprocess

from . import config


def unsafe_filter_driver(cwd):
    """Read every config scope without inspecting worker-written file contents."""
    result = subprocess.run(config.GIT + ["config", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
                            cwd=cwd, text=True, capture_output=True, errors="replace")
    if result.returncode == 1:  # No configured filter commands.
        return None
    result.check_returncode()
    # NUL-delimited records preserve embedded newlines in filter commands.
    for record in result.stdout.split("\0"):
        if not record:
            continue
        key, _, command = record.partition("\n")
        if not command.startswith(("git-lfs ", "git lfs ")):
            return key[len("filter."):key.rfind(".")]
    return None


def workspace_filter_driver(st):
    return unsafe_filter_driver(st["worktree"]) if st.get("sandbox") == "workspace" else None


def rules_path(cfg=None):
    override = config.CODEX_HOME / "codex-worker-rules.md"
    if override.is_file():
        return override
    cfg = config.accounts_config() if cfg is None else cfg
    filename = "rules.ru.md" if cfg.get("language", "en") == "ru" else "rules.md"
    return config.SKILL / "references" / filename


def rules_text(cfg=None, no_push=False):
    text = rules_path(cfg).read_text(encoding="utf-8")
    if not no_push:
        return text
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("- Commit as you go."):
            lines[index] = ("- Commit as you go. Keep commits local; do not publish the branch or open or edit PRs. "
                            "Do not merge the PR or deploy anything: Claude handles review and merging.\n")
        elif line.startswith("- Коммить по ходу,"):
            lines[index] = ("- Коммить по ходу локально. Не публикуй ветку, не открывай и не редактируй PR. "
                            "Не мержи PR и ничего не выкатывай: ревью и слияние делает Claude.\n")
    return "".join(lines)


def sandbox_rules(cfg, reviewing=False, no_push=False):
    if cfg.get("language", "en") == "ru":
        rules = ("## Песочница worktree\n\n"
                 "Этот раздел отменяет правила про коммиты и публикацию выше. ")
        rules += (("git commit в песочнице недоступен, не пытайся его запускать. "
                   "Не публикуй ветку, не открывай и не редактируй PR. ") if no_push else
                  ("git commit и git push в песочнице недоступны, не пытайся их запускать. "
                   "Не открывай и не редактируй PR: надзиратель делает это из pr_title/pr_body. "))
        if reviewing:
            return rules + "Файлы не меняй; верни только JSON по схеме ревью."
        rules += ("Оставь изменения в worktree: надзиратель закоммитит их локально после хода. " if no_push else
                  "Оставь изменения в worktree: надзиратель закоммитит и опубликует их после хода. ")
        return rules + ("В итоге дай commit_message по правилам репозитория: первая строка — заголовок. "
                        "Если изменений нет или ты уже закоммитил их сам, commit_message — пустая строка.")
    rules = ("## Workspace sandbox\n\n"
             "This section overrides the commit and publication rules above. ")
    rules += (("git commit is unavailable in the sandbox; do not try it. "
               "Do not publish the branch or open or edit PRs. ") if no_push else
              ("git commit and git push are unavailable in the sandbox; do not try them. "
               "Do not open or edit PRs yourself: the supervisor does it from pr_title/pr_body. "))
    if reviewing:
        return rules + "Do not change files; return only JSON matching the review schema."
    rules += ("Leave changes in the worktree: the supervisor will commit them locally after the turn. " if no_push else
              "Leave changes in the worktree: the supervisor will commit and publish them after the turn. ")
    return rules + ("Return commit_message following the repository's rules; the first line is the subject. "
                    "Use an empty commit_message if there are no changes or you already committed them yourself.")


def sandbox_args(st):
    if st.get("sandbox", "off") != "workspace":
        return []
    # JSON strings are TOML basic strings too; TOML also requires escaping DEL.
    roots = json.dumps(st.get("writable_dirs", []), ensure_ascii=False).replace("\x7f", "\\u007f")
    return ["-c", 'sandbox_mode="workspace-write"',
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", "sandbox_workspace_write.writable_roots=" + roots]
