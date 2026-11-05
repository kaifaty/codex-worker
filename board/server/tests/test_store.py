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
    assert "newest-20261105T120000Z" not in {task["slug"] for task in store.summaries()}
    assert "old-05" in {task["slug"] for task in store.summaries()}
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


def test_finished_scouts_are_closed_for_success_failure_and_abandoned_work(tmp_path, pids):
    live, dead = pids
    for outcome in ("completed", "failed", "running", "sleeping"):
        write_state(tmp_path, outcome, kind="scout", pid=dead, outcome=outcome)
    write_state(tmp_path, "alive-scout", kind="scout", pid=live)
    store = Store(tmp_path)
    store.poll()
    rows = {task["slug"]: task for task in store.summaries()}
    assert {slug: row["column"] for slug, row in rows.items()} == {
        "completed": "closed", "failed": "closed", "running": "closed", "sleeping": "closed",
        "alive-scout": "running",
    }


def test_stale_tasks_close_by_state_mtime_with_live_exemption_and_configurable_threshold(tmp_path, pids, monkeypatch):
    now = 20 * 86400.0
    monkeypatch.setattr("codex_worker_board.store.time.time", lambda: now)
    live, dead = pids
    for slug, pid, age in (("stale", dead, 7 * 86400 + 1), ("boundary", dead, 7 * 86400),
                           ("recent", dead, 2 * 86400), ("alive", live, 8 * 86400)):
        directory = write_state(tmp_path, slug, pid=pid, outcome="completed", result={"status": "done"})
        os.utime(directory / "state.json", (now - age, now - age))
    store = Store(tmp_path)
    store.poll()
    assert {task["slug"]: task["column"] for task in store.summaries()} == {
        "stale": "closed", "boundary": "review_me", "recent": "review_me", "alive": "running",
    }
    custom = Store(tmp_path, stale_days=1)
    custom.poll()
    assert custom.detail("recent")["column"] == "closed"
    assert custom.detail("alive")["column"] == "running"


def test_snapshot_closed_limit_combines_archive_and_live_folders_by_updated_at(tmp_path, pids, monkeypatch):
    now = 20 * 86400.0
    monkeypatch.setattr("codex_worker_board.store.time.time", lambda: now)
    live, dead = pids
    for i in range(30):
        archived = write_state(tmp_path / ".archive", f"archived-{i:02}", pid=dead)
        archived_at = now - 1000 + i
        os.utime(archived / "state.json", (archived_at, archived_at))
        scout = write_state(tmp_path, f"scout-{i:02}", kind="scout", pid=dead)
        scout_at = now - 970 + i
        os.utime(scout / "state.json", (scout_at, scout_at))
    stale = write_state(tmp_path, "stale-work", pid=dead)
    os.utime(stale / "state.json", (now - 8 * 86400, now - 8 * 86400))
    running = write_state(tmp_path, "running", pid=live)
    os.utime(running / "state.json", (now - 8 * 86400, now - 8 * 86400))
    attention = write_state(tmp_path, "attention", pid=dead)
    os.utime(attention / "state.json", (now, now))
    store = Store(tmp_path)
    store.poll()
    rows = store.summaries()
    closed = [task for task in rows if task["column"] == "closed"]
    assert len(rows) == 52
    assert len(closed) == 50
    assert {task["slug"] for task in closed} == (
        {f"archived-{i:02}" for i in range(10, 30)} | {f"scout-{i:02}" for i in range(30)}
    )
    assert {task["slug"] for task in rows if task["column"] != "closed"} == {"running", "attention"}
    assert store.detail("stale-work")["column"] == "closed"
    assert store.detail("archived-00")["column"] == "closed"


@pytest.mark.parametrize("executor,task_model,cursor_model,expected", [
    ("codex", "gpt-6.1-sol", "claude-opus-5-5", "gpt-6.1-sol"),
    ("codex", None, "claude-opus-5-5", None),
    ("claude", "claude-opus-5-5", "gpt-6.1-sol", "claude-opus-5-5"),
    ("claude", None, "claude-opus-5-5", "claude-opus-5-5"),
])
def test_model_always_belongs_to_executor_including_during_review(tmp_path, pids, executor, task_model, cursor_model, expected):
    live, _ = pids
    for kind in ("work", "review"):
        write_state(tmp_path, kind, kind=kind, pid=live, executor=executor,
                    task_model=task_model, model=cursor_model, review_model="reviewer-only-model",
                    reviews=[{"started": 30, "finished": None}])
    store = Store(tmp_path)
    store.poll()
    assert store.detail("work")["model"] == expected
    assert store.detail("review")["model"] == expected
    assert store.detail("review")["phase"] == "review"


@pytest.mark.parametrize("fields,expected_phase", [
    ({"outcome": "completed", "check_pid": 123}, "check"),
    ({"ci": {"outcome": "pending"}}, "ci"),
    ({"rounds": [{}]}, "fix"),
])
def test_phase_observation_timer_preserves_unknown_start_and_resets_on_phase_change(tmp_path, pids, monkeypatch, fields, expected_phase):
    now = [1000.0]
    monkeypatch.setattr("codex_worker_board.store.time.time", lambda: now[0])
    live, _ = pids
    write_state(tmp_path, pid=live, **fields)
    store = Store(tmp_path)
    store.poll()
    assert store.detail("task")["phase"] == expected_phase
    assert store.detail("task")["phase_started_at"] is None
    now[0] = 1010.0
    store.poll()
    assert store.detail("task")["phase_started_at"] is None

    write_state(tmp_path, pid=live)
    store.poll()
    assert store.detail("task")["phase"] == "turn"
    assert store.detail("task")["phase_started_at"] == 20.0
    now[0] = 1020.0
    write_state(tmp_path, pid=live, **fields)
    store.poll()
    assert store.detail("task")["phase_started_at"] == 1020.0
    now[0] = 1030.0
    store.poll()
    assert store.detail("task")["phase_started_at"] == 1020.0

    write_state(tmp_path, pid=live)
    store.poll()
    now[0] = 1040.0
    write_state(tmp_path, pid=live, **fields)
    store.poll()
    assert store.detail("task")["phase_started_at"] == 1040.0
    write_state(tmp_path, pid=live, kind="review", reviews=[{"started": 900.0, "finished": None}])
    store.poll()
    assert store.detail("task")["phase_started_at"] == 900.0


def test_phase_observation_timer_starts_for_task_appearing_after_first_poll(tmp_path, pids, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("codex_worker_board.store.time.time", lambda: now[0])
    live, _ = pids
    store = Store(tmp_path)
    store.poll()
    now[0] = 1010.0
    write_state(tmp_path, pid=live, ci={"outcome": "pending"})
    store.poll()
    assert store.detail("task")["phase"] == "ci"
    assert store.detail("task")["phase_started_at"] == 1010.0
