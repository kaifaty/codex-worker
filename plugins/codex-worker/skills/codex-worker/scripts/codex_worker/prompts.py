"""Scout, review, fix and resume prompt builders."""
import shlex

from . import checking, config, threads


REVIEW_ANGLES = {
    "correctness": "Focus on behavior and acceptance criteria (Done when), including regressions and unmet requirements.",
    "concurrency": "Focus on races, processes, signals, files and state shared by parallel commands. Check repeated runs and recovery after interruption.",
    "security": "Focus on secrets, shell and path injection, untrusted input, permissions and sandbox boundaries.",
    "tests": "Focus on ineffective or weakened tests, uncovered brief requirements, and mocks replacing the code under test.",
}
AUTO_REVIEW_ANGLES = ["correctness", "concurrency", "tests"]


def scout_prompt(question, cfg=None):
    cfg = config.accounts_config() if cfg is None else cfg
    if cfg.get("language", "en") == "ru":
        rules = ("Ты разведчик: изучи код и ответь на вопрос Claude.\n"
                 "Файлы не меняй, ничего не коммить и не пушь.\n"
                 "Ответ — примерно до 300 слов, если вопрос не просит иного; без вступления.\n"
                 "Каждый факт подкрепи ссылкой файл:строка. "
                 "Отдельно отметь, что проверено, а что предположение.\n"
                 "Назови тесты, которые покрывают эту область. Скажи, чего не смотрел.")
    else:
        rules = ("You are a scout: investigate the code and answer Claude's question.\n"
                 "Do not change files, commit, or push.\n"
                 "Keep the answer to about 300 words unless the question asks otherwise; no introduction.\n"
                 "Cite file:line for every fact. Separate verified findings from assumptions.\n"
                 "Name the tests covering this area. State what you did not inspect.")
    return question.rstrip() + "\n\n" + rules


