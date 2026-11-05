"""Contract checks through the REST and WebSocket interfaces."""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from codex_worker_board import app as board_app
from codex_worker_board.app import Settings, create_app


SUMMARY_FIELDS = {
    "slug", "kind", "repo", "branch", "task_type", "executor", "account", "reviewer",
    "model", "effort", "column", "phase", "phase_started_at", "alive", "turn", "fix_round",
    "outcome", "result_status", "summary", "pr", "ci", "sleeping_until", "created", "updated_at",
    "activity",
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


def test_rest_contract_files_and_unknown_paths(board):
    client, _, directory, _, _ = board
    snapshot = client.get("/api/snapshot").json()
    assert set(snapshot) == {"generated_at", "tasks", "accounts"}
    assert isinstance(snapshot["generated_at"], float)
    assert len(snapshot["tasks"]) == 1
    assert set(snapshot["tasks"][0]) == SUMMARY_FIELDS
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
        (directory / "events.jsonl").write_text(json.dumps(event) + "\n")
        activity = receive_until(socket, "activity")
        assert activity == {"type": "activity", "slug": "demo",
                            "activity": {"at": None, "kind": "command", "text": "git status"}}
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
