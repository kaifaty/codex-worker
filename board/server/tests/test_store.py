import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from teamster_board.store import Store, alive, phase


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
    monkeypatch.setattr("teamster_board.store.time.time", lambda: now)
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
    monkeypatch.setattr("teamster_board.store.time.time", lambda: now)
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
    monkeypatch.setattr("teamster_board.store.time.time", lambda: now[0])
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
    monkeypatch.setattr("teamster_board.store.time.time", lambda: now[0])
    live, _ = pids
    store = Store(tmp_path)
    store.poll()
    now[0] = 1010.0
    write_state(tmp_path, pid=live, ci={"outcome": "pending"})
    store.poll()
    assert store.detail("task")["phase"] == "ci"
    assert store.detail("task")["phase_started_at"] == 1010.0


def write_session(desktop, slug="local_example", **fields):
    directory = desktop / "claude-code-sessions" / "account" / "device"
    directory.mkdir(parents=True, exist_ok=True)
    state = {"sessionId": slug, "cliSessionId": "cli-" + slug, "title": "Session title",
             "cwd": "/tmp/project", "model": "claude-model", "effort": "high",
             "isArchived": False, "createdAt": 1000, "lastActivityAt": time.time() * 1000,
             "completedTurns": 3, "interruptedAwaitingPerson": None, "prs": [], **fields}
    path = directory / (slug + ".json")
    path.write_text(json.dumps(state))
    return path


def write_live(home, pid, session="cli-local_example", **fields):
    directory = home / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    path.write_text(json.dumps({"pid": pid, "sessionId": session, "cwd": "/tmp/project",
                                "status": "busy", "statusUpdatedAt": 2000,
                                "entrypoint": "desktop", **fields}))
    return path


def session_store(tmp_path, **options):
    return Store(tmp_path / "tasks", claude_desktop_dir=tmp_path / "desktop",
                 claude_home=tmp_path / "home", sessions_seconds=0, **options)


@pytest.mark.parametrize("fields,busy,expected", [
    ({"isArchived": True, "interruptedAwaitingPerson": {}}, True, None),
    ({"interruptedAwaitingPerson": {}, "lastActivityAt": 1,
      "prs": [{"state": "MERGED"}]}, True, "running"),
    ({"interruptedAwaitingPerson": False, "lastActivityAt": 1,
      "prs": [{"state": "CLOSED"}]}, False, "attention"),
    ({"prs": [{"state": "MERGED"}, {"state": "CLOSED"}]}, False, "closed"),
    ({"prs": [{"state": "MERGED"}, {"state": "OPEN"}]}, False, "review_me"),
    ({"lastActivityAt": 1}, False, "closed"),
    ({"lastActivityAt": (300000 - 48 * 3600) * 1000}, False, "review_me"),
    ({}, False, "review_me"),
])
def test_session_column_precedence(tmp_path, pids, monkeypatch, fields, busy, expected):
    monkeypatch.setattr("teamster_board.store.time.time", lambda: 300000)
    write_session(tmp_path / "desktop", **fields)
    if busy:
        write_live(tmp_path / "home", pids[0])
    store = session_store(tmp_path)
    store.poll()
    rows = store.summaries()
    if expected is None:
        assert rows == []
        assert store.detail("local_example") is None
    else:
        assert len(rows) == 1
        assert rows[0]["column"] == expected
        assert rows[0]["alive"] is busy
        assert rows[0]["phase"] == ("turn" if busy else "idle")
        assert rows[0]["activity"] is None


