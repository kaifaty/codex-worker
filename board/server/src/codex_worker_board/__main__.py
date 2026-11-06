"""Console entry point for the local board server."""

import argparse
import os
import plistlib
import sys
from pathlib import Path

from .app import REPOSITORY

LAUNCHD_LABEL = "codex-worker.board"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def launchd_plist(host, port):
    """A launchd job that keeps the board running; printing it never installs or loads anything."""
    task_home = Path(os.environ.get("CODEX_TASK_HOME", "~/.codex")).expanduser()
    environment = {"PATH": os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"),
                                            "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
                   "CODEX_TASK_HOME": str(task_home)}
    environment.update((key, value) for key, value in os.environ.items() if key.startswith("BOARD_"))
    # The venv interpreter is a symlink; its own directory holds the console script.
    command = Path(sys.executable).parent / "codex-worker-board"
    options = ["--host", host, "--port", str(port)]
    program = [str(command)] + options if command.is_file() else [sys.executable, "-m", "codex_worker_board"] + options
    log = str(task_home / "board-server.log")
    return {"Label": LAUNCHD_LABEL, "ProgramArguments": program, "WorkingDirectory": str(REPOSITORY),
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "EnvironmentVariables": environment, "StandardOutPath": log, "StandardErrorPath": log}


def add_address(parser):
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only codex-worker task board")
    add_address(parser)
    commands = parser.add_subparsers(dest="command")
    plist = commands.add_parser("plist", help="print a launchd job that keeps the board running")
    add_address(plist)
    args = parser.parse_args(argv)
    if args.command == "plist":
        sys.stdout.write(plistlib.dumps(launchd_plist(args.host, args.port)).decode("utf-8"))
        return
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
