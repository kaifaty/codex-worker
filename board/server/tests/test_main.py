"""The launchd job printed by `codex-worker-board plist`."""

import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from codex_worker_board import __main__ as board_main
from codex_worker_board.app import REPOSITORY


PATH = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"),
                        "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def clean_environment(monkeypatch, task_home):
    for key in list(os.environ):
        if key.startswith("BOARD_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CODEX_TASK_HOME", str(task_home))


@pytest.mark.parametrize("console_script", [True, False])
def test_plist_fields(tmp_path, monkeypatch, capsys, console_script):
    clean_environment(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("BOARD_STALE_DAYS", "3")
    monkeypatch.setenv("BOARD_MACHINES_FILE", "~/machines.json")
    venv = tmp_path / "venv/bin"
    venv.mkdir(parents=True)
    if console_script:
        (venv / "codex-worker-board").write_text("")
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    board_main.main(["plist", "--host", "127.0.0.2", "--port", "9001"])
    job = plistlib.loads(capsys.readouterr().out.encode("utf-8"))
    options = ["--host", "127.0.0.2", "--port", "9001"]
    if console_script:
        assert job["ProgramArguments"] == [str(venv / "codex-worker-board")] + options
    else:
        assert job["ProgramArguments"] == [str(venv / "python"), "-m", "codex_worker_board"] + options
    log = str(tmp_path / "home/board-server.log")
    assert job == {
        "Label": "codex-worker.board", "ProgramArguments": job["ProgramArguments"],
        "WorkingDirectory": str(REPOSITORY), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "EnvironmentVariables": {"PATH": PATH, "CODEX_TASK_HOME": str(tmp_path / "home"),
                                 "BOARD_STALE_DAYS": "3", "BOARD_MACHINES_FILE": "~/machines.json"},
        "StandardOutPath": log, "StandardErrorPath": log,
    }
    board_main.main(["plist"])
    defaults = plistlib.loads(capsys.readouterr().out.encode("utf-8"))["ProgramArguments"]
    assert defaults[-4:] == ["--host", "127.0.0.1", "--port", "8787"]


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_plist_program_serves_snapshot(tmp_path, monkeypatch):
    home, task_home = tmp_path / "home", tmp_path / "tasks"
    home.mkdir()
    task_home.mkdir()
    fake = tmp_path / "accounts.py"
    fake.write_text("print('{\"accounts\": []}')\n")
    clean_environment(monkeypatch, task_home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BOARD_CODEX_TASK", str(fake))
    monkeypatch.setenv("BOARD_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("BOARD_CLAUDE_DESKTOP_DIR", str(tmp_path / "desktop"))
    port = free_port()
    output = subprocess.run([sys.executable, "-m", "codex_worker_board", "plist", "--port", str(port)],
                            check=True, capture_output=True, timeout=30).stdout
    job = plistlib.loads(output)
    assert job["ProgramArguments"][0] == str(Path(sys.executable).parent / "codex-worker-board")
    assert {key: job["EnvironmentVariables"][key]
            for key in ("BOARD_CODEX_TASK", "BOARD_CLAUDE_HOME", "BOARD_CLAUDE_DESKTOP_DIR")} == {
        "BOARD_CODEX_TASK": str(fake), "BOARD_CLAUDE_HOME": str(tmp_path / "claude"),
        "BOARD_CLAUDE_DESKTOP_DIR": str(tmp_path / "desktop")}
    with open(job["StandardOutPath"], "ab") as log:
        server = subprocess.Popen(job["ProgramArguments"], cwd=job["WorkingDirectory"],
                                  env=dict(job["EnvironmentVariables"], HOME=str(home)),
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 30
        while True:
            assert server.poll() is None, Path(job["StandardOutPath"]).read_text()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=2) as response:
                    assert response.status == 200
                    break
            except OSError:
                assert time.monotonic() < deadline, Path(job["StandardOutPath"]).read_text()
                time.sleep(0.1)
    finally:
        server.terminate()
        server.wait(timeout=15)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