@pytest.mark.parametrize("cwd,origin,expected", [
    ("/tmp/project/.claude/worktrees/topic", None, "project"),
    ("/tmp/project", None, "project"),
    ("/tmp/elsewhere/.claude/worktrees/topic", "/tmp/original", "original"),
    (None, None, "(no folder)"),
])
def test_session_repo_and_allowlisted_summary(tmp_path, cwd, origin, expected):
    desktop = tmp_path / "desktop"
    write_session(desktop, cwd=cwd or str(desktop / "sessions" / "scratch"), originCwd=origin,
                  createdAt=1000, lastActivityAt=2000,
                  secret="private metadata must not escape",
                  prs=[{"url": "https://example.test/pr/1", "state": "OPEN", "branch": "own-first", "own": True},
                       {"url": "https://example.test/pr/2", "state": "OPEN", "branch": "own-last", "own": True,
                        "secret": "private PR metadata"},
                       {"url": "https://example.test/pr/3", "state": "OPEN", "branch": "foreign", "own": False}])
    store = session_store(tmp_path)
    store.poll()
    detail = store.detail("local_example")
    assert detail["repo"] == expected
    assert detail["kind"] == "session"
    assert detail["title"] == "Session title"
    assert detail["link"] == "claude://claude.ai/epitaxy/local_example"
    assert detail["executor"] == "claude"
    assert detail["model"] == "claude-model"
    assert detail["effort"] == "high"
    assert detail["turn"] == 3
    assert detail["fix_round"] == 0
    assert detail["branch"] == "own-last"
    assert detail["pr"] == "https://example.test/pr/2"
    assert detail["created"] == 1.0
    assert detail["updated_at"] == detail["phase_started_at"] == 2.0
    assert all(detail[key] is None for key in (
        "reviewer", "account", "task_type", "ci", "outcome", "result_status", "summary", "sleeping_until"))
    assert detail["files"] == []
    assert detail["timeline"] == [{"at": 1.0, "text": "Session created"},
                                    {"at": 2.0, "text": "Last activity"}]
    assert "private" not in json.dumps(detail)
    assert not {"cwd", "originCwd", "cliSessionId", "prs", "secret"} & detail.keys()
    assert store.file_path("local_example", "brief") is None
    write_session(desktop, prs=[{"url": "https://example.test/pr/3", "state": "OPEN", "branch": "foreign"}])
    store.poll()
    assert store.detail("local_example")["pr"] == "https://example.test/pr/3"
    assert store.detail("local_example")["branch"] is None


def test_session_live_state_only_opens_numeric_json_and_checks_pid(tmp_path, pids, monkeypatch):
    import builtins
    import io

    live, dead = pids
    write_session(tmp_path / "desktop")
    home = tmp_path / "home"
    write_live(home, dead)
    key = home / "sessions" / f"{live}.hash.key"
    key.write_text("never read this key")
    foreign = home / "sessions" / "not-a-pid.json"
    foreign.write_text(json.dumps({"pid": live, "sessionId": "cli-local_example", "status": "busy"}))
    blocked = {key, foreign}

    def guarded(original):
        def open_checked(path, *args, **kwargs):
            if not isinstance(path, int):
                assert Path(path) not in blocked, "non-state file must never be opened"
            return original(path, *args, **kwargs)
        return open_checked

    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    monkeypatch.setattr(io, "open", guarded(io.open))
    monkeypatch.setattr(os, "open", guarded(os.open))
    store = session_store(tmp_path)
    store.poll()
    assert store.detail("local_example")["alive"] is False
    write_live(home, live, status="idle")
    store.poll()
    assert store.detail("local_example")["alive"] is False
    write_live(home, live)
    store.poll()
    assert store.detail("local_example")["alive"] is True
    assert store.detail("local_example")["phase_started_at"] == 2.0


def test_session_bad_files_keep_previous_state_and_missing_sources_are_optional(tmp_path, pids, monkeypatch):
    store = session_store(tmp_path)
    assert store.poll() == []
    assert store.summaries() == []
    metadata = write_session(tmp_path / "desktop")
    live = write_live(tmp_path / "home", pids[0])
    store.poll()
    previous = store.summaries()
    metadata.write_text('{"sessionId":')
    live.write_text('{"pid":')
    assert store.poll() == []
    assert store.summaries() == previous
    original_read = Path.read_text

    def no_unchanged_metadata_read(path, *args, **kwargs):
        assert path != metadata, "unchanged malformed metadata should not be read again"
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", no_unchanged_metadata_read)
        assert store.poll() == []
    write_live(tmp_path / "home", pids[0], status="idle")
    assert store.poll()[0]["task"]["column"] == "review_me"
    write_session(tmp_path / "desktop", isArchived=True)
    assert store.poll() == [{"type": "task_removed", "slug": "local_example"}]
    assert store.detail("local_example") is None
    write_session(tmp_path / "desktop")
    store.poll()
    metadata.unlink()
    assert store.poll() == [{"type": "task_removed", "slug": "local_example"}]
    assert store.detail("local_example") is None


