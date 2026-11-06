"""Keep the lead role independent of the worker and its harness."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_loader import load_worker


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/codex-worker/skills/codex-worker"


class LeadRoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-lead-role-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worker = load_worker({"HOME": str(self.root), "CODEX_TASK_HOME": str(self.root)},
                                  name="lead_role_worker")

    def test_prompts_use_neutral_lead_for_both_executors_and_languages(self):
        worker = self.worker
        brief = self.root / "brief.md"
        brief.write_text("Implement the requested change.\n", encoding="utf-8")
        parser = argparse.ArgumentParser()
        parser.add_argument("slug")
        worker.cli.add_command_options(parser, "start")
        for language in ("en", "ru"):
            for executor_name in ("codex", "claude"):
                for no_push in (False, True):
                    with self.subTest(language=language, executor=executor_name, no_push=no_push):
                        cfg = dict(worker.config.accounts_config(), language=language)
                        executor = worker.executors.get(executor_name)
                        slug = "%s-%s-%s" % (language, executor_name, "local" if no_push else "push")
                        args = parser.parse_args([
                            slug, "--brief", str(brief), "--repo", str(self.root),
                            "--base", "origin/main", "--executor", executor_name,
                        ] + (["--no-push"] if no_push else []))
                        with mock.patch.object(worker.config, "accounts_config", return_value=cfg), \
                                mock.patch.object(executor, "prepare"), \
                                mock.patch.object(worker.commands, "main_repo", return_value=self.root), \
                                mock.patch.object(worker.task_state, "running_tasks", return_value=[]), \
                                mock.patch.object(worker.procs, "sh", return_value=""), \
                                mock.patch.object(worker.supervisor, "spawn"), \
                                contextlib.redirect_stdout(io.StringIO()):
                            worker.commands.cmd_start(args)
                            st = worker.task_state.load(slug)
                            task = worker.config.TASKS / slug
                            rules = SKILL / "references" / ("rules.ru.md" if language == "ru" else "rules.md")
                            self.assertEqual(worker.sandboxing.rules_path(cfg), rules)
                            work = (task / "brief.md").read_text(encoding="utf-8")
                            self.assertIn("ведущий" if language == "ru" else "the lead", work)
                            st[executor.cursor_field] = "previous-session"
                            prompts = {
                                "work": work,
                                "scout": worker.prompts.scout_prompt("Where is the entry point?", cfg),
                                "handoff": worker.threads.handoff(st, cfg=cfg),
                                "followup": worker.threads.followup_prompt(st, "Continue the task.", cfg),
                                "followup_handoff": worker.threads.followup_prompt(
                                    dict(st, **{executor.cursor_field: ""}), "Continue the task.", cfg),
                                "wrapup": worker.prompts.wrapup_prompt(
                                    st, {"outcome": "budget_exhausted", "minutes": 1}, cfg),
                                "review": worker.prompts.review_prompt(st, cfg),
                            }
                            for reviewing in (False, True):
                                prompts["sandbox-%s" % reviewing] = worker.sandboxing.sandbox_rules(
                                    cfg, reviewing=reviewing, no_push=no_push)
                            for name, prompt in prompts.items():
                                with self.subTest(prompt=name):
                                    self.assertNotRegex(prompt, r"\bClaude\b")

    def test_lead_amendments_read_old_and_new_sources_without_rewriting(self):
        worker = self.worker
        task = worker.config.TASKS / "amendments"
        task.mkdir(parents=True)
        st = {"slug": "amendments", "base": "origin/main", "thread": "existing-thread"}
        messages = task / "messages.jsonl"
        for sources in (("claude",), ("lead",), ("claude", "lead"), ("review", "ci")):
            original = "".join(json.dumps({"source": source, "text": "Amend the brief."}) + "\n"
                               for source in sources)
            messages.write_text(original, encoding="utf-8")
            for language in ("en", "ru"):
                with self.subTest(sources=sources, language=language):
                    with mock.patch.object(worker.checking, "guarded_changes", return_value=[]):
                        prompt = worker.prompts.review_prompt(st, {"language": language})
                    amendment = "Поправки к брифу" if language == "ru" else "Read the brief amendments"
                    self.assertEqual(amendment in prompt, bool(set(sources) & {"lead", "claude"}))
                    self.assertEqual(messages.read_text(encoding="utf-8"), original)
        worker.threads.followup_prompt(st, "Use the new requirement.", {})
        self.assertEqual(worker.threads.followup_messages(st)[-1]["source"], "lead")

    def test_lead_documents_and_relative_links(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        guide = (SKILL / "references/lead-guide.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(skill.splitlines()), 45)
        self.assertNotIn("CLAUDE_SKILL_DIR", guide)
        self.assertNotIn("/code-review", guide)
        paths = [ROOT / "README.md", SKILL / "SKILL.md"] + sorted((SKILL / "references").glob("*.md"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]\n]+\]\(([^)\s]+)\)", text):
                link = urlsplit(target)
                if link.scheme or link.netloc:
                    continue
                destination = path.parent / unquote(link.path) if link.path else path
                with self.subTest(path=str(path.relative_to(ROOT)), target=target):
                    self.assertTrue(destination.is_file(), "Missing file: " + str(destination))
                    if link.fragment:
                        headings = []
                        fenced = False
                        for line in destination.read_text(encoding="utf-8").splitlines():
                            if line.startswith(("```", "~~~")):
                                fenced = not fenced
                            heading = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", line) if not fenced else None
                            if heading:
                                slug = re.sub(r"[^\w -]", "", heading[1].lower()).replace(" ", "-")
                                anchor = slug
                                suffix = 0
                                while anchor in headings:
                                    suffix += 1
                                    anchor = slug + "-" + str(suffix)
                                headings.append(anchor)
                        self.assertIn(unquote(link.fragment), headings, "Missing heading in " + str(destination))


if __name__ == "__main__":
    unittest.main()
