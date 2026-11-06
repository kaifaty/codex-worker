"""Contract checks through the REST and WebSocket interfaces."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from codex_worker_board import app as board_app
from codex_worker_board.app import Settings, create_app


SUMMARY_FIELDS = {
    "slug", "kind", "repo", "branch", "task_type", "executor", "account", "reviewer",
    "model", "effort", "column", "phase", "phase_started_at", "alive", "turn", "fix_round",
    "outcome", "result_status", "summary", "pr", "ci", "sleeping_until", "created", "updated_at",
    "activity", "title", "link",
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TASK_HOME", str(tmp_path))
    monkeypatch.setattr(board_app, "PING_SECONDS", 0.05)
    fake = tmp_path / "accounts.py"
    fake.write_text(
        "import sys\nassert sys.flags.isolated\n"
        "assert sys.argv[1:] == ['accounts', '--json']\nprint('{\"accounts\": []}')\n"
    )
    settings = Settings(tmp_path, fake, poll_seconds=0.01, web_dist=tmp_path / "dist")
    with subprocess.Popen(["sleep", "30"]) as process:
        directory = tmp_path / "claude-tasks" / "demo"
        directory.mkdir(parents=True)
        state = {
            "slug": "demo", "kind": "work", "repo": "/tmp/repository", "branch": "demo-branch",
            "executor": "codex", "account": "main", "reviewer": "claude", "pid": process.pid,
            "outcome": "running", "turns": 1, "turn_started": 10.0, "created": 1.0,
        }
        (directory / "state.json").write_text(json.dumps(state))
        application = create_app(settings)
        try:
            with TestClient(application, base_url="http://localhost", headers={"Host": "localhost"}) as client:
                yield client, application, directory, state, settings
        finally:
            process.terminate()


def receive_until(socket, wanted):
    for _ in range(50):
        message = socket.receive_json()
        if message["type"] == wanted:
            return message
    pytest.fail(f"No {wanted} message received")


def test_stale_days_environment_reaches_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TASK_HOME", str(tmp_path))
    monkeypatch.setenv("BOARD_CODEX_TASK", str(tmp_path / "fake_accounts.py"))
    monkeypatch.delenv("BOARD_STALE_DAYS", raising=False)
    assert Settings.from_env().stale_days == 7
    monkeypatch.setenv("BOARD_STALE_DAYS", "2.5")
    settings = Settings.from_env()
    assert settings.stale_days == 2.5
    assert create_app(settings).state.board.store.stale_days == 2.5


def test_rest_contract_files_and_unknown_paths(board):
    client, _, directory, _, _ = board
    snapshot = client.get("/api/snapshot").json()
    assert set(snapshot) == {"generated_at", "tasks", "accounts", "machines"}
    assert snapshot["machines"] == {"config_error": None, "items": []}
    assert isinstance(snapshot["generated_at"], float)
    assert len(snapshot["tasks"]) == 1
    assert set(snapshot["tasks"][0]) == SUMMARY_FIELDS
    assert snapshot["tasks"][0]["title"] is None
    assert snapshot["tasks"][0]["link"] is None
    assert set(snapshot["accounts"]) == {"fetched_at", "error", "data"}
    for name, filename in (("brief", "brief.md"), ("last", "last.md"),
                           ("review", "review.md"), ("check", "check.log")):
        content = "discard this prefix" + "x" * (256 * 1024)
        (directory / filename).write_text(content)
        response = client.get(f"/api/tasks/demo/files/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert response.content == b"x" * (256 * 1024)
    detail = client.get("/api/tasks/demo").json()
    assert set(detail) == SUMMARY_FIELDS | {"timeline", "files"}
    assert set(detail["files"]) == {"brief", "last", "review", "check"}
    assert len(detail["timeline"]) <= 200
    (directory / "last.md").unlink()
    for path in (
        "/api/tasks/unknown", "/api/tasks/unknown/files/brief", "/api/tasks/demo/files/last",
        "/api/tasks/demo/files/state.json", "/api/tasks/demo/files/auth.json",
        "/api/tasks/demo/files/../state.json", "/api/tasks/demo/files/%2e%2e%2fstate.json",
        "/api/tasks/%2e%2e%2fstate.json", "/api/tasks/demo/files/brief/other",
    ):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1:4321", "[::1]:8787"])
def test_local_hosts_and_origins_are_allowed(board, host):
    client, _, _, _, _ = board
    assert client.get("/api/snapshot", headers={"Host": host}).status_code == 200
    with client.websocket_connect("/ws", headers={"Host": host, "Origin": f"http://{host}"}) as socket:
        assert socket.receive_json()["type"] == "snapshot"


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil", "127.0.0.1.evil:8787"])
def test_foreign_hosts_are_forbidden(board, host):
    client, _, _, _, _ = board
    assert client.get("/api/snapshot", headers={"Host": host}).status_code == 403
    with pytest.raises(WebSocketDenialResponse) as rejected:
        with client.websocket_connect("/ws", headers={"Host": host}):
            pass
    assert rejected.value.status_code == 403


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost.evil", "null"])
def test_foreign_websocket_origins_are_forbidden(board, origin):
    client, _, _, _, _ = board
    with pytest.raises(WebSocketDenialResponse) as rejected:
        with client.websocket_connect("/ws", headers={"Origin": origin}):
            pass
    assert rejected.value.status_code == 403


def test_websocket_snapshot_then_task_activity_and_archive(board):
    client, _, directory, state, _ = board
    with client.websocket_connect("/ws") as socket:
        first = socket.receive_json()
        assert first["type"] == "snapshot"
        assert first["snapshot"]["tasks"][0]["slug"] == "demo"
        state["turns"] = 2
        (directory / "state.json").write_text(json.dumps(state))
        changed = receive_until(socket, "task")
        assert changed["task"]["turn"] == 2
        event = {"type": "item.started", "item": {"type": "command_execution", "command": "git status"}}
        events = directory / "events.jsonl"
        events.write_text(json.dumps(event) + "\n")
        activity = receive_until(socket, "activity")
        assert activity == {"type": "activity", "slug": "demo",
                            "activity": {"at": events.stat().st_mtime, "kind": "command", "text": "git status"}}
        snapshot = client.get("/api/snapshot").json()
        assert snapshot["tasks"][0]["activity"] == activity["activity"]
        archived = directory.parent / ".archive" / "demo-20261105"
        archived.parent.mkdir()
        directory.rename(archived)
        removed = receive_until(socket, "task_removed")
        assert removed == {"type": "task_removed", "slug": "demo"}
        closed = receive_until(socket, "task")
        assert closed["task"]["slug"] == archived.name
        assert closed["task"]["column"] == "closed"
        assert client.get(f"/api/tasks/{archived.name}").status_code == 200


def test_websocket_ignores_messages_and_sends_only_changes(board):
    client, _, _, _, _ = board
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "snapshot"
        socket.send_text("ignored client input")
        message = socket.receive_json()
        if message["type"] == "accounts":
            assert message["accounts"]["data"] == {"accounts": []}
            message = socket.receive_json()
        assert message["type"] == "ping"
        assert isinstance(message["at"], float)


def test_static_app_and_spa_fallback_use_temporary_dist(board):
    client, _, _, _, settings = board
    assert "build" in client.get("/").text.lower()
    settings.web_dist.mkdir()
    (settings.web_dist / "index.html").write_text("<main>Temporary board</main>")
    (settings.web_dist / "app.js").write_text("console.log('temporary');")
    assert client.get("/").text == "<main>Temporary board</main>"
    assert client.get("/tasks/demo").text == "<main>Temporary board</main>"
    assert client.get("/app.js").text == "console.log('temporary');"
    assert client.get("/api/not-a-route").status_code == 404


def test_symlinks_do_not_expose_other_files(board, tmp_path):
    client, application, directory, _, _ = board
    outside = tmp_path / "unrelated.txt"
    outside.write_text("not a task document")
    (directory / "brief.md").symlink_to(outside)
    assert client.get("/api/tasks/demo/files/brief").status_code == 404
    alias = directory.parent / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    client.portal.call(application.state.board.poll)
    assert client.get("/api/tasks/alias").status_code == 404


def test_session_environment_defaults_and_store_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TASK_HOME", str(tmp_path / "tasks"))
    monkeypatch.setenv("BOARD_CODEX_TASK", str(tmp_path / "accounts.py"))
    for name in ("BOARD_CLAUDE_DESKTOP_DIR", "BOARD_CLAUDE_HOME", "BOARD_SESSIONS_SECONDS",
                 "BOARD_SESSION_STALE_HOURS"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert defaults.claude_desktop_dir == Path.home() / "Library/Application Support/Claude"
    assert defaults.claude_home == Path.home() / ".claude"
    assert defaults.sessions_seconds == 5
    assert defaults.session_stale_hours == 48
    monkeypatch.setenv("BOARD_CLAUDE_DESKTOP_DIR", str(tmp_path / "desktop"))
    monkeypatch.setenv("BOARD_CLAUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOARD_SESSIONS_SECONDS", "2.5")
    monkeypatch.setenv("BOARD_SESSION_STALE_HOURS", "12")
    settings = Settings.from_env()
    assert settings.claude_desktop_dir == tmp_path / "desktop"
    assert settings.claude_home == tmp_path / "home"
    store = create_app(settings).state.board.store
    assert store.sessions_seconds == 2.5
    assert store.session_stale_hours == 12


def test_machine_environment_and_websocket(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TASK_HOME", str(tmp_path))
    monkeypatch.delenv("BOARD_MACHINES_FILE", raising=False)
    monkeypatch.delenv("BOARD_MACHINES_SECONDS", raising=False)
    defaults = Settings.from_env()
    assert defaults.machines_seconds == 30
    assert create_app(defaults).state.board.machines.path == tmp_path / "board-machines.json"
    config = tmp_path / "machines.json"
    target = "private-target.example"
    config.write_text(json.dumps({"machines": [{"id": "worker", "name": "Worker", "ssh": target}]}))
    fake = tmp_path / "fake-ssh"
    fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
                    f"print({target!r}, file=sys.stderr)\nsys.exit(255)\n")
    fake.chmod(0o700)
    accounts = tmp_path / "accounts.py"
    accounts.write_text("print('{}')\n")
    monkeypatch.setenv("BOARD_CODEX_TASK", str(accounts))
    monkeypatch.setenv("BOARD_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("BOARD_CLAUDE_DESKTOP_DIR", str(tmp_path / "desktop"))
    monkeypatch.setenv("BOARD_MACHINES_FILE", str(config))
    monkeypatch.setenv("BOARD_MACHINES_SECONDS", "0.05")
    settings = Settings.from_env()
    settings.ssh_command = str(fake)
    assert settings.machines_file == config
    assert settings.machines_seconds == 0.05
    application = create_app(settings)
    with TestClient(application, base_url="http://localhost", headers={"Host": "localhost"}) as client:
        with client.websocket_connect("/ws") as socket:
            first = socket.receive_json()
            assert "machines" in first["snapshot"]
            # A config error changes the payload even if the first probe already completed.
            config.write_text('{"machines": [')
            message = receive_until(socket, "machines")
            while message["machines"]["config_error"] is None:
                message = receive_until(socket, "machines")
            item = message["machines"]["items"][0]
            assert item["error"] == "unreachable"
            assert item["status"] == "offline"
        response = client.get("/api/snapshot")
        assert target not in response.text
        assert target not in json.dumps(message)
        assert response.json()["machines"]["items"][0]["metrics"] is None


def test_session_rest_websocket_status_activity_and_removal(tmp_path):
    desktop = tmp_path / "desktop"
    home = tmp_path / "home"
    directory = desktop / "claude-code-sessions" / "account" / "device"
    directory.mkdir(parents=True)
    metadata = directory / "local_example.json"
    state = {"sessionId": "local_example", "cliSessionId": "cli-example", "title": "Example session",
             "cwd": "/tmp/project", "originCwd": "/tmp/project", "model": "claude-model",
             "effort": "high", "isArchived": False, "createdAt": 1000,
             "lastActivityAt": 1793872800000, "completedTurns": 1,
             "interruptedAwaitingPerson": None, "prs": [], "private": "metadata secret"}
    metadata.write_text(json.dumps(state))
    live_dir = home / "sessions"
    live_dir.mkdir(parents=True)
    live_path = live_dir / f"{os.getpid()}.json"
    live = {"pid": os.getpid(), "sessionId": "cli-example", "cwd": "/tmp/project",
            "status": "idle", "statusUpdatedAt": 1793872801000, "entrypoint": "desktop",
            "private": "live state secret"}
    live_path.write_text(json.dumps(live))
    transcript = home / "projects" / "-tmp-project" / "cli-example.jsonl"
    transcript.parent.mkdir(parents=True)
    fake = tmp_path / "accounts.py"
    fake.write_text("print('{}')\n")
    settings = Settings(tmp_path / "tasks", fake, poll_seconds=3600,
                        claude_desktop_dir=desktop, claude_home=home, sessions_seconds=0)
    application = create_app(settings)
    with TestClient(application, base_url="http://localhost", headers={"Host": "localhost"}) as client:
        snapshot = client.get("/api/snapshot").json()
        assert len(snapshot["tasks"]) == 1
        task = snapshot["tasks"][0]
        assert set(task) == SUMMARY_FIELDS
        assert task["kind"] == "session"
        assert task["alive"] is False
        detail = client.get("/api/tasks/local_example").json()
        assert set(detail) == SUMMARY_FIELDS | {"timeline", "files"}
        assert detail["files"] == []
        assert len(detail["timeline"]) == 2
        assert "secret" not in json.dumps(detail)
        for path in ("/api/tasks/local_example/files/brief", "/api/tasks/local_example/files/state.json",
                     "/api/tasks/%2e%2e%2fx", "/api/tasks/%2e%2e%2fx/files/brief"):
            assert client.get(path).status_code == 404
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "snapshot"
            live["status"] = "busy"
            live_path.write_text(json.dumps(live))
            client.portal.call(application.state.board.poll)
            changed = receive_until(socket, "task")
            assert changed["task"]["slug"] == "local_example"
            assert changed["task"]["alive"] is True
            assert changed["task"]["column"] == "running"
            event = {"type": "assistant", "timestamp": "2026-11-05T10:00:02Z",
                     "message": {"content": [{"type": "text", "text": "First update"}]}}
            transcript.write_text(json.dumps(event) + "\n")
            client.portal.call(application.state.board.poll)
            activity = receive_until(socket, "activity")
            assert activity == {"type": "activity", "slug": "local_example",
                                "activity": {"at": 1793872802.0, "kind": "message", "text": "First update"}}
            event["message"]["content"][0]["text"] = "Appended update"
            with transcript.open("a") as stream:
                stream.write(json.dumps(event) + "\n")
            client.portal.call(application.state.board.poll)
            assert receive_until(socket, "activity")["activity"]["text"] == "Appended update"
            state["isArchived"] = True
            metadata.write_text(json.dumps(state))
            client.portal.call(application.state.board.poll)
            assert receive_until(socket, "task_removed") == {"type": "task_removed", "slug": "local_example"}
            assert client.get("/api/tasks/local_example").status_code == 404
            state["isArchived"] = False
            metadata.write_text(json.dumps(state))
            client.portal.call(application.state.board.poll)
            assert receive_until(socket, "task")["task"]["slug"] == "local_example"
            metadata.unlink()
            client.portal.call(application.state.board.poll)
            assert receive_until(socket, "task_removed")["slug"] == "local_example"
