#!/usr/bin/env python3
"""Compare CLI help bytes with a local Git revision, without starting a task.

Run with python3 -I tests/compare_cli_help.py --base origin/main. The baseline
plugin tree is extracted to a temporary directory and supplies the command list.
"""

import argparse
import difflib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path("plugins/codex-worker/skills/codex-worker/scripts/codex_task.py")


def archive_plugins(root, revision, destination):
    destination = destination.resolve()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision, "--", "plugins/"],
        cwd=str(root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tree:
        # Git supplies relative paths. Reject links as well so an extraction
        # cannot write through a repository symlink outside this temporary tree.
        for member in tree.getmembers():
            target = destination / member.name
            if (destination not in target.resolve().parents
                    or not (member.isfile() or member.isdir())):
                raise ValueError("unsupported archive entry: " + member.name)
        if hasattr(tarfile, "data_filter"):
            tree.extractall(str(destination), filter="data")
        else:
            # Older Python 3.9 builds do not have extraction filters.
            tree.extractall(str(destination))


def help_environment(directory):
    home = directory / "home"
    home.mkdir()
    # Deliberately avoid inheriting account homes, logical clocks, polling
    # overrides or other task settings from the caller's shell/test fixtures.
    return {
        "PATH": os.defpath,
        "HOME": str(home),
        "CODEX_TASK_HOME": str(home / ".codex"),
        "CODEX_TASK_CODEX": str(directory / "codex-disabled"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "LC_ALL": "C",
        "COLUMNS": "80",
        "TERM": "dumb",
    }


def run_help(script, command, environment, directory):
    # -B also keeps future package imports from writing bytecode in plugins/.
    return subprocess.run(
        [sys.executable, "-I", "-B", str(script)] + list(command) + ["--help"],
        cwd=str(directory), env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )


def commands_from_help(output):
    # argparse lists all subparser choices (including commands without a help
    # description) in the first usage paragraph, even when it wraps lines.
    usage = re.split(rb"\n\s*\n", output, maxsplit=1)[0]
    choices = re.search(rb"\{([^{}]+)\}", usage)
    if choices is None:
        raise ValueError("baseline --help has no command choices in its usage")
    commands = [name.strip() for name in choices.group(1).split(b",")]
    if not commands or any(re.fullmatch(rb"[A-Za-z0-9_-]+", name) is None
                           for name in commands):
        raise ValueError("cannot read baseline command choices")
    return [name.decode("ascii") for name in commands]


def compare(revision, root=None):
    root = ROOT if root is None else Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix="codex-cli-help-") as temporary:
        directory = Path(temporary)
        baseline = directory / "baseline"
        baseline.mkdir()
        archive_plugins(root, revision, baseline)
        environment = help_environment(directory)
        original = run_help(baseline / SCRIPT, (), environment, directory)
        if original.returncode:
            raise ValueError("baseline --help exited {}: {!r}".format(
                original.returncode, original.stderr))
        commands = [()] + [(name,) for name in commands_from_help(original.stdout)]
        differences = []
        for command in commands:
            before = (original if not command else run_help(
                baseline / SCRIPT, command, environment, directory))
            after = run_help(root / SCRIPT, command, environment, directory)
            if (before.stdout, before.stderr, before.returncode) != (
                    after.stdout, after.stderr, after.returncode):
                differences.append((command, before, after))
        return len(commands), differences


def print_difference(command, before, after):
    label = " ".join(("codex_task.py",) + command + ("--help",))
    print("FAIL: " + label)
    if before.returncode != after.returncode:
        print("exit code: {} -> {}".format(before.returncode, after.returncode))
    for stream in ("stdout", "stderr"):
        original, current = getattr(before, stream), getattr(after, stream)
        if original == current:
            continue
        # Compare bytes above; repr lines keep invalid UTF-8, CRLF and missing
        # final newlines visible without making the diagnostic locale-sensitive.
        print("{}: {} -> {} bytes".format(stream, len(original), len(current)))
        lines = difflib.unified_diff(
            [repr(line) + "\n" for line in original.splitlines(keepends=True)],
            [repr(line) + "\n" for line in current.splitlines(keepends=True)],
            fromfile="base/" + stream, tofile="working/" + stream,
        )
        for line in lines:
            print(line, end="")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", metavar="REV", required=True,
                        help="local Git revision to compare")
    args = parser.parse_args(argv)
    try:
        count, differences = compare(args.base)
    except (OSError, ValueError, tarfile.TarError,
            subprocess.SubprocessError) as error:
        parser.exit(2, "comparison failed: {}\n".format(error))
    if not differences:
        print("PASS: {} commands identical".format(count))
        return 0
    for command, before, after in differences:
        print_difference(command, before, after)
    return 1


if __name__ == "__main__":
    sys.exit(main())