def test_session_metadata_scan_cadence_cache_and_archived_memory(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("teamster_board.store.time.monotonic", lambda: clock[0])
    desktop = tmp_path / "desktop"
    metadata = write_session(desktop)
    archived = write_session(desktop, "local_archived", isArchived=True, title="archived payload")
    read = []
    original = json.loads

    def track_loads(raw, *args, **kwargs):
        state = original(raw, *args, **kwargs)
        if isinstance(state, dict) and "sessionId" in state:
            read.append(state["sessionId"])
        return state

    monkeypatch.setattr(json, "loads", track_loads)
    store = Store(tmp_path / "tasks", claude_desktop_dir=desktop,
                  claude_home=tmp_path / "home", sessions_seconds=5)
    store.poll()
    assert sorted(read) == ["local_archived", "local_example"]
    assert "local_archived" not in store.session_records
    assert store._session_signatures[archived] == (archived.stat().st_mtime_ns, archived.stat().st_size)
    clock[0] = 106.0
    store.poll()
    assert len(read) == 2
    metadata.write_text(metadata.read_text().replace("Session title", "Changed title"))
    clock[0] = 107.0
    store.poll()
    assert store.detail("local_example")["title"] == "Session title"
    clock[0] = 112.0
    store.poll()
    assert store.detail("local_example")["title"] == "Changed title"
    assert read.count("local_example") == 2
    assert read.count("local_archived") == 1
    assert archived.is_file()


def test_session_moved_transcript_search_is_cached_and_only_busy_sessions_read(tmp_path, pids, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("teamster_board.store.time.monotonic", lambda: clock[0])
    write_session(tmp_path / "desktop")
    home = tmp_path / "home"
    write_live(home, pids[0], status="idle")
    moved = home / "projects" / "old-folder" / "cli-local_example.jsonl"
    moved.parent.mkdir(parents=True)
    event = {"type": "assistant", "timestamp": "2026-11-05T10:00:00Z",
             "message": {"content": [{"type": "text", "text": "First update"}]}}
    directories = Store._directories
    searches = []

    def count_searches(root):
        if root == home / "projects":
            searches.append(root)
        return directories(root)

    monkeypatch.setattr(Store, "_directories", staticmethod(count_searches))
    store = session_store(tmp_path)
    store.poll()
    assert searches == []
    assert store.detail("local_example")["activity"] is None
    write_live(home, pids[0])
    store.poll()
    assert len(searches) == 1
    assert store.detail("local_example")["activity"] is None
    moved.write_text(json.dumps(event) + "\n")
    clock[0] = 159.0
    store.poll()
    assert len(searches) == 1
    assert store.detail("local_example")["activity"] is None
    clock[0] = 160.0
    store.poll()
    assert len(searches) == 2
    first = store.detail("local_example")["activity"]
    assert first["text"] == "First update"
    assert first["at"] == 1793872800.0
    event["message"]["content"][0]["text"] = "Second update"
    with moved.open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    clock[0] = 220.0
    messages = store.poll()
    assert len(searches) == 2
    assert any(message["type"] == "activity" and message["activity"]["text"] == "Second update"
               for message in messages)
    store.poll()
    assert len(searches) == 2
    write_live(home, pids[0], status="idle")
    store.poll()
    assert store.detail("local_example")["activity"] is None


@pytest.mark.parametrize("initially_exists", [True, False])
def test_session_transcript_direct_path_uses_ascii_and_retries_until_created(tmp_path, pids, monkeypatch,
                                                                         initially_exists):
    write_session(tmp_path / "desktop", cwd="/tmp/ЯA2")
    home = tmp_path / "home"
    write_live(home, pids[0], cwd="/tmp/ЯA2")
    transcript = home / "projects" / "-tmp--A2" / "cli-local_example.jsonl"
    transcript.parent.mkdir(parents=True)
    event = json.dumps({"type": "assistant", "timestamp": "2026-11-05T10:00:00Z",
                        "message": {"content": [{"type": "text", "text": "Current update"}]}}) + "\n"
    if initially_exists:
        transcript.write_text(event)
    directories = Store._directories
    searches = []

    def count_searches(root):
        if root == home / "projects":
            searches.append(root)
        return directories(root)

    monkeypatch.setattr(Store, "_directories", staticmethod(count_searches))
    store = session_store(tmp_path)
    store.poll()
    assert len(searches) == (0 if initially_exists else 1)
    if not initially_exists:
        assert store.detail("local_example")["activity"] is None
        transcript.write_text(event)
        store.poll()
        assert len(searches) == 1
    assert store.detail("local_example")["activity"]["text"] == "Current update"


def test_session_activity_cannot_carry_over_between_busy_turns_or_server_start(tmp_path, pids):
    write_session(tmp_path / "desktop")
    home = tmp_path / "home"
    write_live(home, pids[0], statusUpdatedAt=1793872800000)
    transcript = home / "projects" / "-tmp-project" / "cli-local_example.jsonl"
    transcript.parent.mkdir(parents=True)
    event = {"type": "assistant", "timestamp": "2026-11-05T10:00:00Z",
             "message": {"content": [{"type": "text", "text": "Previous turn"}]}}
    transcript.write_text(json.dumps(event) + "\n")
    store = session_store(tmp_path)
    assert any(message["type"] == "activity" for message in store.poll())
    assert store.detail("local_example")["activity"]["text"] == "Previous turn"
    write_live(home, pids[0], status="idle", statusUpdatedAt=1793872801000)
    store.poll()
    assert store.session_records["local_example"].activity is None
    user = {"type": "user", "timestamp": "2026-11-05T10:00:02Z",
            "message": {"content": "Next request"}}
    with transcript.open("a") as stream:
        stream.write(json.dumps(user) + "\n")
    write_live(home, pids[0], statusUpdatedAt=1793872802000)
    messages = store.poll()
    assert store.detail("local_example")["activity"] is None
    assert not any(message["type"] == "activity" for message in messages)
    event["timestamp"] = "2026-11-05T10:00:03Z"
    event["message"]["content"][0]["text"] = "Current turn"
    with transcript.open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    messages = store.poll()
    assert store.detail("local_example")["activity"]["text"] == "Current turn"
    assert any(message["type"] == "activity" and message["activity"]["text"] == "Current turn"
               for message in messages)
    write_live(home, pids[0], statusUpdatedAt=1793872804000)
    messages = store.poll()
    assert store.detail("local_example")["activity"] is None
    assert not any(message["type"] == "activity" for message in messages)
    restarted = session_store(tmp_path)
    messages = restarted.poll()
    assert restarted.detail("local_example")["alive"] is True
    assert restarted.detail("local_example")["activity"] is None
    assert not any(message["type"] == "activity" for message in messages)


def test_session_symlinks_and_invalid_slug_are_not_read(tmp_path, pids):
    desktop = tmp_path / "desktop"
    metadata = write_session(desktop)
    home = tmp_path / "home"
    live = write_live(home, pids[0])
    live.rename(home / "live.json")
    live.symlink_to(home / "live.json")
    alias = metadata.with_name("local_alias.json")
    alias.symlink_to(metadata)
    write_session(desktop, "local_invalid", sessionId="../x")
    store = session_store(tmp_path)
    store.poll()
    assert len(store.summaries()) == 1
    assert store.detail("local_example")["alive"] is False
    for slug in ("../x", "local_alias", "local_invalid"):
        assert store.detail(slug) is None
    live.unlink()
    write_live(home, pids[0])
    transcript = home / "projects" / "-tmp-project" / "cli-local_example.jsonl"
    transcript.parent.mkdir(parents=True)
    outside = home / "transcript.jsonl"
    outside.write_text(json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "Must not be read"}]}}) + "\n")
    transcript.symlink_to(outside)
    store.poll()
    assert store.detail("local_example")["alive"] is True
    assert store.detail("local_example")["activity"] is None
    transcript.unlink()
    project = transcript.parent
    project.rename(home / "outside-project")
    (home / "outside-project" / transcript.name).write_text(outside.read_text())
    project.symlink_to(home / "outside-project", target_is_directory=True)
    store.poll()
    assert store.detail("local_example")["activity"] is None
    metadata.rename(desktop / "metadata.json")
    metadata.symlink_to(desktop / "metadata.json")
    store.poll()
    assert store.summaries() == []


def test_session_closed_limit_is_shared_with_tasks_and_custom_staleness(tmp_path, monkeypatch):
    now = 300000.0
    monkeypatch.setattr("teamster_board.store.time.time", lambda: now)
    desktop = tmp_path / "desktop"
    for index in range(30):
        task = write_state(tmp_path / "tasks" / ".archive", f"archived-{index:02}")
        os.utime(task / "state.json", (100 + index, 100 + index))
        write_session(desktop, f"local_closed-{index:02}", lastActivityAt=(200 + index) * 1000)
    write_session(desktop, "local_recent", lastActivityAt=(now - 3600) * 1000)
    store = session_store(tmp_path)
    store.poll()
    rows = store.summaries()
    assert len(rows) == 51
    closed = {row["slug"] for row in rows if row["column"] == "closed"}
    assert closed == ({f"archived-{index:02}" for index in range(10, 30)} |
                      {f"local_closed-{index:02}" for index in range(30)})
    assert store.detail("local_recent")["column"] == "review_me"
    custom = session_store(tmp_path, session_stale_hours=0.5)
    custom.poll()
    assert custom.detail("local_recent")["column"] == "closed"
