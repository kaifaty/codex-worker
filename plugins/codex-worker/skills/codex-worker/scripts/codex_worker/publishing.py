"""Completed work turns and supervisor publication."""
import json
import re
import shutil
import subprocess
import tempfile
import urllib.parse

from . import clock, config, result_schema, sandboxing, task_state, threads


def completed_work_turn(st):
    return (st.get("kind") == "work" and
            (st.get("outcome") == "completed" or
             (st.get("outcome") in ("budget_exhausted", "stuck") and
              st.get("wrapup", {}).get("outcome") == "completed")))


def publish_work(st, stopped):
    """Publish a completed workspace turn outside the worker's write boundary."""
    if st.get("sandbox") != "workspace" or not completed_work_turn(st) or stopped():
        return
    receipt = {"at": clock.CLOCK.time(), "sha": "", "pushed": False, "pr": "", "action": "skipped", "error": ""}

    def run(args):
        return subprocess.run(args, cwd=st["worktree"], check=True, capture_output=True,
                              text=True, errors="replace").stdout.strip()

    stage = "commit"
    try:
        driver = sandboxing.unsafe_filter_driver(st["worktree"])
        if driver is not None:
            raise RuntimeError(f"filter driver {driver} is configured; the supervisor does not run filters "
                               "on worker-written files — commit this turn manually")
        if not run(config.GIT + ["status", "--porcelain"]):
            return
        message = ((st.get("result") or {}).get("commit_message") or "").strip()
        message = message or f"{st['slug']}: turn {st['turns']}"
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", prefix="commit-",
                                         dir=str(config.TASKS / st["slug"])) as file:
            file.write(message + "\n")
            file.flush()
            if stopped():
                return
            run(config.GIT + ["add", "-A"])
            if stopped():
                return
            run(config.GIT + ["commit", "-F", file.name])
        receipt["sha"] = run(config.GIT + ["rev-parse", "HEAD"])
        if not st.get("no_push"):
            stage = "push"
            run(config.GIT + ["push", "-u", "origin", st["branch"]])
            receipt["pushed"] = True
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        detail = (exc.stderr or exc.stdout or str(exc)) if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        receipt["error"] = stage + ": " + detail.strip()
    st["publish"] = receipt
    task_state.save(st)


def publish_pr(st, stopped):
    """The supervisor is the only PR writer, in every work mode."""
    if not completed_work_turn(st) or stopped():
        return
    receipt = st.setdefault("publish", {"at": clock.CLOCK.time(), "sha": "", "pushed": False,
                                        "pr": "", "action": "skipped", "error": ""})
    receipt.update(at=clock.CLOCK.time(), action="skipped", pr="")

    def run(args):
        return subprocess.run(args, cwd=st["worktree"], check=True, capture_output=True,
                              text=True, errors="replace").stdout.strip()

    def detail(exc):
        return ((exc.stderr or exc.stdout or str(exc))
                if isinstance(exc, subprocess.CalledProcessError) else str(exc)).strip()

    def skip(reason):
        receipt["error"] = receipt["error"] + "; " + reason if receipt["error"] else reason

    stage = "not pushed"
    try:
        if st.get("no_push"):
            skip("no-push")
            return
        # Remote-tracking refs can be stale: verify the actual origin branch.
        try:
            head = run(config.GIT + ["rev-parse", "HEAD"])
            remote = run(config.GIT + ["ls-remote", "--heads", "origin", "refs/heads/" + st["branch"]])
            receipt["pushed"] = bool(remote and remote.split()[0] == head)
        except (OSError, subprocess.CalledProcessError) as exc:
            receipt["pushed"] = False
            skip("not pushed: " + detail(exc))
            return
        if not receipt["pushed"]:
            skip("not pushed")
            return
        stage = "origin remote"
        remote_url = run(config.GIT + ["remote", "get-url", "origin"])
        if remote_url.startswith("file://") or not re.match(r"[^/]+:", remote_url):
            skip("not a GitHub remote: local path")
            return
        url = urllib.parse.urlsplit(remote_url) if remote_url.startswith(("ssh://", "https://")) else None
        scp_url = re.fullmatch(r"[^/@:]+@([^/:]+):(.+)", remote_url) if url is None else None
        host = url.hostname if url is not None else scp_url[1] if scp_url else None
        path = url.path if url is not None else scp_url[2] if scp_url else ""
        if host and host.casefold() != "github.com" and (scp_url or url.scheme == "ssh"):
            try:
                ssh_config = run(["ssh", "-G", host])
            except (OSError, subprocess.CalledProcessError):
                ssh_config = ""
            for line in ssh_config.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] == "hostname":
                    host = parts[1]
                    break
            if host.casefold() == "github.com":
                remote_url = "github.com/" + path.lstrip("/").removesuffix(".git")
        if host and host.casefold() != "github.com":
            skip("not a GitHub remote: " + host)
            return
        gh = shutil.which("gh")
        if not gh:
            skip("gh unavailable")
            return
        stage = "gh repo view"
        repository = json.loads(run([gh, "repo", "view", remote_url, "--json", "nameWithOwner"]))["nameWithOwner"]
        stage = "gh pr list"
        prs = json.loads(run([gh, "pr", "list", "--head", st["branch"], "--state", "all",
                             "--json", "url,state,headRepository,headRepositoryOwner", "--repo", remote_url]))
        # --head also matches fork branches with the same name.
        # gh can leave headRepository.nameWithOwner empty; the owner is separate.
        prs = [item for item in prs if
               ((item.get("headRepositoryOwner") or {}).get("login", "") + "/" +
                (item.get("headRepository") or {}).get("name", "")).casefold() == repository.casefold()]
        # Prefer the current open PR if this branch name has been reused.
        pr = next((item for item in prs if item["state"] == "OPEN"), prs[0] if prs else None)
        result = st.get("result") or {}
        title = result.get("pr_title", "")
        body = result.get("pr_body", "")
        if pr:
            receipt["pr"] = pr["url"]
            if pr["state"] in ("CLOSED", "MERGED"):
                skip("pr " + pr["state"].lower())
                return
            if not body:
                receipt["action"] = "unchanged"
                return
        if not pr:
            if not title or not body:
                stage = "PR text history"
                for event in reversed(threads.events(st)):
                    item = event.get("item") or {}
                    if event.get("type") != "item.completed" or item.get("type") != "agent_message":
                        continue
                    previous = result_schema.parse_result(item.get("text", ""))
                    if previous is not None:
                        title = title or previous["pr_title"]
                        body = body or previous["pr_body"]
                    if title and body:
                        break
            body = body or result.get("summary", "")
        title = title or run(config.GIT + ["log", "-1", "--format=%s"])
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", prefix="pr-",
                                         dir=str(config.TASKS / st["slug"])) as file:
            file.write(body)
            file.flush()
            if stopped():
                skip("stopped")
                return
            if pr:
                stage = "gh pr edit"
                run([gh, "pr", "edit", receipt["pr"], "--title", title, "--body-file", file.name,
                     "--repo", remote_url])
                receipt["action"] = "updated"
            else:
                stage = "gh pr create"
                receipt["pr"] = run([gh, "pr", "create", "--base", st["base"].split("/", 1)[-1],
                                     "--head", st["branch"], "--title", title, "--body-file", file.name,
                                     "--repo", remote_url])
                receipt["action"] = "created"
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError) as exc:
        skip(stage + ": " + detail(exc))
    finally:
        task_state.save(st)
