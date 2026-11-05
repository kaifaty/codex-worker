import json
import os
import subprocess
from pathlib import Path

import pytest

from codex_worker_board.store import Store, alive, phase


@pytest.fixture
def pids():
    running = subprocess.Popen(["sleep", "60"])
    finished = subprocess.Popen(["sleep", "0"])
    finished.wait(timeout=5)
    try:
        yield running.pid, finished.pid
    finally:
        running.terminate()
        running.wait(timeout=5)


def write_state(tasks, slug="task", **fields):
    directory = tasks / slug
    directory.mkdir(parents=True, exist_ok=True)
    state = {"slug": slug, "kind": "work", "repo": "/tmp/project", "executor": "codex",
             "account": "main", "outcome": "running", "turns": 2, "created": 10,
             "turn_started": 20, **fields}
    (directory / "state.json").write_text(json.dumps(state))
    return directory


@pytest.mark.parametrize("state,expected", [
    ({"outcome": "running"}, ("turn", 20.0)),
    ({"outcome": "completed", "check_pid": 123}, ("check", None)),
    ({"kind": "review", "reviews": [{"started": 30, "finished": None}]}, ("review", 30.0)),
    ({"rounds": [{}], "outcome": "running"}, ("fix", None)),
    ({"ci": {"outcome": "pending"}}, ("ci", None)),
    ({"outcome": "sleeping", "rotations": [{"event": "sleep", "at": 40}]}, ("sleeping", 40.0)),
    ({"outcome": "completed", "turn_finished": 50}, ("idle", 50.0)),
])
def test_each_phase(state, expected):
    assert phase(dict(turn_started=20, **state), True) == expected
    assert phase(state, False)[0] == "idle"


def test_phase_priorities_and_ci_repair():
    assert phase({"outcome": "running", "ci": {"outcome": "failed", "rounds": 1}}, True)[0] == "fix"
    assert phase({"outcome": "running", "kind": "review", "rounds": [{}]}, True)[0] == "review"
    assert phase({"outcome": "completed", "kind": "review", "check_pid": 1}, True)[0] == "check"
    assert phase({"outcome": "sleeping", "kind": "review", "ci": {"outcome": "pending"}}, True)[0] == "sleeping"


def test_each_column_and_actual_supervisor_liveness(tmp_path, pids):
    live, dead = pids
    tasks = tmp_path / "claude-tasks"
    write_state(tasks, "running", pid=live)
    write_state(tasks, "sleeping", pid=live, outcome="sleeping", sleeping={"until": 100})
    write_state(tasks, "review", pid=dead, outcome="completed", result={"status": "done"})
    write_state(tasks, "attention", pid=dead)
    write_state(tasks / ".archive", "old-123", pid=live, outcome="completed", result={"status": "done"})
    store = Store(tasks)
    store.poll()
    rows = {task["slug"]: task for task in store.summaries()}
    assert {slug: row["column"] for slug, row in rows.items()} == {
        "running": "running", "sleeping": "sleeping", "review": "review_me",
        "attention": "attention", "old-123": "closed"}
    assert rows["running"]["alive"] is True
    assert rows["attention"]["alive"] is False
    assert rows["old-123"]["alive"] is True
    assert rows["sleeping"]["sleeping_until"] == 100.0


@pytest.mark.parametrize("outcome,status", [
    ("sleeping", "done"), ("failed", "done"), ("stopped", "done"),
    ("budget_exhausted", "done"), ("conflict", "done"), ("completed", "partial"),
    ("completed", "needs_decision"), ("completed", "blocked"),
])
def test_dead_tasks_needing_attention(tmp_path, outcome, status):
    write_state(tmp_path, outcome=outcome, result={"status": status})
    store = Store(tmp_path)
    store.poll()
    assert store.summaries()[0]["column"] == "attention"


def test_permission_error_means_alive(monkeypatch):
    def denied(pid, signal):
        assert (pid, signal) == (123, 0)
        raise PermissionError
    monkeypatch.setattr(os, "kill", denied)
    assert alive(123) is True
    assert alive(None) is False
    assert alive(0) is False


def test_bad_state_preserves_cache_then_recovers(tmp_path, monkeypatch):
    directory = write_state(tmp_path, result={"status": "done", "summary": "first line\nsecond"})
    store = Store(tmp_path)
    assert len(store.poll()) == 1
    original = store.summaries()
    state_path = directory / "state.json"
    original_read = Path.read_text
    def no_state_read(path, *args, **kwargs):
        assert path != state_path, "unchanged state should not be read again"
        return original_read(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", no_state_read)
        assert store.poll() == []
    state_path.write_text('{"outcome":')
    assert store.poll() == []
    assert store.summaries() == original
    write_state(tmp_path, outcome="completed", result={"status": "done", "summary": "new"})
    assert store.poll()[0]["task"]["summary"] == "new"


def test_archive_cap_and_removed_message(tmp_path):
    live = write_state(tmp_path)
    for i in range(55):
        directory = write_state(tmp_path / ".archive", f"old-{i:02}")
        os.utime(directory / "state.json", (100 + i, 100 + i))
    store = Store(tmp_path)
    store.poll()
    assert len(store.summaries()) == 51
    newest = write_state(tmp_path / ".archive", "newest-20261105T120000Z")
    os.utime(newest / "state.json", (1, 1))
    store.poll()
    assert len(store.summaries()) == 51
    assert store.detail("newest-20261105T120000Z")["column"] == "closed"
    assert "old-05" not in {task["slug"] for task in store.summaries()}
    assert "old-00" not in {task["slug"] for task in store.summaries()}
    old = tmp_path / ".archive" / "old-00"
    (old / "brief.md").write_text("Archived brief")
    assert store.detail("old-00")["column"] == "closed"
    assert store.detail("old-00")["files"] == ["brief"]
    assert store.file_path("old-00", "brief") == old / "brief.md"
    assert len(store.summaries()) == 51
    assert store.detail("old-54")["slug"] == "old-54"
    assert store.detail("old-54")["column"] == "closed"
    (live / "state.json").unlink()
    live.rmdir()
    assert store.poll() == [{"type": "task_removed", "slug": "task"}]


def test_details_and_file_allowlist(tmp_path):
    directory = write_state(tmp_path, executor="claude", claude_account="pool", task_model="opus",
                            reasoning_effort="high", publish={"pr": "https://example.test/pr/1"},
                            rounds=[{}], ci={"outcome": "failed", "rounds": 2}, checks=[{"at": i, "outcome": "passed", "cmd": "x" * 250}
                                               for i in range(250)])
    (directory / "brief.md").write_text("brief")
    (directory / "last.md").write_text("last")
    (directory / "auth.json").write_text("must never be read")
    (directory / "review.md").symlink_to(directory / "auth.json")
    (tmp_path / "escape").symlink_to(directory, target_is_directory=True)
    store = Store(tmp_path)
    store.poll()
    detail = store.detail("task")
    assert detail["files"] == ["brief", "last"]
    assert len(detail["timeline"]) == 200
    assert all(len(entry["text"]) <= 200 and "\n" not in entry["text"] for entry in detail["timeline"])
    assert detail["account"] == "pool"
    assert detail["model"] == "opus"
    assert detail["effort"] == "high"
    assert detail["repo"] == "project"
    assert detail["fix_round"] == 3
    assert detail["pr"] == "https://example.test/pr/1"
    assert store.detail("escape") is None
    assert store.detail("unknown") is None
    for name in ("../state.json", "state.json", "auth.json", "review", "missing"):
        assert store.file_path("task", name) is None
    assert store.file_path("task", "brief") == directory / "brief.md"
