"""Asynchronous notification hooks and their child processes."""
import json
import signal
import subprocess
import sys

from . import clock, config


NOTIFY_PROCESSES = []


def reap_notifications():
    """Poll only our notification children; never wait for a slow hook."""
    NOTIFY_PROCESSES[:] = [proc for proc in NOTIFY_PROCESSES if proc.poll() is None]


def current_turn_account(st):
    if st.get("kind") == "review":
        if "review_account" in st and st["review_account"] is None:
            return None
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        return st.get("review_account") or (reviews[-1]["account"] if reviews else st["account"])
    return st["account"]


def unblock_notification_signals():
    # The supervisor is single-threaded. Reset only the child's inherited mask
    # before exec so hooks remain cancellable during the parent's finalization.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})


def notify(st, cfg, event, message, **fields):
    reap_notifications()
    cfg = config.accounts_config() if cfg is None else cfg
    setting = cfg.get("notify")
    if setting is None or ("notify_events" in cfg and event not in cfg["notify_events"]):
        return
    message = st["slug"] + ": " + " ".join(message.splitlines())
    payload = dict(event=event, slug=st["slug"], repo=st["repo"], branch=st["branch"],
                   kind=st.get("kind", "work"), account=current_turn_account(st), at=clock.CLOCK.time(), message=message)
    payload.update(fields)
    if setting == "macos":
        escaped = message.replace("\\", "\\\\").replace('"', '\\"')
        args = ["osascript", "-e", f'display notification "{escaped}" with title "codex-worker"']
    else:
        args = list(setting) + [json.dumps(payload, ensure_ascii=False)]
    try:
        # The child keeps this descriptor even if clean unlinks the task directory.
        with (config.TASKS / st["slug"] / "notify.log").open("ab", buffering=0) as log:
            try:
                proc = subprocess.Popen(args, cwd="/", stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log, start_new_session=True,
                                        preexec_fn=unblock_notification_signals)
            except (OSError, ValueError) as exc:
                log.write(f"{event}: notification launch failed: {exc}\n".encode("utf-8", errors="replace"))
            else:
                NOTIFY_PROCESSES.append(proc)
    except OSError as exc:
        print(f"notification log failed: {exc}", file=sys.stderr)