def review_prompt(st, cfg=None):
    cfg = config.accounts_config() if cfg is None else cfg
    brief = config.TASKS / st["slug"] / "brief.md"
    ru = cfg.get("language", "en") == "ru"
    amendments = ""
    if any(message.get("source") == "claude" for message in threads.followup_messages(st)):
        messages = brief.with_name("messages.jsonl")
        amendments = ((f"Поправки к брифу — в файле {messages}. Прочитай его: записи с source `claude` — "
                       "поправки Claude к брифу; более поздние записи `claude` важнее брифа и более ранних поправок. "
                       "Записи с source `review` и `ci` — запросы исправлений от надзирателя, а не требования задачи.\n") if ru else
                      (f"Read the brief amendments at {messages}: entries with source `claude` are Claude's "
                       "amendments to the brief; later `claude` entries take precedence over the brief and earlier "
                       "amendments. Entries with source `review` or `ci` are supervisor fix requests, not task requirements.\n"))
    context = ""
    if any(entry.get("review") is not None for entry in st.get("rounds", [])):
        context += (("Начиная со второго ревью ищи регрессии, внесённые исправлениями, и невыполненные "
                     "пункты «Готово, когда». Новые граничные случаи вне брифа отмечай только как P3.\n") if ru else
                    ("From the second review onward, look for regressions introduced by the fixes and unmet "
                     "acceptance criteria (Done when). Report new edge cases outside the brief only as P3.\n"))
    guarded = checking.guarded_lines(checking.guarded_changes(st))
    if guarded:
        context += "\n".join(guarded) + "\n"
        context += (("Прочитай эти охраняемые файлы целиком и проверь, не ослаблены ли проверки: "
                     "убранные утверждения, пропуски, расширенные допуски, моки вместо проверяемого кода.\n") if ru else
                    ("Read these guarded files in full and check for weakened checks: removed assertions, "
                     "skips, widened tolerances, or mocks replacing the code under test.\n"))
    checks = st.get("checks", [])
    if (checks and st.get("check") == checks[-1]["cmd"] and checks[-1]["outcome"] == "passed"):
        check = checks[-1]
        evidence = f"`{check['cmd']}`: passed (exit {check['exit']}, {check['seconds']:.0f} s).\n"
        context += (("Сохранённая проверка уже прошла: " + evidence +
                     "Не перезапускай весь набор проверок. Запускай только точечные проверки, "
                     "чтобы подтвердить конкретную находку.\n") if ru else
                    ("The saved check already passed: " + evidence +
                     "Do not rerun the full test suite. Run only targeted checks to confirm a specific finding.\n"))
        if check.get("base_check"):
            base = check["base_check"]
            if base["outcome"] == "skipped":
                context += f"Base tests: skipped ({base['reason']}).\n"
            else:
                context += (f"Base tests: {base['outcome']} (exit {base['exit']}, {base['seconds']:.0f} s).\n"
                            + base.get("tail", "") + "\n")
            context += (("Это сигнал для ревью; причиной может быть законная смена поведения.\n") if ru else
                        ("This is review evidence; a legitimate behavior change may explain a failure.\n"))
    rejected = []
    for entry in st.get("rounds", []):
        findings = {item["id"]: item for item in (entry.get("review") or {}).get("findings", [])}
        for decision in entry.get("decisions", []):
            finding = findings.get(decision["id"])
            if decision["decision"] == "rejected" and finding is not None:
                rejected.append(f"{finding['id']} [{finding['severity']}] {finding['file']}:{finding['line']} "
                                f"— {finding['title']}\n{finding['detail']}\nverify: {finding['verify']}\n"
                                + ("Причина отклонения: " if ru else "Rejection reason: ") + decision["note"])
    if rejected:
        context += (("Исполнитель отклонил следующие находки. Не поднимай их снова, если причина верна; "
                     "если поднимаешь — объясни, чем она неверна.\n") if ru else
                    ("The worker rejected these findings. Do not raise them again if the reason is valid; "
                     "if you raise one again, explain why its rejection reason is wrong.\n"))
        context += "\n\n".join(rejected) + "\n"
    if ru:
        return (f"Проведи ревью изменений задачи: git diff $(git merge-base {shlex.quote(st['base'])} HEAD).\n"
                f"Бриф задачи — в файле {brief}. Прочитай его и сверяйся с разделом «Готово, когда».\n"
                + amendments +
                "Ищи только реальные дефекты: неверное поведение, невыполненные пункты брифа, "
                "сломанные или фиктивные проверки, опасные побочные эффекты, гонки, "
                "новый код, который повторяет существующую функцию вместо её вызова и теряет её поведение "
                "(повторы, нормализацию, форматирование).\n"
                "Серьёзность — по реальному триггеру: P1 или P2 только для поведения, достижимого на входах "
                "и состояниях, которые встречаются в этом репозитории, в брифе или при обычном использовании; "
                "дефект, которому нужен гипотетический или синтетический вход, — P3. Лишний объём — тоже дефект: "
                "новые опции, настройки, абстракции, файлы или обобщения, которые не нужны для «Готово, когда». "
                "Ставь P2, если они добавляют интерфейс или поведение, которого не просили, иначе P3, "
                "и укажи, что убрать.\n"
                "Для каждого дефекта укажи файл:строка, что не так и как это проверить. "
                "Без замечаний по стилю.\n"
                + context +
                "Файлы не меняй, ничего не коммить. Ответь JSON по схеме ревью: verdict clean, если "
                "дефектов нет, иначе defects. Находки нумеруй F1, F2 и далее; severity P1/P2/P3, "
                "file, line, title, detail и verify. При clean findings пуст; summary — краткий итог.")
    return (f"Review the task changes: git diff $(git merge-base {shlex.quote(st['base'])} HEAD).\n"
            f"Read the task brief at {brief} and check its acceptance criteria.\n"
            + amendments +
            "Look only for real defects: incorrect behavior, unmet brief requirements, "
            "broken or ineffective checks, dangerous side effects, races, and "
            "new code that duplicates an existing function instead of calling it and loses its behavior "
            "(retries, normalization, formatting).\n"
            "Severity follows a realistic trigger: P1 or P2 only for behavior reachable with inputs or states "
            "that occur in this repository, the brief or normal use; a defect that needs a hypothetical or "
            "synthetic input is P3. Unrequested scope is a defect too: new options, settings, abstractions, "
            'files or generality that "Done when" does not need. Rate it P2 when it adds an interface or '
            "behavior nobody asked for, otherwise P3, and say what to remove.\n"
            "For each defect, give file:line, explain what is wrong and how to verify it. "
            "Do not comment on style.\n"
            + context +
            "Do not change files or create commits. Return JSON matching the review schema: verdict clean "
            "when there are no defects, otherwise defects. Number findings F1, F2, etc.; include severity "
            "P1/P2/P3, file, line, title, detail and verify. For clean, findings is empty; summary is a short recap.")


