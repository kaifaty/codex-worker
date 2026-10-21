"""Workspace, pull request and CI tests."""
import builtins
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import traceback
import unittest
from pathlib import Path
from unittest import mock

from rotation_support import PYTHON, SCRIPT, SKILL, RotationTestCase


class WorkspaceTests(RotationTestCase):
    def gh_writes(self):
        return [event for event in self.gh_invocations()
                if event["argv"][:2] in (["pr", "create"], ["pr", "edit"])]

    def configure_ci(self, snapshots, jobs=None, head_sha=None):
        path = self.root / "fake-gh-ci.json"
        value = {"checks": snapshots, "jobs": jobs or {}}
        if head_sha is not None:
            value["head_sha"] = head_sha
        path.write_text(json.dumps(value))
        self.env.update(CODEX_FAKE_GH_CI=str(path), CODEX_TASK_CI_POLL_SECONDS="0.02")
        return path

    def ci_check(self, bucket="pass", name="test (macos-latest, 3.9)", job="123"):
        states = {"pending": "IN_PROGRESS", "pass": "SUCCESS", "fail": "FAILURE",
                  "skipping": "SKIPPED", "cancel": "CANCELLED"}
        return {"name": name, "state": states[bucket], "bucket": bucket,
                "link": "https://github.com/fixture/repo/actions/runs/42/job/" + job}

    def ci_work(self, contents="implemented\n", **changes):
        return {"mode": "complete", "write_files": {"tracked.txt": contents},
                "reply": self.work_result(commit_message="Implement CI fixture",
                                          pr_title="Wait for pull request CI",
                                          pr_body="Wait for all pull request checks.", **changes)}

    def ci_polls(self):
        return [event for event in self.gh_invocations() if event["argv"][:2] == ["pr", "checks"]]

    def ci_unittest_failure_fixture(self):
        prefix = "test (ubuntu-latest, 3.9)\tRun tests\t2026-10-19T12:00:00.0000000Z "
        failure = [prefix + text for text in (
            "=" * 70,
            "FAIL: test_compatibility (tests.test_example.CompatibilityTests)",
            "-" * 70,
            "Traceback (most recent call last):",
            '  File "tests/test_example.py", line 12, in test_compatibility',
            "    self.assertEqual(actual, expected)",
            "AssertionError: Python 3.9 compatibility")]
        summary = [prefix + "Ran 301 tests in 0.123s", prefix + "FAILED (failures=1)"]
        progress = [prefix + "test_success_%03d (tests.test_example.Tests) ... ok" % index
                    for index in range(300)]
        lines = (progress[:10] + failure + [prefix] + progress[10:] +
                 [prefix + "-" * 70, summary[0], prefix, summary[1]])
        return "\n".join(lines), failure, summary, progress

    def ci_failure_blocks_fixture(self, count, job="fixture"):
        blocks = [["=" * 70, "FAIL: test_%03d (%s.Tests)" % (index, job),
                   "-" * 70, "AssertionError: %s failure %03d" % (job, index)]
                  for index in range(count)]
        return "\n\n".join("\n".join(block) for block in blocks), blocks

    def ci_adjacent_tracebacks_fixture(self, count=60):
        buffer, blocks = io.StringIO(), []
        for index in range(count):
            start = buffer.tell()
            try:
                raise ValueError("fixture traceback %03d" % index)
            except ValueError:
                traceback.print_exc(file=buffer)
            blocks.append(buffer.getvalue()[start:].splitlines())
        return buffer.getvalue(), blocks

    def test_workspace_settings_are_normalized_saved_and_inherited_by_say_and_review(self):
        user_home = self.root / "user-home"
        user_home.mkdir()
        self.env["HOME"] = str(user_home)
        cache = '.cache/quote"-slash\\-line\n-tab\t-del\x7f-🙂'
        roots = [str(user_home / cache), str(self.repo / "build-cache")]
        self.configure(language="en", sandbox="workspace", writable_dirs=["~/" + cache, "build-cache"])
        settings = self.home / "codex-worker.json"
        settings.write_text((self.home / "claude-accounts.json").read_text())
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)},
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("sandbox-inherit", "--review")
        output = self.wait("sandbox-inherit")
        state = self.state("sandbox-inherit")
        self.assertEqual((state["sandbox"], state["writable_dirs"]), ("workspace", roots))
        self.assertIn("== sandbox: workspace", output)
        for args in (("status",), ("status", "sandbox-inherit")):
            self.assertIn("sandbox workspace", self.cli(*args))
        config = json.loads(settings.read_text())
        config.update(sandbox="off", writable_dirs=[])
        settings.write_text(json.dumps(config))
        self.cli("say", "sandbox-inherit", "Continue with the task's saved sandbox")
        self.wait("sandbox-inherit")
        self.cli("review", "sandbox-inherit")
        self.wait("sandbox-inherit")
        executions = self.invocations()
        self.assertEqual([event["resume"] for event in executions], [False, False, True, False])
        self.assertEqual([event["review"] for event in executions], [False, True, False, True])
        self.assertEqual(self.state("sandbox-inherit")["writable_dirs"], roots)
        for event in executions:
            self.assert_workspace_sandbox(event, roots)
            self.assertIn("## Workspace sandbox", event["prompt"])
            if event["review"]:
                self.assertNotIn("commit_message", event["prompt"])
                self.assertNotIn("commit_message", event["output_schema"]["properties"])
        self.start("sandbox-override", "--sandbox", "workspace")
        self.wait("sandbox-override")
        self.assertEqual(self.state("sandbox-override")["sandbox"], "workspace")
        self.assert_workspace_sandbox(self.invocations()[-1])

    def test_workspace_settings_validate_values_and_default_to_off(self):
        worker = self.worker()
        config = worker.config.accounts_config()
        self.assertEqual(config["sandbox"], "off")
        self.assertEqual(config["writable_dirs"], [])
        invalid = {"sandbox": (None, False, "workspace-write"),
                   "writable_dirs": (None, "cache", [None], [""])}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    (self.home / "codex-worker.json").write_text(json.dumps({key: value}))
                    with self.assertRaises(SystemExit) as raised:
                        worker.config.accounts_config()
                    self.assertIn(key, str(raised.exception))

    def test_workspace_rules_are_localized_and_off_has_no_sandbox_flags_or_section(self):
        for language, heading in (("en", "## Workspace sandbox"), ("ru", "## Песочница worktree")):
            with self.subTest(language=language):
                self.configure(language=language, sandbox="workspace")
                self.start("sandbox-rules-" + language)
                self.wait("sandbox-rules-" + language)
                prompt = self.invocations()[-1]["prompt"]
                self.assertIn(heading, prompt)
                for text in ("git commit", "git push", "commit_message", "pr_title", "pr_body", "worktree"):
                    self.assertIn(text, prompt[prompt.index(heading):])
                self.assertNotIn("gh pr create", prompt)
                self.start("sandbox-off-" + language, "--sandbox", "off")
                output = self.wait("sandbox-off-" + language)
                self.assert_no_workspace_sandbox(self.invocations()[-1])
                self.assertNotIn(heading, self.invocations()[-1]["prompt"])
                self.assertNotIn("== sandbox:", output)
                self.assertEqual(self.state("sandbox-off-" + language)["sandbox"], "off")
        self.configure(language="en")
        self.start("sandbox-default-off")
        self.wait("sandbox-default-off")
        self.assert_no_workspace_sandbox(self.invocations()[-1])
        self.assertIn("sandbox off", self.cli("status", "sandbox-default-off"))

    def test_pr_supervisor_creates_from_result_and_wait_uses_receipt_url(self):
        self.configure(language="en")
        self.env["GH_REPO"] = "unrelated/host-repository"
        result = self.work_result(pr_title='Publish the worker\'s "complete" change',
                                  pr_body="\nDescribe the whole task.\n\nKeep this exact body.\n",
                                  pr="https://example.test/pull/stale-model-url")
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "implemented\n"},
            "commit_files": ["tracked.txt"], "push": True, "reply": result}])
        self.start("pr-create")
        output = self.wait("pr-create")
        state = self.state("pr-create")
        self.assertEqual(state["outcome"], "completed")
        self.assert_output_schema(self.invocations()[0], "pr-create")
        self.assertEqual([event["argv"][:2] for event in self.gh_invocations()],
                         [["repo", "view"], ["pr", "list"], ["pr", "create"]])
        origin = str(self.root / "origin.git")
        self.assertEqual(self.gh_invocations()[0]["argv"][2], origin)
        for event in self.gh_invocations()[1:]:
            self.assertEqual(event["argv"][event["argv"].index("--repo") + 1], origin)
        create, = self.gh_writes()
        args = create["argv"]
        self.assertEqual(args[args.index("--base") + 1], "main")
        self.assertEqual(args[args.index("--head") + 1], state["branch"])
        self.assertEqual(args[args.index("--title") + 1], result["pr_title"])
        self.assertEqual(create["body"], result["pr_body"])
        publication = state["publish"]
        self.assertEqual(publication["sha"], "", "Codex owns the ordinary turn's commit")
        self.assertTrue(publication["pushed"])
        self.assertEqual((publication["action"], publication["error"]), ("created", ""))
        self.assertIn("== PR created " + publication["pr"], output)
        self.assertIn("PR: " + publication["pr"], output)
        self.assertNotIn(result["pr"], output)

    def test_pr_supervisor_resolves_github_ssh_aliases_and_preserves_other_remotes(self):
        self.configure(language="en")
        _ = self.repo
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        # Keep fixture transport local while exposing the requested publication URL.
        fake_git = self.root / "git"
        fake_git.write_text(
            "#!" + PYTHON + "\n"
            "import os, sys\n"
            "if sys.argv[-3:] == ['remote', 'get-url', 'origin']:\n"
            "    print(os.environ['CODEX_FAKE_REMOTE_URL'])\n"
            "else:\n"
            "    os.execv(" + repr(real_git) + ", [" + repr(real_git) + "] + sys.argv[1:])\n")
        fake_git.chmod(0o700)
        ssh_trace = self.root / "ssh-trace.jsonl"
        fake_ssh = self.root / "ssh"
        fake_ssh.write_text(
            "#!" + PYTHON + "\n"
            "import json, os, sys\n"
            "with open(" + repr(str(ssh_trace)) + ", 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "assert len(sys.argv) == 3 and sys.argv[1] == '-G'\n"
            "print('user git\\nhostname ' + os.environ['CODEX_FAKE_SSH_HOSTNAME'])\n")
        fake_ssh.chmod(0o700)
        cases = (
            ("scp-alias", "git@github-alias:owner/repo.git", "github.com", "github.com/owner/repo",
             ["-G", "github-alias"]),
            ("url-alias", "ssh://git@github-alias:2222/owner/repo.git", "github.com", "github.com/owner/repo",
             ["-G", "github-alias"]),
            ("scp-github", "git@github.com:owner/repo.git", "github.com", "git@github.com:owner/repo.git", None),
            ("url-github", "ssh://git@github.com/owner/repo.git", "github.com", "ssh://git@github.com/owner/repo.git", None),
            ("other-host", "git@forgejo-alias:owner/repo.git", "forgejo.example", "git@forgejo-alias:owner/repo.git",
             ["-G", "forgejo-alias"]),
        )
        for name, remote, hostname, expected, ssh_args in cases:
            with self.subTest(remote=remote):
                (self.home / "exec-count").unlink(missing_ok=True)
                ssh_trace.unlink(missing_ok=True)
                self.env.update(CODEX_FAKE_REMOTE_URL=remote, CODEX_FAKE_SSH_HOSTNAME=hostname)
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {"tracked.txt": name + "\n"},
                    "commit_files": ["tracked.txt"], "push": True,
                    "reply": self.work_result(pr_title="Publish through SSH aliases", pr_body="Publish this change.")}])
                before = len(self.gh_invocations())
                slug = "pr-ssh-" + name
                self.start(slug)
                self.wait(slug)
                publication = self.state(slug)["publish"]
                self.assertEqual((publication["action"], publication["error"]), ("created", ""))
                self.assertTrue(publication["pr"])
                calls = self.gh_invocations()[before:]
                self.assertEqual([event["argv"][:2] for event in calls],
                                 [["repo", "view"], ["pr", "list"], ["pr", "create"]])
                self.assertEqual(calls[0]["argv"][2], expected)
                for event in calls[1:]:
                    self.assertEqual(event["argv"][event["argv"].index("--repo") + 1], expected)
                ssh_calls = [json.loads(line) for line in ssh_trace.read_text().splitlines()] if ssh_trace.exists() else []
                self.assertEqual(ssh_calls, [ssh_args] if ssh_args else [])

    def test_pr_supervisor_ignores_fork_prs_with_the_same_branch_name(self):
        self.configure(language="en")
        for own_state in ("OPEN", "CLOSED", "MERGED", None):
            with self.subTest(own_state=own_state):
                (self.home / "exec-count").unlink(missing_ok=True)
                slug = "pr-head-repo-" + (own_state or "missing").lower()
                own_url = "https://example.test/pull/42"
                foreign_prs = [{"url": "https://example.test/pull/99", "state": "OPEN",
                                "headRepository": {"id": "R_other", "name": "repo", "nameWithOwner": ""},
                                "headRepositoryOwner": {"login": "other"}},
                               {"url": "https://example.test/pull/98", "state": "OPEN",
                                "headRepository": {"id": "R_another", "name": "another-repo", "nameWithOwner": ""},
                                "headRepositoryOwner": {"login": "fixture"}},
                               {"url": "https://example.test/pull/97", "state": "OPEN",
                                "headRepository": None, "headRepositoryOwner": None},
                               {"url": "https://example.test/pull/96", "state": "OPEN",
                                "headRepository": {"id": "R_no_owner", "name": "repo", "nameWithOwner": ""},
                                "headRepositoryOwner": None}]
                prs = list(foreign_prs)
                if own_state:
                    prs.append({"url": own_url, "state": own_state,
                                "headRepository": {"id": "R_fixture", "name": "Repo", "nameWithOwner": ""},
                                "headRepositoryOwner": {"login": "Fixture"}})
                Path(self.env["CODEX_FAKE_GH_STATE"]).write_text(json.dumps({"codex/" + slug: prs}))
                before = len(self.gh_writes())
                result = self.work_result(pr_title="Publish our task", pr_body="Describe our complete PR.")
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {"tracked.txt": slug + "\n"},
                    "commit_files": ["tracked.txt"], "push": True, "reply": result}])
                self.start(slug)
                self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                publication = state["publish"]
                writes = self.gh_writes()[before:]
                if own_state == "OPEN":
                    self.assertEqual((publication["action"], publication["pr"]), ("updated", own_url))
                    edit, = writes
                    self.assertEqual(edit["argv"][:3], ["pr", "edit", own_url])
                    self.assertEqual(edit["body"], result["pr_body"])
                elif own_state:
                    self.assertEqual((publication["action"], publication["pr"]), ("skipped", own_url))
                    self.assertEqual(publication["error"], "pr " + own_state.lower())
                    self.assertFalse(writes)
                else:
                    self.assertEqual(publication["action"], "created")
                    create, = writes
                    self.assertEqual(create["argv"][:2], ["pr", "create"])
                    self.assertEqual(create["body"], result["pr_body"])
                for foreign in foreign_prs:
                    self.assertFalse(any(foreign["url"] in event["argv"] for event in writes))

    def test_pr_supervisor_fix_round_updates_only_with_a_nonempty_body(self):
        self.configure(language="en")
        for body in ("Describe the entire PR after fixing the defect.\n", ""):
            with self.subTest(body=body):
                (self.home / "exec-count").unlink(missing_ok=True)
                slug = "pr-fix-update" if body else "pr-fix-unchanged"
                before = len(self.gh_writes())
                decisions = [{"id": finding["id"], "decision": "fixed", "note": "Fixed the defect."}
                             for finding in self.review_result()["findings"]]
                fix_result = self.work_result(pr_title="Describe all completed work", pr_body=body,
                                              findings=decisions)
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "write_files": {"tracked.txt": "first implementation\n"},
                     "commit_files": ["tracked.txt"], "push": True,
                     "reply": self.work_result(pr_title="Implement the whole task", pr_body="Initial PR body.")},
                    {"mode": "complete", "reply": self.review_result()},
                    {"mode": "complete", "write_files": {"tracked.txt": "corrected implementation\n"},
                     "commit_files": ["tracked.txt"], "push": True, "reply": fix_result},
                    {"mode": "complete", "reply": self.review_result(False)}])
                self.start(slug, "--review", "--fix-rounds", "1")
                output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["auto_fix_stop"], "clean")
                self.assertEqual(len(state["rounds"]), 1)
                writes = self.gh_writes()[before:]
                expected = [["pr", "create"], ["pr", "edit"]] if body else [["pr", "create"]]
                self.assertEqual([event["argv"][:2] for event in writes], expected)
                self.assertEqual(state["publish"]["action"], "updated" if body else "unchanged")
                self.assertEqual(state["publish"]["error"], "")
                if body:
                    edit = writes[-1]
                    self.assertIn(state["publish"]["pr"], edit["argv"])
                    self.assertEqual(edit["argv"][edit["argv"].index("--title") + 1], fix_result["pr_title"])
                    self.assertEqual(edit["body"], body)
                    self.assertIn("== PR updated " + state["publish"]["pr"], output)
                self.assertIn("PR: " + state["publish"]["pr"], output)

    def test_pr_supervisor_edit_failure_keeps_url_and_does_not_fail_the_turn(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "initial PR implementation\n"},
             "commit_files": ["tracked.txt"], "push": True,
             "reply": self.work_result(pr_title="Implement the whole task", pr_body="Initial complete description.")},
            {"mode": "complete", "write_files": {"tracked.txt": "updated PR implementation\n"},
             "commit_files": ["tracked.txt"], "push": True,
             "reply": self.work_result(pr_title="Update the whole task", pr_body="Updated complete description.")}])
        self.start("pr-edit-error")
        self.wait("pr-edit-error")
        url = self.state("pr-edit-error")["publish"]["pr"]
        self.env["CODEX_FAKE_GH_EDIT_ERROR"] = "1"
        self.cli("say", "pr-edit-error", "Publish the updated task description")
        output = self.wait("pr-edit-error")
        state = self.state("pr-edit-error")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["publish"]["action"], "skipped")
        self.assertEqual(state["publish"]["pr"], url)
        self.assertIn("fixture PR edit failed", state["publish"]["error"])
        self.assertIn("== PR skipped: ", output)
        self.assertIn("PR: " + url, output)
        self.assertEqual([event["argv"][:2] for event in self.gh_writes()],
                         [["pr", "create"], ["pr", "edit"]])

    def test_pr_supervisor_empty_fields_use_commit_subject_and_result_summary(self):
        self.configure(language="en")
        result = self.work_result(summary="The complete task summary.\nPreserve its final newline.\n")
        subject = "Use the last commit subject as the PR title"
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "fallback implementation\n"},
            "commit_files": ["tracked.txt"], "commit_subject": subject, "push": True, "reply": result}])
        self.start("pr-fallback")
        self.wait("pr-fallback")
        create, = self.gh_writes()
        self.assertEqual(create["argv"][create["argv"].index("--title") + 1], subject)
        self.assertEqual(create["body"], result["summary"])

    def test_pr_supervisor_requires_origin_branch_to_match_local_head(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "not pushed yet\n"},
             "commit_files": ["tracked.txt"], "reply": self.work_result(pr_body="Do not publish yet.")},
            {"mode": "complete", "push": True, "reply": self.work_result()},
            {"mode": "complete", "write_files": {"tracked.txt": "ahead of origin\n"},
             "commit_files": ["tracked.txt"], "reply": self.work_result(pr_body="Still not pushed.")}])
        self.start("pr-not-pushed")
        output = self.wait("pr-not-pushed")
        publication = self.state("pr-not-pushed")["publish"]
        self.assertEqual(publication["action"], "skipped")
        self.assertFalse(publication["pushed"])
        self.assertIn("not pushed", publication["error"])
        self.assertIn("== PR skipped: not pushed", output)
        self.assertFalse(self.gh_invocations(), "missing origin branch must stop before gh")
        self.cli("say", "pr-not-pushed", "Push the completed branch")
        self.wait("pr-not-pushed")
        self.assertEqual(self.state("pr-not-pushed")["publish"]["action"], "created")
        calls = list(self.gh_invocations())
        self.cli("say", "pr-not-pushed", "Make another local change")
        output = self.wait("pr-not-pushed")
        state = self.state("pr-not-pushed")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["publish"]["action"], "skipped")
        self.assertIn("not pushed", state["publish"]["error"])
        self.assertIn("== PR skipped: not pushed", output)
        self.assertEqual(self.gh_invocations(), calls, "a stale remote SHA must stop before gh")

    def test_pr_supervisor_missing_gh_skips_without_changing_turn_outcome(self):
        self.configure(language="en")
        # Restrict PATH so the host's installed gh can never run in this case.
        bin_dir = self.root / "without-gh"
        bin_dir.mkdir()
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        (bin_dir / "git").symlink_to(real_git)
        self.env["PATH"] = str(bin_dir)
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "pushed without gh\n"},
            "commit_files": ["tracked.txt"], "push": True, "reply": self.work_result()}])
        self.start("pr-no-gh")
        output = self.wait("pr-no-gh")
        state = self.state("pr-no-gh")
        self.assertEqual(state["outcome"], "completed")
        self.assertTrue(state["publish"]["pushed"])
        self.assertEqual(state["publish"]["action"], "skipped")
        self.assertIn("gh unavailable", state["publish"]["error"])
        self.assertIn("== PR skipped: gh unavailable", output)
        self.assertFalse(self.gh_invocations())

    def test_pr_supervisor_inaccessible_repo_skips_without_changing_turn_outcome(self):
        self.configure(language="en")
        self.env["CODEX_FAKE_GH_REPO_ERROR"] = "1"
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "pushed to inaccessible remote\n"},
            "commit_files": ["tracked.txt"], "push": True, "reply": self.work_result()}])
        self.start("pr-no-repo")
        output = self.wait("pr-no-repo")
        state = self.state("pr-no-repo")
        self.assertEqual(state["outcome"], "completed")
        self.assertTrue(state["publish"]["pushed"])
        self.assertEqual(state["publish"]["action"], "skipped")
        self.assertIn("gh repo view", state["publish"]["error"])
        self.assertIn("fixture remote is not accessible", state["publish"]["error"])
        self.assertIn("== PR skipped: ", output)
        self.assertEqual([event["argv"][:2] for event in self.gh_invocations()], [["repo", "view"]])

    def test_pr_supervisor_closed_and_merged_prs_are_preserved(self):
        self.configure(language="en")
        for pr_state in ("CLOSED", "MERGED"):
            with self.subTest(pr_state=pr_state):
                (self.home / "exec-count").unlink(missing_ok=True)
                slug = "pr-" + pr_state.lower()
                url = "https://example.test/pull/" + pr_state.lower()
                Path(self.env["CODEX_FAKE_GH_STATE"]).write_text(json.dumps({
                    "codex/" + slug: {"url": url, "state": pr_state}}))
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {"tracked.txt": pr_state + " implementation\n"},
                    "commit_files": ["tracked.txt"], "push": True,
                    "reply": self.work_result(pr_title="Do not reopen this PR", pr_body="Do not edit this PR.")}])
                self.start(slug)
                output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertEqual((state["publish"]["pr"], state["publish"]["action"]), (url, "skipped"))
                self.assertEqual(state["publish"]["error"], "pr " + pr_state.lower())
                self.assertIn("== PR skipped: pr " + pr_state.lower(), output)
                self.assertIn("PR: " + url, output)
                self.assertFalse(self.gh_writes())

    def test_pr_supervisor_successful_wrapup_publishes_in_both_sandbox_modes(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        for sandbox in ("off", "workspace"):
            with self.subTest(sandbox=sandbox):
                self.configure(language="en", sandbox=sandbox)
                (self.home / "exec-count").unlink(missing_ok=True)
                before = len(self.gh_writes())
                result = self.work_result("partial", pr_title="Publish the saved checkpoint",
                                          pr_body="Describe the whole PR at the saved checkpoint.\n",
                                          commit_message="Save the bounded checkpoint" if sandbox == "workspace" else "")
                wrapup = {"mode": "complete", "write_files": {"tracked.txt": "saved checkpoint\n"},
                          "reply": result}
                if sandbox == "off":
                    wrapup.update(commit_files=["tracked.txt"], push=True)
                self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}, wrapup])
                slug = "pr-wrapup-" + sandbox
                self.start(slug, "--max-minutes", "10")
                output = self.wait(slug, timeout=15)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "budget_exhausted")
                self.assertEqual(state["wrapup"]["outcome"], "completed")
                self.assertEqual(state["result"], result)
                self.assertEqual(state["publish"]["action"], "created")
                self.assertEqual(state["publish"]["error"], "")
                create, = self.gh_writes()[before:]
                self.assertEqual(create["argv"][:2], ["pr", "create"])
                self.assertEqual(create["argv"][create["argv"].index("--title") + 1], result["pr_title"])
                self.assertEqual(create["body"], result["pr_body"])
                self.assertIn("== PR created " + state["publish"]["pr"], output)

    def test_pr_rules_delegate_publication_and_forbid_description_attribution(self):
        rules = (SKILL / "references" / "rules.md").read_text()
        self.assertIn("push the branch; do not open or edit PRs yourself", rules)
        self.assertIn("pr_title", rules)
        self.assertIn("pr_body", rules)
        attribution = next(line for line in rules.splitlines() if "Co-Authored-By" in line)
        self.assertIn("Generated with", attribution)
        self.assertIn("pr_body", attribution)
        self.assertNotIn("gh", rules)

    def test_pr_standalone_review_preserves_receipt_without_replaying_publication(self):
        for sandbox in ("off", "workspace"):
            with self.subTest(sandbox=sandbox):
                self.configure(language="en", sandbox=sandbox)
                (self.home / "exec-count").unlink(missing_ok=True)
                slug = "pr-review-receipt-" + sandbox
                work = {"mode": "complete", "write_files": {"tracked.txt": slug + "\n"},
                        "reply": self.work_result(pr_title="Keep the published task visible",
                                                  pr_body="Describe the complete task.")}
                if sandbox == "off":
                    work.update(commit_files=["tracked.txt"], push=True)
                self.account(self.home, "A", self.reset, runs=[
                    work, {"mode": "complete", "reply": self.review_result(False)}])
                self.start(slug)
                work_output = self.wait(slug)
                publication = self.state(slug)["publish"]
                url = publication["pr"]
                self.assertEqual(publication["action"], "created")
                self.assertIn("== PR created " + url, work_output)
                if sandbox == "workspace":
                    self.assertIn("== committed ", work_output)
                self.assertIn("PR: " + url, self.cli("status", slug))
                writes = self.gh_writes()
                self.cli("review", slug)
                review_output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertEqual(state["publish"], publication)
                self.assertIn("PR: " + url, self.cli("status", slug))
                self.assertEqual(self.gh_writes(), writes)
                self.assertNotIn("== PR created ", review_output)
                self.assertNotIn("== committed ", review_output)

    def test_workspace_rotation_and_review_fallback_keep_the_sandbox_and_scout_stays_read_only(self):
        self.configure(language="en", sandbox="workspace", writable_dirs=[str(self.root / "cache")])
        roots = [str(self.root / "cache")]
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("sandbox-rotation")
        self.wait("sandbox-rotation")
        self.assertEqual(self.state("sandbox-rotation")["account"], "second")
        self.assertEqual(len(self.invocations()), 2)
        self.assertNotEqual(self.invocations()[0]["thread"], self.invocations()[1]["thread"])
        fixture = json.loads((self.other / "fixture.json").read_text())
        fixture["review_prompt_conflict"] = True
        (self.other / "fixture.json").write_text(json.dumps(fixture))
        self.cli("review", "sandbox-rotation")
        self.wait("sandbox-rotation")
        rejected, fallback = self.invocations()[-2:]
        self.assertEqual(rejected["mode"], "review_prompt_conflict")
        self.assertTrue(rejected["review"])
        self.assertFalse(fallback["review"])
        self.assertEqual(fallback["prompt"], rejected["prompt"])
        for event in self.invocations():
            self.assert_workspace_sandbox(event, roots)
            self.assertIn("## Workspace sandbox", event["prompt"])
        for event in (rejected, fallback):
            self.assertNotIn("commit_message", event["prompt"])
            self.assertNotIn("commit_message", event["output_schema"]["properties"])
        self.cli("scout", "sandbox-scout", "Read the task's implementation")
        self.wait("sandbox-scout")
        scout = self.invocations()[-1]
        self.assert_scout_read_only(scout)
        self.assertFalse(any(value.startswith("sandbox_workspace_write.") for value in scout["argv"]))
        self.assertNotIn("## Workspace sandbox", scout["prompt"])
        self.assertIn("sandbox read-only", self.cli("status", "sandbox-scout"))

    def test_workspace_supervisor_commits_pushes_without_hooks_and_creates_only_one_pr(self):
        self.configure(language="en", sandbox="workspace")
        hooks = self.root / "hooks"
        hooks.mkdir()
        markers = []
        for name in ("pre-commit", "pre-push"):
            marker = self.root / (name + ".ran")
            markers.append(marker)
            hook = hooks / name
            hook.write_text("#!/bin/sh\n: > " + shlex.quote(str(marker)) + "\nexit 17\n")
            hook.chmod(0o700)
        self.git("config", "core.hooksPath", str(hooks))
        message = 'Publish "workspace" changes\n\nKeep supervisor hooks disabled.\nRecord the worktree changes.'
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "first sandbox change\n",
                                                     "new.txt": "new file\n"},
             "reply": self.work_result(commit_message=message)},
            {"mode": "complete", "write_files": {"tracked.txt": "second sandbox change\n"},
             "remove_files": ["new.txt"], "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result()}])
        self.start("sandbox-publish")
        output = self.wait("sandbox-publish")
        state = self.state("sandbox-publish")
        publish = state["publish"]
        self.assertEqual(set(publish), {"at", "sha", "pushed", "pr", "action", "error"})
        self.assertGreater(publish["at"], 0)
        self.assertEqual(publish["sha"], self.git("rev-parse", state["branch"]))
        self.assertTrue(publish["pushed"])
        self.assertEqual(publish["error"], "")
        self.assertEqual(publish["action"], "created")
        self.assertEqual(publish["pr"], "https://example.test/pull/1")
        self.assertEqual(self.git("log", "-1", "--format=%B", state["branch"]), message)
        self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "rev-parse", state["branch"]),
                         publish["sha"])
        subject = message.splitlines()[0]
        self.assertIn("== committed " + publish["sha"] + " " + json.dumps(subject) +
                      "; pushed; PR " + publish["pr"], output)
        creates = [event for event in self.gh_invocations() if event["argv"][:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1)
        args = creates[0]["argv"]
        self.assertEqual(args[args.index("--base") + 1], "main")
        self.assertEqual(args[args.index("--head") + 1], state["branch"])
        self.assertEqual(args[args.index("--title") + 1], subject)
        self.assertEqual(creates[0]["body"], self.work_result()["summary"])
        self.assertIn("== PR created " + publish["pr"], output)
        first_sha = publish["sha"]
        self.cli("say", "sandbox-publish", "Make another change")
        self.wait("sandbox-publish")
        state = self.state("sandbox-publish")
        self.assertNotEqual(state["publish"]["sha"], first_sha)
        self.assertEqual(self.git("log", "-1", "--format=%s", state["branch"]), "sandbox-publish: turn 2")
        self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "rev-parse", state["branch"]),
                         state["publish"]["sha"])
        self.assertFalse((Path(state["worktree"]) / "new.txt").exists())
        for marker in markers:
            self.assertFalse(marker.exists(), "supervisor must disable " + marker.stem)
        second_sha = state["publish"]["sha"]
        self.cli("say", "sandbox-publish", "Confirm that no more changes are needed")
        self.wait("sandbox-publish")
        self.assertEqual(self.git("rev-parse", state["branch"]), second_sha)
        self.assertEqual(self.state("sandbox-publish")["publish"]["action"], "unchanged")
        self.assertEqual(len([event for event in self.gh_invocations()
                              if event["argv"][:2] == ["pr", "create"]]), 1)

    def test_workspace_commits_completed_turns_with_any_typed_status(self):
        self.configure(language="en", sandbox="workspace")
        for status in ("done", "partial", "blocked", "needs_decision"):
            with self.subTest(status=status):
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {"tracked.txt": status + "\n"},
                    "reply": self.work_result(status, commit_message="Save " + status + " checkpoint")}])
                slug = "sandbox-status-" + status.replace("_", "-")
                self.start(slug)
                self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertEqual(state["result"]["status"], status)
                self.assertEqual(self.git("log", "-1", "--format=%s", state["branch"]),
                                 "Save " + status + " checkpoint")
                self.assertTrue(state["publish"]["pushed"])

    def test_workspace_filter_guard_blocks_worker_clean_script_before_status_and_wait(self):
        self.configure(language="en", sandbox="workspace")
        # Commit the attributes while no driver is configured, then install a
        # harmless initial clean command before the worker replaces its script.
        self.commit_fixture({".gitattributes": "tracked.txt filter=project\n",
                             "clean.sh": "#!/bin/sh\ncat\n"})
        self.git("config", "filter.project.clean", "/bin/sh clean.sh")
        initial = self.git("rev-parse", "HEAD")
        marker = self.root / "outside-worktree.marker"
        marker.write_text("untouched\n")
        script = "#!/bin/sh\nprintf 'filter executed\\n' > " + shlex.quote(str(marker)) + "\ncat\n"
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "worker change\n", "clean.sh": script},
            "reply": self.work_result(commit_message="Do not run a worker-written filter")}])
        trace_path = self.record_git_calls()
        self.start("sandbox-filter-guard")
        output = self.wait("sandbox-filter-guard")
        state = self.state("sandbox-filter-guard")
        error = ("commit: filter driver project is configured; the supervisor does not run filters "
                 "on worker-written files — commit this turn manually")
        self.assertEqual(state["outcome"], "completed")
        self.assertIn(error, state["publish"]["error"])
        self.assertIn("not pushed", state["publish"]["error"])
        self.assertEqual(state["publish"]["sha"], "")
        self.assertFalse(state["publish"]["pushed"])
        self.assertEqual(state["publish"]["pr"], "")
        self.assertIn("== publish error: " + error, output)
        self.assertEqual(marker.read_text(), "untouched\n")
        self.assertEqual(self.git("rev-parse", state["branch"]), initial)
        self.assertEqual((Path(state["worktree"]) / "clean.sh").read_text(), script)
        self.assertEqual((Path(state["worktree"]) / "tracked.txt").read_text(), "worker change\n")
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        worktree_events = [event for event in events if event["cwd"] == state["worktree"]]
        self.assertTrue(worktree_events)
        self.assertIn("config", worktree_events[0]["argv"])
        self.assertTrue(any("--get-regexp" in event["argv"] for event in worktree_events))
        self.assertFalse(any("status" in event["argv"] or "add" in event["argv"] or
                             "commit" in event["argv"] for event in worktree_events), worktree_events)
        self.assertFalse(self.gh_invocations())

    def test_workspace_filter_guard_allows_git_lfs_command_prefixes_without_running_lfs(self):
        self.configure(language="en", sandbox="workspace")
        for index, command in enumerate(("git-lfs clean -- %f", "git lfs filter-process")):
            with self.subTest(command=command):
                # No attributes select this driver: real Git LFS is never run.
                self.git("config", "filter.lfs.clean", command)
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {"tracked.txt": command + "\n"},
                    "reply": self.work_result(commit_message="Allow an inactive Git LFS driver")}])
                slug = "sandbox-lfs-filter-" + str(index)
                self.start(slug)
                self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertEqual(state["publish"]["error"], "")
                self.assertTrue(state["publish"]["pushed"])
                self.assertEqual(self.git("log", "-1", "--format=%s", state["branch"]),
                                 "Allow an inactive Git LFS driver")

    def test_workspace_filter_guard_reads_all_config_scopes_and_command_forms(self):
        self.env.update(GIT_CONFIG_SYSTEM=str(self.root / "git-system.conf"),
                        GIT_CONFIG_GLOBAL=str(self.root / "git-global.conf"))
        worker = self.worker()
        cases = (("clean", "/bin/sh clean.sh", "project.nested"),
                 ("smudge", "/bin/sh\nclean.sh", "project.nested"),
                 ("process", "git-lfs-custom process", "project.nested"),
                 ("clean", "", "project.nested"),
                 ("smudge", " git lfs smudge", "project.nested"),
                 ("process", "git-lfs filter-process", None),
                 ("clean", "git lfs clean -- %f", None),
                 ("smudge", "git-lfs smudge\n--skip", None))
        with mock.patch.dict(os.environ, self.env):
            self.assertIsNone(worker.sandboxing.unsafe_filter_driver(self.repo))
            for scope in ("--system", "--global", "--local"):
                for operation, command, expected in cases:
                    with self.subTest(scope=scope, operation=operation, command=command):
                        key = "filter.project.nested." + operation
                        self.git("config", scope, key, command)
                        try:
                            self.assertEqual(worker.sandboxing.unsafe_filter_driver(self.repo), expected)
                        finally:
                            self.git("config", scope, "--unset-all", key)

    def test_workspace_supervisor_git_commands_disable_fsmonitor_and_hooks(self):
        self.configure(language="en", sandbox="workspace")
        trace_path = self.record_git_calls()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "supervisor publication\n"},
            "reply": self.work_result(commit_message="Keep supervisor Git commands isolated")}])
        self.start("sandbox-git-safety")
        self.wait("sandbox-git-safety")
        self.cli("status", "sandbox-git-safety")
        self.assertTrue(self.state("sandbox-git-safety")["publish"]["pushed"])
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        self.assertTrue(events)
        for event in events:
            argv = event["argv"]
            self.assertIn("core.hooksPath=/dev/null", argv, event)
            self.assertIn("core.fsmonitor=false", argv, event)
            for setting in ("core.hooksPath=/dev/null", "core.fsmonitor=false"):
                self.assertEqual(argv[argv.index(setting) - 1], "-c", event)
        for command in ("config", "status", "add", "commit", "push"):
            self.assertTrue(any(command in event["argv"] for event in events), (command, events))

    def test_workspace_stopped_and_failed_turns_leave_changes_uncommitted(self):
        self.configure(language="en", sandbox="workspace")
        initial = self.git("rev-parse", "HEAD")
        for mode in ("failed", "hold"):
            with self.subTest(mode=mode):
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": mode, "write_files": {"tracked.txt": mode + " changes\n"},
                    "reply": self.work_result(commit_message="Do not commit interrupted work")}])
                slug = "sandbox-" + mode
                self.start(slug)
                worktree = Path(self.state(slug)["worktree"])
                self.until(lambda: (worktree / "tracked.txt").read_text() == mode + " changes\n")
                if mode == "hold":
                    self.cli("stop", slug, timeout=20)
                self.cli("wait", slug, "--timeout", "10")
                state = self.state(slug)
                self.assertEqual(state["outcome"], "stopped" if mode == "hold" else "failed")
                self.assertEqual(self.git("rev-parse", state["branch"]), initial)
                self.assertFalse(state.get("publish"))
                self.assertFalse(self.gh_invocations())

    def test_workspace_check_uses_codex_sandbox_and_keeps_the_saved_command_and_log(self):
        roots = [str(self.root / "cache")]
        self.configure(language="en", sandbox="workspace", writable_dirs=roots)
        command = "printf 'sandbox check marker\\n'; test -f tracked.txt"
        self.start("sandbox-check", "--check", command)
        output = self.wait("sandbox-check")
        state = self.state("sandbox-check")
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual(state["checks"][-1]["cmd"], command)
        events = self.sandbox_invocations()
        self.assertEqual(len(events), 1)
        self.assert_workspace_sandbox(events[0], roots)
        self.assertEqual(events[0]["argv"][-4:], ["--", "/bin/sh", "-c", command])
        self.assertEqual(events[0]["cwd"], state["worktree"])
        self.assertEqual(events[0]["home"], str(self.home))
        self.assertIn("sandbox check marker", (self.home / "claude-tasks" / "sandbox-check" / "check.log").read_text())
        self.assertIn("== check `" + command + "`: passed", output)

    def test_workspace_check_timeout_and_stop_kill_the_sandbox_process_group(self):
        self.configure(language="en", sandbox="workspace")
        command = 'sleep 60 & child=$!; echo "$child" > check-child.pid; wait "$child"'
        for outcome in ("timeout", "stopped"):
            with self.subTest(outcome=outcome):
                slug = "sandbox-check-" + outcome
                self.start(slug, "--check", command, "--check-timeout", "0.5" if outcome == "timeout" else "60")
                child = self.check_child(slug)
                if outcome == "stopped":
                    self.cli("stop", slug, timeout=20)
                self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["checks"][-1]["outcome"], outcome)
                if outcome == "timeout":
                    self.assertGreaterEqual(state["checks"][-1]["seconds"], 0.5)
                else:
                    self.assertEqual(state["outcome"], "stopped")
                self.assertFalse(state.get("check_pid"))
                event = self.sandbox_invocations()[-1]
                self.assert_workspace_sandbox(event)
                self.assertEqual(event["pid"], event["pgid"])
                self.until(lambda: not self.process_is_live(child))
                self.until(lambda: not self.process_is_live(event["pid"]))

    def test_workspace_push_failure_is_reported_without_failing_the_turn(self):
        self.configure(language="en", sandbox="workspace")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "write_files": {"tracked.txt": "unpublished change\n"},
             "reply": self.work_result(commit_message="Save work without an origin")}])
        self.start("sandbox-no-origin")
        self.wait("sandbox-no-origin")
        self.assertEqual(self.state("sandbox-no-origin")["publish"]["action"], "skipped")
        self.assertIn("not pushed", self.state("sandbox-no-origin")["publish"]["error"])
        self.git("remote", "remove", "origin")
        self.cli("say", "sandbox-no-origin", "Save another change")
        output = self.wait("sandbox-no-origin")
        state = self.state("sandbox-no-origin")
        publish = state["publish"]
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(publish["sha"], self.git("rev-parse", state["branch"]))
        self.assertFalse(publish["pushed"])
        self.assertEqual(publish["pr"], "")
        self.assertIn("origin", publish["error"])
        self.assertIn("== publish error: " + publish["error"], output)
        self.assertFalse(self.gh_invocations())

    def test_workspace_pr_failure_is_reported_without_failing_the_turn(self):
        self.configure(language="en", sandbox="workspace")
        self.env["CODEX_FAKE_GH_CREATE_ERROR"] = "1"
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "published change without PR\n"},
            "reply": self.work_result(commit_message="Publish before a PR service failure")}])
        self.start("sandbox-pr-error")
        output = self.wait("sandbox-pr-error")
        state = self.state("sandbox-pr-error")
        self.assertEqual(state["outcome"], "completed")
        self.assertTrue(state["publish"]["pushed"])
        self.assertEqual(state["publish"]["pr"], "")
        self.assertIn("fixture PR creation failed", state["publish"]["error"])
        self.assertIn("== PR skipped: " + state["publish"]["error"], output)
        self.assertNotIn("== publish error:", output)

    def test_ci_pending_then_passed_is_reported_without_an_extra_codex_turn(self):
        self.configure(language="en", sandbox="workspace")
        self.configure_notify()
        self.configure_ci([[self.ci_check("pending")],
                           [self.ci_check(), self.ci_check("skipping", name="optional", job="124")]])
        self.account(self.home, "A", self.reset, runs=[self.ci_work()])
        self.start("ci-pass", "--ci")
        output = self.wait("ci-pass")
        state = self.state("ci-pass")
        ci = state["ci"]
        self.assertEqual((ci["outcome"], ci["reason"], ci["rounds"]), ("passed", "passed", 0))
        self.assertEqual(ci["sha"], self.git("rev-parse", state["branch"]))
        self.assertEqual([check["bucket"] for check in ci["checks"]], ["pass", "skipping"])
        self.assertGreaterEqual(ci["seconds"], 0)
        self.assertRegex(output, r"== CI " + ci["sha"][:7] + r": passed \(2/2, \d+(?:\.\d+)? min\)")
        self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(len(self.ci_polls()), 2)
        for event in self.ci_polls():
            self.assertEqual(event["argv"][2:], [state["publish"]["pr"], "--json", "name,state,bucket,link"])
        self.assertEqual(self.notification("ci-pass", "turn_finished")["ci"], ci)
        self.until(lambda: any(row["phase"] == "turn" for row in self.telemetry_journal()))
        turn = next(row for row in self.telemetry_journal() if row["phase"] == "turn")
        self.assertEqual(turn["ci"], ci)
        report = json.loads(self.cli("report", "--slug", "ci-pass", "--json"))
        self.assertEqual(report["tasks"][0]["ci"], ci)
        self.assertEqual(report["summary"]["ci"], {"passed": 1, "total": 1, "pass_fraction": 1.0})
        self.assertIn("CI passed: 1/1", self.cli("report", "--slug", "ci-pass"))

    def test_ci_failed_round_uses_same_thread_log_checks_and_no_second_review(self):
        self.configure(language="en", sandbox="workspace")
        self.env["GH_REPO"] = "unrelated/host-repository"
        log = "fixture macOS traceback\nAssertionError: Python 3.9 compatibility\n"
        failed_check = self.ci_check("fail")
        self.configure_ci([[self.ci_check("pending")], [failed_check],
                           [self.ci_check("pending")], [self.ci_check()]], jobs={"123": log})
        self.account(self.home, "A", self.reset, runs=[
            self.ci_work(), {"mode": "complete", "reply": self.review_result(False)},
            self.ci_work("fixed Python 3.9\n")])
        check_log = self.root / "ci-local-checks"
        command = "printf 'checked\\n' >> " + shlex.quote(str(check_log))
        self.start("ci-fixed", "--ci", "--review", "--check", command)
        output = self.wait("ci-fixed")
        state = self.state("ci-fixed")
        self.assertEqual((state["ci"]["outcome"], state["ci"]["rounds"], state["ci"]["reason"]),
                         ("passed", 1, "passed"))
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertEqual(check_log.read_text().splitlines(), ["checked", "checked"])
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False])
        self.assertTrue(executions[2]["resume"])
        self.assertEqual(executions[2]["thread"], executions[0]["thread"])
        for text in (failed_check["name"], failed_check["link"]) + tuple(log.splitlines()):
            self.assertIn(text, executions[2]["prompt"])
        jobs = [event["argv"] for event in self.gh_invocations() if event["argv"][:2] == ["run", "view"]]
        self.assertEqual(jobs, [["run", "view", "--job", "123", "--log-failed",
                                 "--repo", "github.com/fixture/repo"]])
        self.assertEqual(len(self.ci_polls()), 4)
        self.assertEqual(state["ci"]["sha"], self.git("rev-parse", state["branch"]))
        publication = state["publish"]
        self.assertEqual((publication["action"], publication["error"]), ("updated", ""))
        create, edit = self.gh_writes()
        self.assertEqual(create["argv"][:2], ["pr", "create"])
        self.assertEqual(edit["argv"][:3], ["pr", "edit", publication["pr"]])
        self.assertTrue(publication["pr"])
        self.assertTrue(all(event["argv"][2] == publication["pr"] for event in self.ci_polls()))
        for event in self.gh_invocations():
            if event["argv"][:2] == ["pr", "list"]:
                self.assertEqual(event["argv"][event["argv"].index("--json") + 1],
                                 "url,state,headRepository,headRepositoryOwner")
        pr = json.loads(Path(self.env["CODEX_FAKE_GH_STATE"]).read_text())[state["branch"]]
        self.assertEqual(pr["headRepository"]["nameWithOwner"], "")
        self.assertEqual(pr["headRepositoryOwner"]["login"] + "/" + pr["headRepository"]["name"],
                         "fixture/repo")
        self.assertRegex(output, r"== review by main \(\d+(?:\.\d+)? min\): no defects")
        self.assertIn(self.review_result(False)["summary"], output)

    def test_ci_repair_keeps_current_review_findings_in_wait_without_review_fix_rounds(self):
        self.configure(language="en", sandbox="workspace")
        for angled in (False, True):
            with self.subTest(angled=angled):
                (self.home / "exec-count").unlink(missing_ok=True)
                self.configure_ci([[self.ci_check("fail")], [self.ci_check()]],
                                  jobs={"123": "fixture CI-only compatibility failure"})
                review = self.review_result()
                reviews = [{"mode": "complete", "reply": review}]
                if angled:
                    reviews.append({"mode": "complete", "reply": self.review_result(False)})
                self.account(self.home, "A", self.reset, runs=[
                    self.ci_work(), *reviews, self.ci_work("fixed CI compatibility\n"),
                    self.ci_work("followup without review\n")])
                slug = "ci-retains-angle-review" if angled else "ci-retains-review"
                options = ("--review-angles", "correctness,tests") if angled else ("--review",)
                before = len(self.invocations())
                polls = len(self.ci_polls())
                self.start(slug, "--ci", *options)
                output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual((state["ci"]["outcome"], state["ci"]["rounds"]), ("passed", 1))
                self.assertEqual(state["kind"], "work")
                self.assertEqual(state["rounds"], [])
                self.assertEqual(len(state["review_result"]["findings"]), 2)
                expected_reviews = [False] + [True] * len(reviews) + [False]
                self.assertEqual([entry["review"] for entry in self.invocations()[before:]],
                                 expected_reviews)
                self.assertEqual(len(self.ci_polls()) - polls, 2)
                heading = "== review angle correctness by main" if angled else "== review by main"
                self.assertRegex(output, heading + r" \(\d+(?:\.\d+)? min\): 2 defects")
                for finding in review["findings"]:
                    self.assertIn("{file}:{line} — {title}".format(**finding), output)
                    self.assertIn(finding["detail"], output)
                    self.assertIn("verify: " + finding["verify"], output)
                self.assertIn("== CI " + state["ci"]["sha"][:7] + ": passed", output)

                self.cli("say", slug, "Continue without another review or CI wait", "--no-ci")
                followup = self.wait(slug)
                current = self.state(slug)
                self.assertEqual(current["review_start"], len(current["reviews"]))
                self.assertNotIn("== review", followup)
                for finding in review["findings"]:
                    self.assertNotIn(finding["title"], followup)
                self.assertEqual(len(self.ci_polls()) - polls, 2)

    def test_ci_repair_refusal_keeps_the_completed_review_findings_in_wait(self):
        self.configure(language="en", sandbox="workspace")
        self.configure_ci([[self.ci_check("fail")]])
        review = self.review_result()
        refusal = "This content was flagged for possible cybersecurity risk."
        self.account(self.home, "A", self.reset, runs=[
            self.ci_work(), {"mode": "complete", "reply": review},
            {"mode": "refused", "error": refusal}])
        self.start("ci-refused-retains-review", "--ci", "--review")
        output = self.wait("ci-refused-retains-review")
        state = self.state("ci-refused-retains-review")
        self.assertEqual((state["outcome"], state["ci"]["reason"]), ("refused", "refused"))
        self.assertEqual(state["rounds"], [])
        self.assertEqual([entry["review"] for entry in self.invocations()], [False, True, False])
        self.assertEqual(len(self.ci_polls()), 1)
        self.assertIn("== refused by the model: " + refusal, output)
        self.assertRegex(output, r"== review by main \(\d+(?:\.\d+)? min\): 2 defects")
        for finding in review["findings"]:
            self.assertIn("{file}:{line} — {title}".format(**finding), output)
            self.assertIn(finding["detail"], output)
            self.assertIn("verify: " + finding["verify"], output)

    def test_ci_repair_prompt_uses_russian_and_keeps_the_failed_job_evidence(self):
        self.configure(language="ru", sandbox="workspace")
        log = "fixture Russian CI repair traceback\nAssertionError: Python 3.9 support\n"
        failed_check = self.ci_check("fail")
        self.configure_ci([[failed_check], [self.ci_check()]], jobs={"123": log})
        self.account(self.home, "A", self.reset, runs=[self.ci_work(), self.ci_work("fixed Python 3.9\n")])
        self.start("ci-fixed-ru", "--ci", "--check", "true")
        self.wait("ci-fixed-ru")
        state = self.state("ci-fixed-ru")
        self.assertEqual((state["ci"]["outcome"], state["ci"]["rounds"]), ("passed", 1))
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        worker = self.worker()
        expected = worker.ci_runs.ci_fix_prompt(state, state["ci"], worker.config.accounts_config())
        self.assertEqual(executions[1]["prompt"].partition("\n\n")[0],
                         expected.partition("\n\n")[0])
        for text in (failed_check["name"], failed_check["link"]) + tuple(log.splitlines()):
            self.assertIn(text, executions[1]["prompt"])
        self.assertEqual([event["argv"][:2] for event in self.gh_writes()],
                         [["pr", "create"], ["pr", "edit"]])
        self.assertEqual(len(self.ci_polls()), 2)

    def test_ci_repair_prompt_localizes_rules_and_workspace_publication_in_both_languages(self):
        worker = self.worker()
        check = self.ci_check("fail")
        check["tail"] = "fixture failed job evidence\nAssertionError: Python 3.9\n"
        ci = {"sha": "a" * 40, "outcome": "failed", "checks": [check]}
        for language in ("en", "ru"):
            self.configure(language=language)
            for sandbox in ("off", "workspace"):
                with self.subTest(language=language, sandbox=sandbox):
                    prompt = worker.ci_runs.ci_fix_prompt({"sandbox": sandbox}, ci, {"language": language})
                    for text in (check["name"], check["link"], "fixture failed job evidence",
                                 "Python 3.9"):
                        self.assertIn(text, prompt)
                    self.assertEqual(prompt, worker.ci_runs.ci_fix_prompt({"sandbox": sandbox}, ci,
                                                                 worker.config.accounts_config()))
                    if sandbox == "workspace":
                        self.assertNotEqual(prompt, worker.ci_runs.ci_fix_prompt({"sandbox": "off"}, ci,
                                                                        {"language": language}))

    def test_ci_starts_after_review_auto_fix_and_its_second_local_check(self):
        self.configure(language="en", sandbox="workspace")
        self.configure_ci([[self.ci_check()]])
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Correct the reported issue."}
                     for finding in self.review_result()["findings"]]
        self.account(self.home, "A", self.reset, runs=[
            self.ci_work(), {"mode": "complete", "reply": self.review_result()},
            self.ci_work("fixed by review\n", findings=decisions),
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("ci-after-fix", "--ci", "--review", "--fix-rounds", "1", "--check", "true")
        self.wait("ci-after-fix")
        state = self.state("ci-after-fix")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(state["ci"]["outcome"], "passed")
        self.assertEqual(state["ci"]["rounds"], 0)
        self.assertEqual(len(state["checks"]), 2)
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False, True])
        self.assertEqual(len(self.ci_polls()), 1)
        self.assertEqual(state["ci"]["sha"], self.git("rev-parse", state["branch"]))

    def test_ci_failed_after_one_round_exhausts_rounds_and_wait_shows_the_log_tail(self):
        self.configure(language="en", sandbox="workspace")
        log = "failure at start\n" + "\n".join("trace line %03d" % index for index in range(200))
        self.configure_ci([[self.ci_check("fail")]], jobs={"123": log})
        self.account(self.home, "A", self.reset, runs=[self.ci_work(), self.ci_work("still broken\n")])
        self.start("ci-exhausted", "--ci")
        output = self.wait("ci-exhausted")
        ci = self.state("ci-exhausted")["ci"]
        self.assertEqual((ci["outcome"], ci["rounds"], ci["reason"]), ("failed", 1, "rounds_exhausted"))
        self.assertIn("== CI " + ci["sha"][:7] + ": FAILED 1/1: " + self.ci_check()["name"], output)
        self.assertIn("trace line 199", output)
        self.assertNotIn("failure at start", output)
        self.assertEqual(len(ci["checks"][0]["tail"].splitlines()), 150)
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual(len(self.ci_polls()), 2)

    def test_ci_blocked_round_checks_locally_and_stops_without_polling_again(self):
        self.configure(language="en", sandbox="workspace")
        self.env["GH_REPO"] = "unrelated/host-repository"
        self.configure_ci([[self.ci_check("fail")]], jobs={"123": "fixture unrelated network outage"})
        blocked = self.work_result("blocked", summary="A public runner outage predates this branch.",
                                   remaining=["Runner logs show the same failure on main."])
        self.account(self.home, "A", self.reset, runs=[self.ci_work(), {"mode": "complete", "reply": blocked}])
        self.start("ci-blocked", "--ci", "--check", "true")
        self.wait("ci-blocked")
        state = self.state("ci-blocked")
        self.assertEqual((state["ci"]["outcome"], state["ci"]["rounds"], state["ci"]["reason"]),
                         ("failed", 1, "blocked"))
        self.assertEqual(state["result"], blocked)
        self.assertEqual(state["ci"]["checks"][0]["tail"], "fixture unrelated network outage")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertEqual(len(self.ci_polls()), 1)
        self.assertEqual(len(self.invocations()), 2)

    def test_ci_flags_and_config_defaults_apply_to_start(self):
        self.configure(language="en", sandbox="workspace")
        defaults = self.worker().config.accounts_config()
        self.assertFalse(defaults["ci"])
        self.assertEqual(defaults["ci_timeout_minutes"], 30)
        self.configure_ci([[self.ci_check()]])
        self.account(self.home, "A", self.reset, runs=[self.ci_work()])
        self.start("ci-flag", "--ci", "--ci-rounds", "2")
        self.wait("ci-flag")
        flagged = self.state("ci-flag")
        self.assertTrue(flagged["ci_enabled"])
        self.assertEqual(flagged["ci_rounds"], 2)
        self.assertEqual(flagged["ci"]["outcome"], "passed")
        self.configure(language="en", sandbox="workspace", ci=True, ci_timeout_minutes=17)
        self.start("ci-config")
        self.wait("ci-config")
        configured = self.state("ci-config")
        self.assertTrue(configured["ci_enabled"])
        self.assertEqual(configured["ci_rounds"], 1)
        self.assertEqual(configured["ci_timeout_minutes"], 17)
        self.assertEqual(configured["ci"]["outcome"], "passed")
        self.assertEqual(len(self.ci_polls()), 2)

    def test_say_preserves_task_ci_and_rounds_without_flags(self):
        self.configure(language="en", sandbox="workspace")
        self.configure_ci([[self.ci_check("fail")]])
        self.account(self.home, "A", self.reset, runs=[self.ci_work()])
        self.start("ci-inherited", "--ci", "--ci-rounds", "0")
        self.wait("ci-inherited")
        self.cli("say", "ci-inherited", "Inspect the existing CI failure")
        self.wait("ci-inherited")
        state = self.state("ci-inherited")
        self.assertTrue(state["ci_enabled"])
        self.assertEqual(state["ci_rounds"], 0)
        self.assertEqual((state["ci"]["outcome"], state["ci"]["reason"]), ("failed", "rounds_exhausted"))
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual(len(self.ci_polls()), 2)

    def test_say_ci_enables_task_ci(self):
        self.configure(language="en", sandbox="workspace")
        self.configure_ci([[self.ci_check("fail")]])
        self.account(self.home, "A", self.reset, runs=[self.ci_work()])
        self.start("ci-say", "--ci-rounds", "0")
        self.wait("ci-say")
        self.assertFalse(self.state("ci-say").get("ci"))
        self.assertFalse(self.ci_polls())
        self.cli("say", "ci-say", "Inspect the existing CI failure", "--ci")
        self.wait("ci-say")
        state = self.state("ci-say")
        self.assertTrue(state["ci_enabled"])
        self.assertEqual(state["ci_rounds"], 0)
        self.assertEqual((state["ci"]["outcome"], state["ci"]["reason"]), ("failed", "rounds_exhausted"))
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual(len(self.ci_polls()), 1)

    def test_say_no_ci_disables_task_ci_and_preserves_that_setting(self):
        self.configure(language="en", sandbox="workspace", ci=True)
        self.configure_ci([[self.ci_check()]])
        self.account(self.home, "A", self.reset, runs=[self.ci_work()])
        self.start("ci-disabled", "--ci-rounds", "2")
        self.wait("ci-disabled")
        self.assertTrue(self.state("ci-disabled")["ci_enabled"])
        before = len(self.ci_polls())
        self.cli("say", "ci-disabled", "Continue without CI enabled", "--no-ci")
        self.wait("ci-disabled")
        self.assertFalse(self.state("ci-disabled")["ci_enabled"])
        self.assertFalse(self.state("ci-disabled").get("ci"))
        self.cli("say", "ci-disabled", "Keep CI disabled")
        self.wait("ci-disabled")
        state = self.state("ci-disabled")
        self.assertFalse(state["ci_enabled"])
        self.assertFalse(state.get("ci"))
        self.assertEqual(state["ci_rounds"], 2)
        self.assertEqual(len(self.ci_polls()), before)
        self.assertEqual(len(self.invocations()), 3)

    def test_say_ci_rounds_replaces_the_saved_limit_without_changing_ci(self):
        self.configure(language="en", sandbox="workspace")
        self.configure_ci([[self.ci_check("fail")]])
        self.account(self.home, "A", self.reset, runs=[self.ci_work()])
        self.start("ci-rounds", "--ci", "--ci-rounds", "0")
        self.wait("ci-rounds")
        self.cli("say", "ci-rounds", "Allow one repair attempt", "--ci-rounds", "1")
        self.wait("ci-rounds")
        state = self.state("ci-rounds")
        self.assertTrue(state["ci_enabled"])
        self.assertEqual(state["ci_rounds"], 1)
        self.assertEqual((state["ci"]["outcome"], state["ci"]["rounds"], state["ci"]["reason"]),
                         ("failed", 1, "rounds_exhausted"))
        self.assertEqual(len(self.invocations()), 3)
        self.assertEqual(len(self.ci_polls()), 3)
        self.cli("say", "ci-rounds", "Disable CI but save its next repair limit", "--no-ci", "--ci-rounds", "2")
        self.wait("ci-rounds")
        self.cli("say", "ci-rounds", "Change only the repair limit", "--ci-rounds", "3")
        self.wait("ci-rounds")
        state = self.state("ci-rounds")
        self.assertFalse(state["ci_enabled"])
        self.assertEqual(state["ci_rounds"], 3)
        self.assertEqual(len(self.ci_polls()), 3)

    def test_say_rejects_conflicting_ci_flags_before_starting_a_turn(self):
        self.configure(language="en")
        self.start("ci-conflicting")
        self.wait("ci-conflicting")
        before, state = len(self.invocations()), self.state("ci-conflicting")
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "say", "ci-conflicting", "Do not run",
                                 "--ci", "--no-ci"], cwd=self.repo, env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)
        self.assertEqual(len(self.invocations()), before)
        self.assertEqual(self.state("ci-conflicting"), state)

    def test_ci_rounds_reject_negative_and_fractional_values_before_any_turn(self):
        self.configure(language="en")
        self.start("ci-invalid-existing")
        self.wait("ci-invalid-existing")
        before, state = len(self.invocations()), self.state("ci-invalid-existing")
        for value in ("-1", "1.5"):
            for args in (("start", "ci-invalid", "--brief", str(self.brief), "--repo", str(self.repo)),
                         ("say", "ci-invalid-existing", "Do not start invalid CI")):
                with self.subTest(command=args[0], value=value):
                    result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(args) + ["--ci", "--ci-rounds", value],
                                            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("ci-rounds", result.stdout + result.stderr)
                    self.assertEqual(len(self.invocations()), before)
                    self.assertEqual(self.state("ci-invalid-existing"), state)
                    self.assertFalse((self.home / "claude-tasks" / "ci-invalid").exists())

    def test_ci_skips_head_mismatch_missing_pr_and_failed_local_check(self):
        cases = (("mismatch", {"head_sha": "f" * 40}, False, None),
                 ("no-pr", {}, True, None), ("local-check", {}, False, "false"))
        for label, settings, no_pr, check in cases:
            with self.subTest(case=label):
                self.configure(language="en", sandbox="workspace")
                self.configure_ci([[self.ci_check()]], **settings)
                self.account(self.home, "A", self.reset, runs=[self.ci_work(label + "\n")])
                (self.home / "exec-count").unlink(missing_ok=True)
                if no_pr:
                    self.env["CODEX_FAKE_GH_CREATE_ERROR"] = "1"
                args = ["--ci"] + (["--check", check] if check else [])
                before = len(self.ci_polls())
                self.start("ci-skip-" + label, *args)
                output = self.wait("ci-skip-" + label)
                self.env.pop("CODEX_FAKE_GH_CREATE_ERROR", None)
                ci = self.state("ci-skip-" + label)["ci"]
                self.assertEqual(ci["outcome"], "skipped")
                self.assertTrue(ci["reason"])
                self.assertEqual(len(self.ci_polls()), before)
                if no_pr:
                    self.assertIn("== PR skipped:", output)
                    self.assertNotIn("== publish error:", output)

    def test_ci_known_pr_edit_failure_still_waits_for_matching_head(self):
        self.configure(language="en", sandbox="workspace")
        self.configure_ci([[self.ci_check()]])
        url = "https://example.test/pull/42"
        Path(self.env["CODEX_FAKE_GH_STATE"]).write_text(json.dumps({
            "codex/ci-edit-error": {"url": url, "state": "OPEN"}}))
        self.env["CODEX_FAKE_GH_EDIT_ERROR"] = "1"
        self.account(self.home, "A", self.reset, runs=[self.ci_work()])
        self.start("ci-edit-error", "--ci")
        output = self.wait("ci-edit-error")
        state = self.state("ci-edit-error")
        self.assertEqual((state["publish"]["action"], state["publish"]["pr"]), ("skipped", url))
        self.assertIn("fixture PR edit failed", state["publish"]["error"])
        self.assertEqual(state["ci"]["outcome"], "passed")
        self.assertEqual(state["ci"]["sha"], self.git("rev-parse", state["branch"]))
        self.assertEqual(len(self.ci_polls()), 1)
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("== PR skipped:", output)
        self.assertNotIn("== publish error:", output)

    def test_ci_stop_and_signal_interrupt_pending_polling(self):
        for label in ("stop", "signal"):
            with self.subTest(cancel=label):
                self.configure(language="en", sandbox="workspace")
                self.configure_ci([[self.ci_check("pending")]])
                self.account(self.home, "A", self.reset, runs=[self.ci_work(label + "\n")])
                (self.home / "exec-count").unlink(missing_ok=True)
                before = len(self.ci_polls())
                self.start("ci-" + label, "--ci")
                self.until(lambda: len(self.ci_polls()) > before)
                if label == "stop":
                    self.cli("stop", "ci-" + label)
                else:
                    os.kill(self.state("ci-" + label)["pid"], signal.SIGTERM)
                self.wait("ci-" + label)
                state = self.state("ci-" + label)
                self.assertEqual(state["outcome"], "stopped" if label == "stop" else "interrupted")
                count = len(self.ci_polls())
                self.assertEqual(len(self.invocations()), 1 if label == "stop" else 2)
                self.assertEqual(len(self.ci_polls()), count)

    def run_ci_wait_fixture(self, snapshots, timeout=30, jobs=None, no_checks_error=False, **changes):
        worker = self.worker()
        st = {"slug": "ci-clock", "worktree": str(self.repo), "outcome": "completed", "ci_enabled": True,
              "ci_timeout_minutes": timeout, "publish": {"pr": "https://example.test/pull/1"}}
        st.update(changes)
        clock, calls, waits, polls = [1000.0], [], [], [0]
        head = self.git("rev-parse", "HEAD")

        def command(state, args, stopped, timeout=60):
            self.assertIs(state, st)
            args = list(args)
            if Path(args[0]).name == "gh":
                args = args[1:]
            calls.append(args)
            if args[:2] == ["pr", "view"]:
                return subprocess.CompletedProcess(args, 0, json.dumps({"headRefOid": head}), "")
            if args[:2] == ["pr", "checks"]:
                checks = snapshots[min(polls[0], len(snapshots) - 1)]
                polls[0] += 1
                if not checks and no_checks_error:
                    return subprocess.CompletedProcess(args, 1, "", "no checks reported on the 'fixture' branch")
                code = 8 if any(check["bucket"] == "pending" for check in checks) else 0
                if any(check["bucket"] in ("fail", "cancel") for check in checks):
                    code = 1
                return subprocess.CompletedProcess(args, code, json.dumps(checks), "")
            self.assertEqual(args[:2], ["run", "view"])
            return subprocess.CompletedProcess(args, 0, (jobs or {})[args[args.index("--job") + 1]], "")

        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        with mock.patch.object(worker.task_state, "save"), mock.patch.object(worker.ci_runs, "ci_command", side_effect=command), \
                mock.patch.object(worker.ci_runs.shutil, "which", return_value=str(self.root / "gh")), \
                mock.patch.object(worker.ci_runs.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.config, "MINUTE_SECONDS", 60), mock.patch.object(worker.config, "CI_POLL_SECONDS", 30):
            result = worker.ci_runs.wait_ci(st, lambda: False, sleep)
        self.assertEqual(st["ci"], result)
        return st, result, calls, waits

    def test_ci_no_checks_grace_and_pending_timeout_use_fake_time(self):
        for snapshots, timeout, outcome, seconds in (([[]], 30, "none", 180),
                                                     ([[self.ci_check("pending")]], 2, "timeout", 120)):
            with self.subTest(outcome=outcome):
                st, ci, calls, waits = self.run_ci_wait_fixture(snapshots, timeout=timeout)
                self.assertEqual((ci["outcome"], ci["reason"]), (outcome, outcome))
                self.assertEqual(ci["seconds"], seconds)
                self.assertEqual(sum(waits), seconds)
                self.assertTrue(all(0 < value <= 30 for value in waits))
                self.assertEqual(st["outcome"], "completed")
                self.assertGreaterEqual(len(calls), 2)

    def test_ci_wait_does_not_spend_weekly_or_active_minutes_budget(self):
        pending = [self.ci_check("pending")]
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [pending] * 8 + [[self.ci_check()]], max_turn_minutes=1,
            max_task_weekly_percent=1, weekly_used_percent=0.75)
        self.assertEqual((ci["outcome"], st["outcome"]), ("passed", "completed"))
        self.assertEqual(ci["seconds"], 240)
        self.assertEqual(st["weekly_used_percent"], 0.75)
        self.assertNotIn("turn_stop", st)
        self.assertEqual(sum(waits), 240)
        self.assertTrue(all(call[:2] in (["pr", "view"], ["pr", "checks"]) for call in calls))

    def test_ci_gh_no_checks_reported_exit_one_observes_the_grace_period(self):
        st, ci, calls, waits = self.run_ci_wait_fixture([[]], no_checks_error=True)
        self.assertEqual((ci["outcome"], ci["reason"], ci["seconds"]), ("none", "none", 180))
        self.assertEqual(sum(waits), 180)
        self.assertGreaterEqual(len(calls), 2)

    def test_ci_failed_job_logs_limit_150_lines_per_check_400_total_and_external_link(self):
        checks = [self.ci_check("fail", name="job " + str(index), job=str(index)) for index in range(4)]
        checks[1]["link"] = checks[1]["link"].replace("/job/", "/jobs/")
        checks.append({"name": "external lint", "state": "CANCELLED", "bucket": "cancel",
                       "link": "https://ci.example.test/build/99"})
        jobs = {str(index): "\n".join("job %s line %03d" % (index, line) for line in range(250))
                for index in range(4)}
        st, ci, calls, waits = self.run_ci_wait_fixture([checks], jobs=jobs)
        self.assertEqual(ci["outcome"], "failed")
        tails = [check.get("tail", "").splitlines() for check in ci["checks"]]
        self.assertLessEqual(sum(len(tail) for tail in tails), 400)
        self.assertTrue(all(len(tail) <= 150 for tail in tails))
        self.assertEqual(tails[0][-1], "job 0 line 249")
        self.assertEqual(len(tails[0]), 150)
        self.assertEqual(ci["checks"][-1]["link"], "https://ci.example.test/build/99")
        self.assertEqual(tails[-1], [])
        job_calls = [call for call in calls if call[:2] == ["run", "view"]]
        self.assertGreaterEqual(len(job_calls), 3)
        self.assertIn(["run", "view", "--job", "1", "--log-failed",
                       "--repo", "github.com/fixture/repo"], job_calls)
        self.assertEqual(waits, [])

    def test_ci_failure_logs_select_unittest_failure_and_summary_without_success_lines(self):
        log, failure, summary, progress = self.ci_unittest_failure_fixture()
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [[self.ci_check("fail")]], jobs={"123": log})
        tail = ci["checks"][0]["tail"]
        selected = tail.splitlines()
        for line in failure + summary:
            self.assertIn(line, selected)
        for line in progress:
            self.assertNotIn(line, tail)
        self.assertEqual([line for line in selected if line in failure + summary], failure + summary)
        self.assertLessEqual(len(selected), 150)
        self.assertEqual(ci["outcome"], "failed")
        worker = self.worker()
        self.assertIn(tail, "\n".join(worker.ci_runs.ci_lines(ci)))
        self.assertIn(tail, worker.ci_runs.ci_fix_prompt(st, ci, {"language": "en"}))
        self.assertEqual(waits, [])

    def test_ci_failure_logs_intermediate_summaries_do_not_displace_failure_and_final_summary(self):
        intermediate = ["Ran 1 test in 0.001s"] * 149
        failure = ["=" * 70, "FAIL: test_example (tests.ExampleTests)", "-" * 70,
                   "Traceback (most recent call last):",
                   '  File "example.py", line 12, in test_example',
                   "    self.assertEqual(actual, expected)", "AssertionError: actual != expected"]
        summary = ["Ran 150 tests in 0.123s", "FAILED (failures=1)"]
        for prefix in ("", "tests\tRun tests\t2026-10-19T12:00:00.0000000Z "):
            with self.subTest(prefix=bool(prefix)):
                log = "\n".join(prefix + line for line in intermediate + failure + [""] + summary)
                st, ci, calls, waits = self.run_ci_wait_fixture(
                    [[self.ci_check("fail")]], jobs={"123": log})
                tail = ci["checks"][0]["tail"]
                selected = tail.splitlines()
                expected = [prefix + line for line in failure + summary]
                self.assertTrue(all(line in selected for line in expected))
                self.assertEqual([line for line in selected if line in expected], expected)
                self.assertNotIn(prefix + intermediate[0], selected)
                self.assertLessEqual(len(selected), 150)
                worker = self.worker()
                self.assertIn(tail, "\n".join(worker.ci_runs.ci_lines(ci)))
                self.assertIn(tail, worker.ci_runs.ci_fix_prompt(st, ci, {"language": "en"}))
                self.assertEqual(waits, [])

    def test_ci_failure_logs_error_text_in_exception_keeps_complete_unittest_tracebacks(self):
        class FixtureTests(unittest.TestCase):
            def test_failure(self):
                self.fail("Error: invalid configuration")

        buffer = io.StringIO()
        result = unittest.TextTestRunner(stream=buffer).run(
            unittest.TestSuite(FixtureTests("test_failure") for _ in range(40)))
        self.assertEqual(len(result.failures), 40)
        annotation = ["##[error]unittest failed", "failure detail 1",
                      "failure detail 2", "failure detail 3", ""]
        failure_lines = buffer.getvalue().splitlines()
        summary = [line for line in failure_lines if line.startswith(("Ran ", "FAILED ("))]
        self.assertEqual(len(summary), 2)
        for prefix in ("", "tests\tRun tests\t2026-10-19T12:00:00.0000000Z "):
            with self.subTest(prefix=bool(prefix)):
                log = "\n".join(prefix + line for line in annotation + failure_lines)
                st, ci, calls, waits = self.run_ci_wait_fixture(
                    [[self.ci_check("fail")]], jobs={"123": log})
                tail = ci["checks"][0]["tail"]
                selected = tail.splitlines()
                headings = [line for line in selected if line.startswith(prefix + "FAIL:")]
                assertions = [line for line in selected
                              if line == prefix + "AssertionError: Error: invalid configuration"]
                self.assertGreater(len(headings), 0)
                self.assertLess(len(headings), 40)
                self.assertEqual(len(headings), len(assertions))
                self.assertEqual(len(headings), selected.count(prefix + "Traceback (most recent call last):"))
                self.assertEqual(selected[-4], assertions[-1])
                self.assertEqual(selected[-3:-1], [prefix + line for line in summary])
                self.assertRegex(selected[-1], r"^… \d+ more failure lines omitted$")
                self.assertLessEqual(len(selected), 150)
                worker = self.worker()
                self.assertIn(tail, "\n".join(worker.ci_runs.ci_lines(ci)))
                self.assertIn(tail, worker.ci_runs.ci_fix_prompt(st, ci, {"language": "en"}))
                self.assertEqual(waits, [])

    def test_ci_failure_logs_exception_group_traceback_survives_successful_progress(self):
        for nested in (False, True):
            group_type = getattr(builtins, "ExceptionGroup", None)
            if group_type is not None:
                member = ValueError("fixture group failure")
                if nested:
                    try:
                        raise group_type("nested fixture group", [member])
                    except Exception as inner:
                        member = inner
                buffer = io.StringIO()
                try:
                    raise group_type("fixture group", [member]) from None
                except Exception:
                    traceback.print_exc(file=buffer)
                failure = buffer.getvalue().splitlines()
            else:
                failure = ["  + Exception Group Traceback (most recent call last):",
                           '  |   File "example.py", line 12, in example',
                           "  | ExceptionGroup: fixture group (1 sub-exception)",
                           "  +-+---------------- 1 ----------------"]
                if nested:
                    failure += ["    | Exception Group Traceback (most recent call last):",
                                '    |   File "example.py", line 10, in example',
                                "    | ExceptionGroup: nested fixture group (1 sub-exception)",
                                "    +-+---------------- 1 ----------------",
                                "      | ValueError: fixture group failure",
                                "      +------------------------------------"]
                else:
                    failure += ["    | ValueError: fixture group failure",
                                "    +------------------------------------"]
            self.assertIn("Exception Group Traceback", failure[0])
            self.assertTrue(any("ValueError: fixture group failure" in line for line in failure))
            self.assertEqual(sum("Exception Group Traceback" in line for line in failure),
                             2 if nested else 1)
            for prefix in ("", "tests\tRun tests\t2026-10-19T12:00:00.0000000Z "):
                for count in (1, 40):
                    with self.subTest(nested=nested, prefix=bool(prefix), groups=count):
                        progress = ["test_%03d ... ok" % index for index in range(300)]
                        log = "\n".join(prefix + line for line in failure * count + progress)
                        st, ci, calls, waits = self.run_ci_wait_fixture(
                            [[self.ci_check("fail")]], jobs={"123": log})
                        tail = ci["checks"][0]["tail"]
                        if count == 1:
                            expected = [prefix + line for line in failure]
                        else:
                            retained_count = 149 // len(failure)
                            self.assertGreater(retained_count, 0)
                            self.assertLess(retained_count, count)
                            expected = ([prefix + line for line in failure * retained_count] +
                                        ["… %d more failure lines omitted" %
                                         ((count - retained_count) * len(failure))])
                        self.assertEqual(tail.splitlines(), expected)
                        self.assertLessEqual(len(tail.splitlines()), 150)
                        worker = self.worker()
                        self.assertIn(tail, "\n".join(worker.ci_runs.ci_lines(ci)))
                        self.assertIn(tail, worker.ci_runs.ci_fix_prompt(st, ci, {"language": "en"}))
                        self.assertEqual(waits, [])

    def test_ci_failure_logs_select_pytest_assertion_failed_test_and_summary(self):
        prefix = "pytest\tRun tests\t2026-10-19T12:00:00.0000000Z "
        assertion = prefix + "E   assert 'actual' == 'expected'"
        failed = prefix + "FAILED tests/test_example.py::test_compatibility - AssertionError"
        summary = prefix + "========================= 1 failed, 300 passed in 0.12s ========================="
        progress = [prefix + "tests/test_success.py::test_%03d PASSED" % index for index in range(300)]
        log = "\n".join([prefix + "___________________ test_compatibility ___________________",
                         prefix + "    def test_compatibility():",
                         prefix + ">       assert actual == expected", assertion, prefix] +
                        progress + [failed, summary])
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [[self.ci_check("fail")]], jobs={"123": log})
        selected = ci["checks"][0]["tail"].splitlines()
        self.assertIn(assertion, selected)
        self.assertIn(failed, selected)
        self.assertIn(summary, selected)
        self.assertLess(selected.index(assertion), selected.index(failed))
        self.assertLess(selected.index(failed), selected.index(summary))
        self.assertFalse(any(line in selected for line in progress))
        self.assertEqual(waits, [])

    def test_ci_failure_logs_select_github_error_annotation_without_other_markers(self):
        prefix = "build\tCompile\t2026-10-19T12:00:00.0000000Z "
        error = prefix + "##[error]compiler exited with code 2"
        detail = prefix + "src/example.c:12: unknown identifier"
        progress = [prefix + "compiled object %03d" % index for index in range(300)]
        log = "\n".join([error, detail, prefix] + progress)
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [[self.ci_check("fail")]], jobs={"123": log})
        selected = ci["checks"][0]["tail"].splitlines()
        self.assertIn(error, selected)
        self.assertIn(detail, selected)
        self.assertFalse(any(line in selected for line in progress))
        self.assertEqual(waits, [])

    def test_ci_failure_logs_select_standalone_python_and_tool_error_markers(self):
        markers = ("ERROR: test_import (tests.test_example.Tests)",
                   "Traceback (most recent call last):",
                   "AssertionError: incompatible result", "Error: compilation failed")
        for marker in markers:
            with self.subTest(marker=marker):
                log = ("\n".join(["=" * 70, marker, "failure detail", ""]) + "\n" +
                       "\n".join("successful line %03d" % index for index in range(300)))
                st, ci, calls, waits = self.run_ci_wait_fixture(
                    [[self.ci_check("fail")]], jobs={"123": log})
                selected = ci["checks"][0]["tail"].splitlines()
                self.assertIn(marker, selected)
                self.assertIn("failure detail", selected)
                self.assertFalse(any(line.startswith("successful line ") for line in selected))
                self.assertEqual(waits, [])

    def test_ci_failure_logs_without_markers_keep_the_previous_tail(self):
        lines = ["unrecognized output %03d" % index for index in range(300)]
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [[self.ci_check("fail")]], jobs={"123": "\n".join(lines)})
        self.assertEqual(ci["checks"][0]["tail"].splitlines(), lines[-150:])
        self.assertEqual(waits, [])

    def test_ci_failure_logs_limit_keeps_first_complete_blocks_and_counts_omitted_lines(self):
        log, blocks = self.ci_failure_blocks_fixture(40)
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [[self.ci_check("fail")]], jobs={"123": log})
        selected = ci["checks"][0]["tail"].splitlines()
        self.assertLessEqual(len(selected), 150)
        self.assertRegex(selected[-1], r"^… \d+ more failure lines omitted$")
        retained = selected[:-1]
        self.assertEqual(len(retained) % 4, 0)
        self.assertGreater(len(retained), 0)
        failure_lines = [line for block in blocks for line in block]
        self.assertEqual(retained, failure_lines[:len(retained)])
        self.assertEqual(selected[-1], "… %d more failure lines omitted" %
                         (len(failure_lines) - len(retained)))
        self.assertEqual(waits, [])

    def test_ci_failure_logs_adjacent_blocks_and_oversized_traceback_keep_summaries(self):
        prefix = "tests\tRun tests\t2026-10-19T12:00:00.0000000Z "
        blocks = [[prefix + "##[error]failure %03d" % index] +
                  [prefix + "failure %03d detail %d" % (index, detail) for detail in range(3)]
                  for index in range(40)]
        adjacent_lines = [line for block in blocks for line in block]
        traceback_lines = ([prefix + "Traceback (most recent call last):"] +
                           [prefix + '  File "example.py", line %d, in example' % index
                            for index in range(160)] + [prefix + "AssertionError: final failure"])
        fixtures = (("adjacent", adjacent_lines, 40, "errors=40"),
                    ("oversized", traceback_lines, 1, "failures=1"))
        for kind, failure_lines, test_count, failures in fixtures:
            with self.subTest(kind=kind):
                summary = [prefix + "Ran %d tests in 0.123s" % test_count,
                           prefix + "FAILED (%s)" % failures]
                log = "\n".join(failure_lines + [prefix] + summary)
                st, ci, calls, waits = self.run_ci_wait_fixture(
                    [[self.ci_check("fail")]], jobs={"123": log})
                selected = ci["checks"][0]["tail"].splitlines()
                self.assertLessEqual(len(selected), 150)
                self.assertEqual(selected[-3:-1], summary)
                retained = selected[:-3]
                if kind == "adjacent":
                    self.assertEqual(retained, adjacent_lines[:144])
                else:
                    self.assertEqual(retained, traceback_lines[:147])
                self.assertEqual(selected[-1], "… %d more failure lines omitted" %
                                 (len(failure_lines) - len(retained)))
                self.assertEqual(waits, [])

    def test_ci_failure_logs_oversized_first_traceback_keeps_its_start_and_summaries(self):
        for unittest_header in (False, True):
            header = (["=" * 70, "FAIL: test_example (tests.ExampleTests)", "-" * 70]
                      if unittest_header else [])
            failure_lines = (header + ["Traceback (most recent call last):"] +
                             ['  File "example.py", line %d, in example' % index
                              for index in range(298 - len(header))] +
                             ["AssertionError: final failure"])
            self.assertEqual(len(failure_lines), 300)
            for prefix in ("", "tests\tRun tests\t2026-10-19T12:00:00.0000000Z "):
                with self.subTest(unittest_header=unittest_header, prefix=bool(prefix)):
                    prefixed_lines = [prefix + line for line in failure_lines]
                    summary = [prefix + "Ran 1 tests in 0.123s", prefix + "FAILED (failures=1)"]
                    log = "\n".join(prefixed_lines + [prefix] + summary)
                    st, ci, calls, waits = self.run_ci_wait_fixture(
                        [[self.ci_check("fail")]], jobs={"123": log})
                    tail = ci["checks"][0]["tail"]
                    selected = tail.splitlines()
                    self.assertEqual(selected, prefixed_lines[:147] + summary +
                                     ["… 153 more failure lines omitted"])
                    self.assertEqual(len(selected), 150)
                    self.assertIn(prefix + "Traceback (most recent call last):", selected)
                    if unittest_header:
                        self.assertIn(prefix + "FAIL: test_example (tests.ExampleTests)", selected)
                    worker = self.worker()
                    self.assertIn(tail, "\n".join(worker.ci_runs.ci_lines(ci)))
                    self.assertIn(tail, worker.ci_runs.ci_fix_prompt(st, ci, {"language": "en"}))
                    self.assertEqual(waits, [])

    def test_ci_failure_logs_adjacent_real_tracebacks_keep_complete_first_blocks(self):
        log, blocks = self.ci_adjacent_tracebacks_fixture()
        failure_lines = [line for block in blocks for line in block]
        self.assertEqual(log.splitlines(), failure_lines)
        self.assertFalse(any(not line for line in failure_lines))
        self.assertGreater(len(failure_lines), 150)
        for prefix in ("", "tests\tRun tests\t2026-10-19T12:00:00.0000000Z "):
            with self.subTest(prefix=bool(prefix)):
                prefixed_blocks = [[prefix + line for line in block] for block in blocks]
                prefixed_lines = [line for block in prefixed_blocks for line in block]
                retained, retained_blocks = [], 0
                for block in prefixed_blocks:
                    if len(retained) + len(block) > 149:
                        break
                    retained.extend(block)
                    retained_blocks += 1
                st, ci, calls, waits = self.run_ci_wait_fixture(
                    [[self.ci_check("fail")]], jobs={"123": "\n".join(prefixed_lines)})
                tail = ci["checks"][0]["tail"]
                selected = tail.splitlines()
                self.assertGreater(retained_blocks, 0)
                self.assertLess(retained_blocks, len(blocks))
                self.assertEqual(selected[:-1], prefixed_lines[:len(retained)])
                self.assertEqual(selected[:-1], retained)
                self.assertEqual(selected[-2], prefixed_blocks[retained_blocks - 1][-1])
                self.assertEqual(sum(line == prefix + "Traceback (most recent call last):"
                                     for line in retained), retained_blocks)
                self.assertEqual(sum(line.startswith(prefix + "ValueError: fixture traceback ")
                                     for line in retained), retained_blocks)
                self.assertEqual(selected[-1], "… %d more failure lines omitted" %
                                 (len(prefixed_lines) - len(retained)))
                self.assertLessEqual(len(selected), 150)
                worker = self.worker()
                self.assertIn(tail, "\n".join(worker.ci_runs.ci_lines(ci)))
                self.assertIn(tail, worker.ci_runs.ci_fix_prompt(st, ci, {"language": "en"}))
                self.assertEqual(waits, [])

    def test_ci_failure_logs_unittest_header_stays_with_its_first_real_traceback(self):
        _, tracebacks = self.ci_adjacent_tracebacks_fixture()
        for marker in ("FAIL:", "ERROR:"):
            header = ["=" * 70, marker + " test_unittest (tests.ExampleTests)", "-" * 70]
            before, length = 0, 0
            while length + len(tracebacks[before]) + len(header) <= 149:
                length += len(tracebacks[before])
                before += 1
            for position in (0, before):
                with self.subTest(marker=marker, position=position):
                    blocks = (tracebacks[:position] + [header + tracebacks[position]] +
                              tracebacks[position + 1:])
                    failure_lines = [line for block in blocks for line in block]
                    retained = []
                    for block in blocks:
                        if len(retained) + len(block) > 149:
                            break
                        retained.extend(block)
                    st, ci, calls, waits = self.run_ci_wait_fixture(
                        [[self.ci_check("fail")]], jobs={"123": "\n".join(failure_lines)})
                    tail = ci["checks"][0]["tail"]
                    selected = tail.splitlines()
                    self.assertGreater(len(retained), 0)
                    self.assertEqual(selected[:-1], retained)
                    self.assertEqual(selected[-1], "… %d more failure lines omitted" %
                                     (len(failure_lines) - len(retained)))
                    if position:
                        self.assertFalse(any(line in selected for line in header))
                    else:
                        self.assertEqual(selected[:len(header + tracebacks[0])],
                                         header + tracebacks[0])
                    self.assertTrue(selected[-2].startswith("ValueError: fixture traceback "))
                    self.assertEqual(selected[:-1].count("Traceback (most recent call last):"),
                                     sum(line.startswith("ValueError: fixture traceback ")
                                         for line in selected[:-1]))
                    self.assertLessEqual(len(selected), 150)
                    worker = self.worker()
                    self.assertIn(tail, "\n".join(worker.ci_runs.ci_lines(ci)))
                    self.assertIn(tail, worker.ci_runs.ci_fix_prompt(st, ci, {"language": "en"}))
                    self.assertEqual(waits, [])

    def test_ci_failure_logs_two_failed_jobs_each_keep_their_first_blocks(self):
        checks = [self.ci_check("fail", name="job " + job, job=job) for job in ("123", "456")]
        fixtures = {job: self.ci_failure_blocks_fixture(60, job) for job in ("123", "456")}
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [checks], jobs={job: fixture[0] for job, fixture in fixtures.items()})
        tails = [check["tail"].splitlines() for check in ci["checks"]]
        self.assertLessEqual(sum(map(len, tails)), 300)
        for check, selected, job in zip(ci["checks"], tails, ("123", "456")):
            with self.subTest(job=job):
                self.assertLessEqual(len(selected), 150)
                self.assertGreater(len(selected), 145)
                failure_lines = [line for block in fixtures[job][1] for line in block]
                self.assertEqual(selected[:-1], failure_lines[:len(selected) - 1])
                self.assertEqual((len(selected) - 1) % 4, 0)
                self.assertEqual(selected[-1], "… %d more failure lines omitted" %
                                 (len(failure_lines) - len(selected) + 1))
        self.assertEqual(waits, [])

    def test_ci_failure_logs_three_failed_jobs_share_the_400_line_budget(self):
        jobs = ("123", "456", "789")
        checks = [self.ci_check("fail", name="job " + job, job=job) for job in jobs]
        fixtures = {job: self.ci_failure_blocks_fixture(60, job) for job in jobs}
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [checks], jobs={job: fixture[0] for job, fixture in fixtures.items()})
        tails = [check["tail"].splitlines() for check in ci["checks"]]
        self.assertLessEqual(sum(map(len, tails)), 400)
        self.assertGreater(sum(map(len, tails)), 395)
        self.assertLess(len(tails[2]), len(tails[1]))
        for selected, job in zip(tails, jobs):
            with self.subTest(job=job):
                self.assertLessEqual(len(selected), 150)
                self.assertEqual((len(selected) - 1) % 4, 0)
                failure_lines = [line for block in fixtures[job][1] for line in block]
                self.assertEqual(selected[:-1], failure_lines[:len(selected) - 1])
                self.assertEqual(selected[-1], "… %d more failure lines omitted" %
                                 (len(failure_lines) - len(selected) + 1))
        self.assertEqual(waits, [])

    def test_ci_wait_and_repair_prompt_share_selected_failure_lines(self):
        self.configure(language="en", sandbox="workspace")
        log, failure, summary, progress = self.ci_unittest_failure_fixture()
        self.configure_ci([[self.ci_check("fail")]], jobs={"123": log})
        self.account(self.home, "A", self.reset, runs=[self.ci_work(), self.ci_work("still broken\n")])
        self.start("ci-selected-evidence", "--ci")
        output = self.wait("ci-selected-evidence")
        ci = self.state("ci-selected-evidence")["ci"]
        self.assertEqual((ci["outcome"], ci["rounds"], ci["reason"]), ("failed", 1, "rounds_exhausted"))
        tail = ci["checks"][0]["tail"]
        prompt = self.invocations()[1]["prompt"]
        self.assertIn(tail, output)
        self.assertIn(tail, prompt)
        for line in failure + summary:
            self.assertIn(line, tail)
        for line in progress:
            self.assertNotIn(line, output)
            self.assertNotIn(line, prompt)
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual(len(self.ci_polls()), 2)

    def test_ci_failed_job_logs_use_each_check_repository(self):
        checks = [self.ci_check("fail", name="first job", job="123"),
                  self.ci_check("fail", name="second job", job="456")]
        checks[1]["link"] = checks[1]["link"].replace("fixture/repo", "another-owner/another-repo")
        checks[1]["link"] += "?check_suite_focus=true#step:1:1"
        st, ci, calls, waits = self.run_ci_wait_fixture(
            [checks], jobs={"123": "first repository log", "456": "second repository log"})
        job_calls = [call for call in calls if call[:2] == ["run", "view"]]
        self.assertEqual(job_calls, [
            ["run", "view", "--job", "123", "--log-failed", "--repo", "github.com/fixture/repo"],
            ["run", "view", "--job", "456", "--log-failed", "--repo", "github.com/another-owner/another-repo"]])
        self.assertEqual([check["tail"] for check in ci["checks"]],
                         ["first repository log", "second repository log"])
        self.assertEqual(waits, [])

    def test_ci_failed_job_logs_share_the_wait_deadline(self):
        worker = self.worker()
        checks = [self.ci_check("fail", name="job " + str(index), job=str(index)) for index in range(4)]
        st = {"ci": {"checks": checks}}
        clock = [100.0]

        def timeout(state, args, stopped, timeout=60):
            self.assertIs(state, st)
            self.assertEqual(timeout, 1)
            clock[0] += timeout
            raise subprocess.TimeoutExpired(args, timeout)

        with mock.patch.object(worker.ci_runs.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.ci_runs, "ci_command", side_effect=timeout) as command:
            worker.ci_runs.ci_failure_logs(st, str(self.root / "gh"), lambda: False, deadline=101)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(clock[0], 101)
        tails = [check.get("tail", "").splitlines() for check in checks]
        self.assertTrue(all(tail for tail in tails))
        self.assertTrue(all(len(tail) <= 150 for tail in tails))
        self.assertLessEqual(sum(len(tail) for tail in tails), 400)
        self.assertTrue(all("CI timeout" in check["tail"] for check in checks[1:]))

    def test_workspace_fix_round_commits_before_each_sandboxed_check(self):
        self.configure(language="en", sandbox="workspace")
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Fixed the reported defect."}
                     for finding in self.review_result()["findings"]]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "initial implementation\n"},
             "reply": self.work_result(commit_message="Implement sandbox work")},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "write_files": {"tracked.txt": "corrected implementation\n"},
             "reply": self.work_result(commit_message="Fix sandbox review findings", findings=decisions)},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = 'test -z "$(git status --porcelain)" && git log -1 --format=%s'
        self.start("sandbox-fix", "--review", "--fix-rounds", "1", "--check", command)
        self.wait("sandbox-fix")
        state = self.state("sandbox-fix")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "passed")
        self.assertEqual(self.git("log", "-2", "--format=%s", state["branch"]).splitlines(),
                         ["Fix sandbox review findings", "Implement sandbox work"])
        self.assertEqual(state["publish"]["sha"], self.git("rev-parse", state["branch"]))
        self.assertTrue(state["publish"]["pushed"])
        self.assertEqual(len(self.sandbox_invocations()), 2)
        for event in self.sandbox_invocations() + self.invocations():
            self.assert_workspace_sandbox(event)
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False, True])
        self.assertTrue(self.invocations()[2]["resume"])
        self.assertEqual(len([event for event in self.gh_invocations()
                              if event["argv"][:2] == ["pr", "create"]]), 1)
