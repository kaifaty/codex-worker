"""Keep the flat task-state registry complete and its lifecycle behavior stable."""
import ast
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_loader import load_worker


SCRIPT = (Path(__file__).resolve().parents[1]
          / "plugins/codex-worker/skills/codex-worker/scripts/codex_task.py")


def literal_mapping_keys(node):
    """Yield only root keys from literal mappings, including literal unpacking."""
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if key is None:
                yield from literal_mapping_keys(value)
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                yield key.value
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
          and node.func.id == "dict"):
        for argument in node.args:
            yield from literal_mapping_keys(argument)
        for keyword in node.keywords:
            if keyword.arg is None:
                yield from literal_mapping_keys(keyword.value)
            else:
                yield keyword.arg


def state_root_keys(tree):
    """Inventory direct ``st`` accesses without counting nested object fields."""
    keys = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "st" and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "st"):
            if node.func.attr in {"get", "pop", "setdefault"} and node.args:
                key = node.args[0]
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
            elif node.func.attr == "update":
                for argument in node.args:
                    keys.update(literal_mapping_keys(argument))
                for keyword in node.keywords:
                    if keyword.arg is None:
                        keys.update(literal_mapping_keys(keyword.value))
                    else:
                        keys.add(keyword.arg)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "st" for target in targets):
                keys.update(literal_mapping_keys(node.value))
    return keys


class StateFieldRegistryTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="codex-state-fields-")
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        self.worker = load_worker({"CODEX_TASK_HOME": home.name}, name="state_fields_worker")
        self.directory = self.worker.config.TASKS / "registry-test"
        self.directory.mkdir(parents=True)

    def test_ast_inventory_covers_literal_accesses_and_excludes_nested_keys(self):
        tree = ast.parse('''
st = {"initial": {"nested_initial": 1}}
st: dict = dict(annotated=1)
st["read"]
st["write"] = 1
del st["delete"]
st["outer"]["nested_subscript"] = 1
st.get("get")
st.pop("pop", None)
st.setdefault("setdefault", {"nested_default": 1})
st.update({"mapping": {"nested_update": 1}, **{"unpacked_mapping": 1}},
          keyword=1, **{"unpacked_keyword": 1})
st.update(dict(constructor=1))
other["not_state"] = 1
st[dynamic_key] = 1
''')
        self.assertEqual(state_root_keys(tree), {
            "initial", "annotated", "read", "write", "delete", "outer", "get", "pop",
            "setdefault", "mapping", "unpacked_mapping", "keyword", "unpacked_keyword",
            "constructor",
        })

    def test_registry_matches_every_literal_root_state_key(self):
        sources = [SCRIPT] + sorted(SCRIPT.with_name("codex_worker").glob("*.py"))
        used = set().union(*(state_root_keys(ast.parse(path.read_text(encoding="utf-8")))
                             for path in sources))
        registered = set(self.worker.task_state.STATE_FIELDS)
        missing = used - registered
        unused = registered - used
        self.assertFalse(missing, "Unregistered state keys: " + ", ".join(sorted(missing)))
        self.assertFalse(unused, "Unused state registry keys: " + ", ".join(sorted(unused)))
        self.assertEqual(len(registered), 73)
        for key, field in self.worker.task_state.STATE_FIELDS.items():
            with self.subTest(key=key):
                self.assertIn(field.lifetime, {"task", "history", "cursor", "turn", "attempt", "runtime"})

    def complete_state(self):
        st = {
            "account": "old-work", "account_ids": {"old-work": "old-identity"},
            "auth_excluded": ["old-work"], "auth_excluded_ids": ["old-identity"],
            "auth_failures": {"old-work": 2}, "auth_transitions": 2,
            "auto_fix_escalations": [{"id": "F1"}], "auto_fix_stop": "old-stop",
            "base": "origin/main", "base_check": True, "branch": "codex/registry-test",
            "capacity_retries": 2,
            "check": "python3 check.py", "check_paths": ["check.py"], "check_pid": 101,
            "check_start": 19, "check_timeout": 42, "checks": [{}, {}, {}],
            "ci": {"status": "failed"}, "ci_enabled": True, "ci_rounds": 3,
            "ci_timeout_minutes": 17, "codex_pid": 102, "created": 1000,
            "effort": "high", "error": "old-error", "event_start": 18,
            "exec_event_start": 6, "exhausted": {"old-work": {}}, "fix_rounds": 9,
            "guards": ["protected/**"], "kind": "work", "max_task_weekly_percent": 30,
            "max_turn_minutes": 11, "needs_login": ["old-work"], "outcome": "done",
            "pending": {"prompt": "old-prompt"}, "pid": 103,
            "publish": {"pr": "https://example.invalid/pull/1"}, "refusal": "old-refusal",
            "repo": str(self.home), "reset_reconciled": True, "reset_wait_seconds": 15,
            "result": {"status": "done", "summary": "old-result"},
            "review_account": "old-review", "review_angle": "correctness",
            "review_angles": ["tests"], "review_result": {"verdict": "clean"},
            "reviewer": "claude",
            "review_skipped": "old-skip", "review_start": 17, "reviews": [{}, {}],
            "rotation_start": 16, "rotations": [{}, {}, {}, {}], "rounds": [{"round": 1}],
            "sandbox": "workspace", "signal": {"name": "SIGTERM", "at": 1001},
            "sleeping": {"until": 2000}, "slug": "registry-test",
            "stop_request": {"pid": 103}, "thread": "old-thread",
            "threads": [{"account": "old-work", "thread": "old-thread", "started": 1000}],
            "turn_finished": 1002, "turn_started": 1001, "turn_stop": {"reason": "budget"},
            "turns": 11, "weekly_budget_warning": {"used_percent": 20},
            "weekly_marks": {"old-work": {"used_percent": 10}}, "weekly_used_percent": 20,
            "work_completed": True, "worktree": str(self.home), "wrapup": {"status": "done"},
            "writable_dirs": [str(self.home / "writable")],
        }
        self.assertEqual(set(st), set(self.worker.task_state.STATE_FIELDS))
        return st

    def spawn(self, st, kind):
        with mock.patch.object(self.worker.supervisor.subprocess, "Popen") as popen, \
                mock.patch.object(self.worker.clock.time, "time", return_value=1234567), \
                mock.patch.object(self.worker.threads, "events", return_value=[{}] * 5), \
                mock.patch.object(self.worker.task_state, "save") as save:
            popen.return_value.pid = 200
            self.worker.supervisor.spawn(st, "new-prompt", effort="medium", current_message=True,
                              kind=kind, review_after=True, fix_rounds=4)
            popen.assert_called_once()
            save.assert_called_once_with(st)

    def test_spawn_resets_exactly_the_marked_registry_fields(self):
        st = self.complete_state()
        before = copy.deepcopy(st)
        self.spawn(st, "review")
        marked = {key for key, field in self.worker.task_state.STATE_FIELDS.items()
                  if field.spawn_reset is not None}
        changed = {key for key in before if key not in st or st[key] != before[key]}
        self.assertEqual(changed - {"pid"}, marked)
        self.assertEqual(set(st) - set(before), set())
        deleted = {"sleeping", "turn_finished", "error", "review_skipped", "auto_fix_stop",
                   "auto_fix_escalations", "turn_stop", "wrapup", "review_result",
                   "review_angle", "refusal", "stop_request", "signal", "ci", "exec_event_start"}
        self.assertEqual(set(before) - set(st), deleted)
        expected = {
            "pending": {"prompt": "new-prompt", "effort": "medium", "current_message": True,
                        "review_after": True},
            "kind": "review", "work_completed": False, "review_start": 2,
            "turn_started": 1234567, "check_start": 3, "turns": 12,
            "rotation_start": 4, "event_start": 5, "exhausted": None,
            "outcome": "running", "reset_wait_seconds": 0, "capacity_retries": 0,
            "fix_rounds": 4, "rounds": [],
        }
        self.assertEqual(marked, deleted | set(expected))
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(st[key], value)
        self.assertEqual(st["pid"], 200)
        for key in {"review_account", "publish", "result"}:
            with self.subTest(preserved=key):
                self.assertEqual(st[key], before[key])

    def test_work_and_scout_also_clear_the_previous_work_receipt(self):
        for kind in ("work", "scout"):
            with self.subTest(kind=kind):
                st = self.complete_state()
                before = copy.deepcopy(st)
                self.spawn(st, kind)
                marked = {key for key, field in self.worker.task_state.STATE_FIELDS.items()
                          if field.spawn_reset is not None}
                changed = {key for key in before if key not in st or st[key] != before[key]}
                # kind already equals work in the fixture, so it need not change.
                self.assertEqual(changed - {"pid", "publish", "result"},
                                 marked - ({"kind"} if kind == "work" else set()))
                self.assertNotIn("publish", st)
                self.assertIsNone(st["result"])

    def load_state(self, state):
        self.worker.task_state.state_path("registry-test").write_text(json.dumps(state), encoding="utf-8")
        return self.worker.task_state.load("registry-test")

    def test_load_keeps_the_same_defaults_and_insertion_order(self):
        expected = {"account": "main", "threads": [], "rotations": [], "reviews": [],
                    "checks": [], "result": None, "fix_rounds": 0, "rounds": [],
                    "guards": [], "base_check": False, "sandbox": "off", "writable_dirs": [],
                    "reviewer": "codex"}
        loaded = self.load_state({})
        self.assertEqual(loaded, expected)
        self.assertEqual(list(loaded), list(expected))
        first = loaded
        second = self.load_state({})
        for key, value in expected.items():
            if isinstance(value, list):
                with self.subTest(key=key):
                    first[key].append("changed")
                    self.assertEqual(second[key], [])

    def test_load_preserves_present_values(self):
        st = self.complete_state()
        self.assertEqual(self.load_state(st), st)
        present = {key: None for key, field in self.worker.task_state.STATE_FIELDS.items()
                   if field.load_default is not None}
        self.assertEqual(self.load_state(present), present)

    def test_load_reconstructs_legacy_thread_history(self):
        for state, expected in (
                ({"thread": "legacy-thread", "created": 42},
                 [{"account": "main", "thread": "legacy-thread", "started": 42}]),
                ({"account": "other", "thread": "legacy-thread"},
                 [{"account": "other", "thread": "legacy-thread", "started": 0}]),
                ({"thread": ""}, [])):
            with self.subTest(state=state):
                self.assertEqual(self.load_state(state)["threads"], expected)


if __name__ == "__main__":
    unittest.main()