def fix_prompt(st, review, check, cfg, base_merge=None):
    ru = cfg.get("language", "en") == "ru"
    if base_merge is not None:
        base = base_merge["base"]
        files = "\n".join("- " + path for path in base_merge["files"])
        if ru:
            prompt = (f"Ветка задачи конфликтует с текущей базой {base}.\n"
                      f"Конфликтующие файлы:\n{files}\n\n"
                      f"Слей {base} в ветку задачи обычным merge-коммитом: "
                      f"`git merge --no-ff {shlex.quote(base)}`. Не делай rebase или force-push. "
                      "Разреши конфликты, сохранив смысл изменений обеих сторон.\n")
            if st.get("check"):
                prompt += f"Запусти сохранённую проверку целиком: `{st['check']}`.\n"
            return prompt + ("Закоммить и запушь изменения. Верни итог по схеме работы; "
                             "findings — пустой массив.\n")
        prompt = (f"The task branch conflicts with the current base {base}.\n"
                  f"Conflicting files:\n{files}\n\n"
                  f"Merge {base} into the task branch with an ordinary merge commit: "
                  f"`git merge --no-ff {shlex.quote(base)}`. Do not rebase or force-push. "
                  "Resolve the conflicts while preserving the intent of both sides.\n")
        if st.get("check"):
            prompt += f"Run the saved check in full: `{st['check']}`.\n"
        return prompt + ("Commit and push the changes. Return the work result schema; "
                         "use an empty findings array.\n")
    if check is not None:
        prompt = (("Исправь сбой сохранённой проверки после предыдущего исправления.\n" if ru else
                   "Fix the saved check failure after the previous fix.\n") +
                  f"`{check['cmd']}`: {check['outcome']}, exit {check['exit']}\ncheck.log (40):\n" +
                  check.get("tail", checking.check_tail(st)))
    else:
        prompt = "Проверь находки ревью по коду:\n" if ru else "Check these review findings against the code:\n"
        for finding in review["findings"]:
            prompt += (f"\n{finding['id']} [{finding['severity']}] {finding['file']}:{finding['line']} "
                       f"— {finding['title']}\n{finding['detail']}\nverify: {finding['verify']}\n")
    prompt += (("\n\nПроверь каждую находку по коду. Настоящие дефекты исправь, где разумно — с тестом. "
                "Неверные отклони с причиной в одну строку. Исправляй каждую находку самым маленьким изменением. "
                "Если исправлению нужен новый механизм (новый анализ, опция, абстракция или обработка входов, "
                "которых в этом репозитории не бывает), сузь решение: пусть неподдерживаемый случай падает явно, "
                "или отклони находку с причиной out of scope и вынеси её в remaining. "
                "Находки о лишнем объёме исправляй удалением лишнего кода. Закоммить и запушь изменения. "
                "Верни итог по схеме работы; в findings укажи для каждой находки id, "
                "decision fixed или rejected и note с пояснением. Если находок нет, findings пуст.\n") if ru else
               ("\n\nVerify every finding against the code. Fix real defects, adding a test where reasonable. "
                "Reject incorrect findings with a one-line reason. Fix each finding with the smallest change. "
                "If a fix would need a new mechanism (a new analysis, option, abstraction, or handling of inputs "
                "that do not occur in this repository), narrow instead: make the unsupported case fail loudly, "
                "or reject the finding with the reason out of scope and list it in remaining. "
                "Fix unrequested-scope findings by deleting the extra code. Commit and push the changes. "
                "Return the work result schema; in findings give each finding's id, decision fixed or rejected, "
                "and an explanatory note. When there are no findings, use an empty findings array.\n"))
    return prompt


