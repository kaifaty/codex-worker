"""Guarded paths and check or base-check execution."""
import fnmatch
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from collections import Counter, deque
from pathlib import Path

from . import accounts, clock, config, notifications, procs, sandboxing, task_state, telemetry_log


def check_paths(st):
    """Read saved and existing relative paths from top-level check tokens."""
    paths = set(st.get("check_paths", []))
    root = Path(st["worktree"])
    try:
        lexer = shlex.shlex(st.get("check") or "", posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = []
    candidates = []
    for token in tokens:
        candidates.append(token)
        if "=" in token:
            name, value = token.split("=", 1)
            if token.startswith("--") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                candidates.append(value)
    for candidate in candidates:
        if not candidate or Path(candidate).is_absolute():
            continue
        path = Path(os.path.abspath(root / candidate))
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            exists = path.exists()
        except OSError:
            # Shell arguments can be code or long strings rather than filenames.
            continue
        if exists:
            paths.add(relative)
    return sorted(paths)


def guarded_path(path, st, paths):
    parts = path.split("/")
    return (any(part in ("tests", "test", "__tests__", "spec") for part in parts[:-1])
            or any(fnmatch.fnmatchcase(parts[-1], pattern)
                   for pattern in ("test_*", "*_test.*", "*.test.*", "*.spec.*"))
            or path == ".gitlab-ci.yml"
            or path.startswith((".github/workflows/", ".forgejo/workflows/"))
            or any(item == "." or path == item or path.startswith(item + "/") for item in paths)
            or any(fnmatch.fnmatchcase(path, pattern.removeprefix("./")) for pattern in st.get("guards", [])))


DISABLED_TEST = re.compile(
    r"@unittest\.(?:skip\w*|expectedFailure)\b|\bself\.skipTest\s*\("
    r"|\bpytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.skip\s*\("
    r"|\.(?:skip|only)\s*\(|\b(?:xit|xdescribe)\s*\("
    r"|@(?:Disabled|Ignore)\b|\bt\.Skip\s*\(", re.I)


def guarded_changes(st):
    """Combine diff layers, matching moved skip markers within each layer."""
    if st.get("kind") == "scout" or sandboxing.workspace_filter_driver(st) is not None:
        return []
    root = st["worktree"]
    paths = check_paths(st)
    changed = {}
    args = config.GIT + ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-renames"]
    base = procs.sh(config.GIT + ["merge-base", st["base"], "HEAD"], cwd=root, check=False) or st["base"]
    for revisions in ([base, "HEAD"], ["--cached", "HEAD"], []):
        removed_lines = Counter()
        added_markers = []
        stats = procs.sh(args + ["--numstat", "-z"] + revisions + ["--"], cwd=root, check=False)
        deleted = set(procs.sh(args + ["--name-only", "-z", "--diff-filter=D"] + revisions + ["--"],
                         cwd=root, check=False).split("\0"))
        for entry in stats.split("\0"):
            if not entry:
                continue
            added, removed, path = entry.split("\t", 2)
            if not guarded_path(path, st, paths):
                continue
            item = changed.setdefault(path, {"path": path, "added": 0, "removed": 0,
                                             "deleted": False, "disabled": [], "moved": 0})
            item["added"] += int(added) if added != "-" else 0
            item["removed"] += int(removed) if removed != "-" else 0
            item["deleted"] |= path in deleted
            # Preserve non-UTF-8 bytes for matching; replacing them here can
            # make different skip conditions look like an unchanged move.
            patch = procs.sh(args + ["--unified=0"] + revisions + ["--", path],
                       cwd=root, check=False, errors="surrogateescape")
            line_number = 0
            in_hunk = False
            for line in patch.splitlines():
                hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if hunk:
                    line_number = int(hunk[1])
                    in_hunk = True
                elif in_hunk and line.startswith("+"):
                    if DISABLED_TEST.search(line[1:]):
                        marker = {"line": line_number, "text": line[1:].strip()}
                        added_markers.append((path, marker))
                    line_number += 1
                elif in_hunk and line.startswith("-"):
                    removed_lines[line[1:].strip()] += 1
                elif in_hunk and line.startswith(" "):
                    line_number += 1
        if not revisions:
            # Untracked additions belong to the index-to-working-tree layer.
            untracked = procs.sh(config.GIT + ["ls-files", "--others", "--exclude-standard", "-z"], cwd=root, check=False)
            for path in filter(None, untracked.split("\0")):
                if not guarded_path(path, st, paths):
                    continue
                file = Path(root) / path
                try:
                    content = os.readlink(file) if file.is_symlink() else file.read_text(encoding="utf-8", errors="surrogateescape")
                except OSError:
                    continue
                lines = content.splitlines()
                item = changed.setdefault(path, {"path": path, "added": 0, "removed": 0,
                                                 "deleted": False, "disabled": [], "moved": 0})
                item["added"] += len(lines)
                added_markers.extend((path, {"line": n, "text": line.strip()})
                                     for n, line in enumerate(lines, 1) if DISABLED_TEST.search(line))
        # Match across files only after collecting every deletion in this layer.
        # A deletion cannot hide a marker added in another layer.
        for path, marker in added_markers:
            item = changed[path]
            if removed_lines[marker["text"]]:
                removed_lines[marker["text"]] -= 1
                item["moved"] += 1
            else:
                # Replacement is only for display, after lossless matching.
                marker["text"] = (marker["text"].encode("utf-8", errors="surrogateescape")
                                  .decode("utf-8", errors="replace"))
                if marker not in item["disabled"]:
                    item["disabled"].append(marker)
    return [changed[path] for path in sorted(changed)]


def guarded_lines(changes):
    if not changes:
        return []
    lines = ["== guarded files changed: " + ", ".join(
        f"{item['path']} (+{item['added']} −{item['removed']})" for item in changes)]
    deleted = [item["path"] for item in changes if item["deleted"]]
    if deleted:
        lines.append("== guarded files deleted: " + ", ".join(deleted))
    disabled = [f"{item['path']}:{marker['line']} {marker['text']}"
                for item in changes for marker in item["disabled"]]
    if disabled:
        lines.append("== guarded tests disabled: " + "; ".join(disabled))
    moved = sum(item["moved"] for item in changes)
    if moved:
        lines.append(f"== guarded skip markers moved: {moved}")
    return lines


def check_tail(st, filename="check.log"):
    path = config.TASKS / st["slug"] / filename
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", errors="replace") as output:
        return "".join(deque(output, maxlen=40)).rstrip()


def execute_check(st, stopped, cwd, filename):
    """Run the saved check after work, owning and reaping its process group."""
    if stopped():
        return None
    started = time.monotonic()
    measured_started = clock.CLOCK.monotonic()
    proc = None
    outcome = "failed"
    code = None
    with (config.TASKS / st["slug"] / filename).open("wb") as log:
        try:
            args = ["/bin/sh", "-c", st["check"]]
            env = None
            if st.get("sandbox") == "workspace":
                args = [config.codex_binary(), "sandbox"] + sandboxing.sandbox_args(st) + ["--"] + args
                env = accounts.account_env({"home": str(config.CODEX_HOME)})
            proc = subprocess.Popen(args, cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            st["check_pid"] = proc.pid
            task_state.save(st)
            deadline = started + st.get("check_timeout", 1800)
            while proc.poll() is None:
                if stopped():
                    outcome = task_state.stop_outcome(st, stopped)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = "timeout"
                    break
                time.sleep(min(0.1, remaining))
            else:
                outcome = "passed" if proc.returncode == 0 else "failed"
        except OSError as exc:
            log.write(f"check launch failed: {exc}\n".encode("utf-8", errors="replace"))
        finally:
            if proc is not None:
                procs.signal_group(proc, signal.SIGKILL)
                code = proc.wait()
            st.pop("check_pid", None)
    if stopped():
        outcome = task_state.stop_outcome(st, stopped)
        st["outcome"] = outcome
    result = {"cmd": st["check"], "exit": code, "seconds": round(clock.CLOCK.monotonic() - measured_started, 3),
              "at": clock.CLOCK.time(), "outcome": outcome}
    if outcome in ("failed", "timeout"):
        result["tail"] = check_tail(st, filename)
    return result


def run_check(st, stopped):
    if stopped():
        return None
    with telemetry_log.record_phase(st, "check", stopped) as phase:
        result = _run_check(st, stopped)
        phase.check = result
        phase.finish(outcome=task_state.stop_outcome(st, stopped) or (result or {}).get("outcome"))
        return result


def _run_check(st, stopped):
    result = execute_check(st, stopped, st["worktree"], "check.log")
    if result is None:
        return None
    st.setdefault("checks", []).append(result)
    task_state.save(st)
    return result


def run_base_check(st, stopped):
    with telemetry_log.record_phase(st, "base_check", stopped) as phase:
        result = _run_base_check(st, stopped)
        phase.check = result
        phase.finish(outcome=task_state.stop_outcome(st, stopped) or (result or {}).get("outcome"))
        return result


def _run_base_check(st, stopped):
    """Check task HEAD with the merge base's guarded files, leaving the task untouched."""
    driver = sandboxing.workspace_filter_driver(st)
    if driver is not None:
        return {"outcome": "skipped", "reason": f"filter driver {driver} is configured; commit this turn manually"}
    if st["worktree"] in st["check"]:
        return {"outcome": "skipped", "reason": "check command uses the task worktree's absolute path"}
    started = clock.CLOCK.monotonic()
    log = config.TASKS / st["slug"] / "base-check.log"
    log.write_text("", encoding="utf-8")
    result = None
    paths = check_paths(st)
    try:
        base = procs.sh(config.GIT + ["merge-base", st["base"], "HEAD"], cwd=st["worktree"])
        with tempfile.TemporaryDirectory(prefix="base-check-", dir=str(config.TASKS / st["slug"])) as directory:
            worktree = Path(directory) / "worktree"
            try:
                subprocess.run(config.GIT + ["worktree", "add", "--quiet", "--detach", str(worktree), "HEAD"],
                               cwd=st["worktree"], check=True, capture_output=True)
                guarded = set()
                for revision in (base, "HEAD"):
                    files = procs.sh(config.GIT + ["ls-tree", "-r", "--name-only", "-z", revision],
                               cwd=worktree)
                    guarded.update(path for path in files.split("\0") if path and guarded_path(path, st, paths))
                if guarded:
                    subprocess.run(config.GIT + ["--literal-pathspecs", "restore", "--source=" + base,
                                    "--staged", "--worktree", "--pathspec-from-file=-", "--pathspec-file-nul"],
                                   input="\0".join(sorted(guarded)) + "\0", text=True,
                                   cwd=worktree, check=True, capture_output=True)
                execution = execute_check(st, stopped, worktree, "base-check.log")
                if execution is not None:
                    result = {key: execution[key] for key in ("outcome", "exit", "seconds")}
            finally:
                if (worktree / ".git").exists():
                    subprocess.run(config.GIT + ["worktree", "remove", "--force", str(worktree)],
                                   cwd=st["worktree"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError, SystemExit) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        with log.open("a", encoding="utf-8") as output:
            output.write("\nbase check setup/cleanup failed: " + (detail or str(exc)) + "\n")
        result = None
    if result is None:
        result = {"outcome": "failed", "exit": None, "seconds": round(clock.CLOCK.monotonic() - started, 3)}
    result["tail"] = check_tail(st, "base-check.log")
    return result


def saved_check(st, cfg, stopped, review_after=True):
    check = run_check(st, stopped) if st.get("check") and not stopped() else None
    if check and check["outcome"] == "passed" and st.get("base_check") and not stopped():
        check["base_check"] = run_base_check(st, stopped)
        task_state.save(st)
    if check and check["outcome"] in ("failed", "timeout"):
        if review_after and not st.get("review_skipped"):
            st["review_skipped"] = "check " + check["outcome"]
        task_state.save(st)
        notifications.notify(st, cfg, "check_failed", f"check {check['outcome']} on {st['account']}",
               outcome=check["outcome"])
    return check