def reset_resume_prompt(cfg=None):
    cfg = config.accounts_config() if cfg is None else cfg
    if cfg.get("language", "en") == "ru":
        return "Лимит подписки сбросился. Продолжай с того места, где остановился."
    return "The subscription limit has reset. Continue from where you stopped."


def auth_resume_prompt(cfg=None):
    cfg = config.accounts_config() if cfg is None else cfg
    if cfg.get("language", "en") == "ru":
        return "Ход прервался из-за ошибки входа. Продолжай с того места, где остановился."
    return "The turn was interrupted by an authentication error. Continue from where you stopped."


def capacity_resume_prompt(cfg=None):
    cfg = config.accounts_config() if cfg is None else cfg
    if cfg.get("language", "en") == "ru":
        return "Ход прервался из-за перегрузки модели. Продолжай с того места, где остановился."
    return "The turn was interrupted because the model was at capacity. Continue from where you stopped."


def wrapup_prompt(st, reason, cfg):
    ru = cfg.get("language", "en") == "ru"
    reviewing = st.get("kind") == "review"
    if reason["outcome"] == "budget_exhausted":
        if "weekly" in reason:
            prompt = ("Бюджет задачи на недельный лимит исчерпан. " if ru else
                      "The task's weekly limit budget is exhausted. ")
        else:
            prompt = ("Время хода вышло. " if ru else "The turn's time budget has expired. ")
        if st.get("kind") not in ("review", "scout"):
            prompt += ("Закоммить то, что работает, и запушь. " if ru else
                       "Commit what works and push it. ")
        if not reviewing:
            prompt += ("Коротко подведи итог со status partial и списком оставшегося в remaining. " if ru else
                       "Give a short final result with status partial and a list of remaining work in remaining. ")
    else:
        prompt = ((f"Команда {reason['command']!r} упала одинаково {reason['count']} раз "
                   f"(exit_code {reason['exit_code']}, SHA-256 хвоста вывода {reason['output_sha256']}). "
                   "Остановись. Приложи числа и что пробовал. ") if ru else
                  (f"Command {reason['command']!r} failed identically {reason['count']} times "
                   f"(exit_code {reason['exit_code']}, output tail SHA-256 {reason['output_sha256']}). "
                   "Stop. Report numerical evidence and what you tried. "))
        if not reviewing:
            prompt += "Короткий итог со status blocked. " if ru else "Give a short final result with status blocked. "
    prompt += ("На сворачивание не больше 10 минут; не продолжай реализацию и не запускай ревью. " if ru else
               "You have at most 10 minutes to wrap up; do not continue implementation or start a review. ")
    if st.get("kind") in ("review", "scout"):
        prompt += ("Режим только чтения: не меняй файлы, не коммить и не пушь. " if ru else
                   "Stay read-only: do not change files, commit or push. ")
    if reviewing:
        prompt += (("Ответь JSON по схеме ревью: в findings верни находки, найденные к этому моменту; "
                    "verdict defects, если находки есть, иначе clean. В summary отметь, что ревью не завершено.") if ru else
                   ("Return JSON matching the review schema: include findings found so far in findings; "
                    "use verdict defects if any findings exist, otherwise clean. In summary state that the review is incomplete."))
    elif st.get("kind") != "scout":
        prompt += "Ответь JSON по схеме работы." if ru else "Return JSON matching the work result schema."
    return prompt
