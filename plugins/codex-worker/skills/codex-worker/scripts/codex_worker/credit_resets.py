"""Available reset credits and their shared journal."""
import fcntl
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager

from . import accounts, clock, config


def reset_credits(info):
    """Only spend available Codex resets that have not expired."""
    return sorted((c for c in info.get("reset_credits", {}).get("credits", [])
                   if c.get("id") and c.get("status") == "available"
                   and c.get("resetType") == "codexRateLimits"
                   and (c.get("expiresAt") is None or c["expiresAt"] > clock.CLOCK.time())),
                  key=lambda c: c.get("expiresAt") if c.get("expiresAt") is not None else float("inf"))


@contextmanager
def reset_journal(cancelled=lambda: False):
    config.CODEX_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = config.CODEX_HOME / "codex-worker-resets.jsonl"
    # Parallel tasks must agree on a key, even if one dies during the RPC.
    with path.open("a+", encoding="utf-8") as journal:
        waiting_logged = False
        while True:
            if cancelled():
                raise RuntimeError("stopped")
            try:
                fcntl.flock(journal, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting_logged:
                    print("reset journal: waiting for lock", file=sys.stderr, flush=True)
                    waiting_logged = True
                time.sleep(0.05)
        journal.seek(0)
        entries = []
        last_line = ""
        for line in journal:
            last_line = line
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        if cancelled():
            raise RuntimeError("stopped")
        if last_line and not last_line.endswith("\n"):
            journal.write("\n")

        def record(entry, **fields):
            value = dict(entry, at=clock.CLOCK.time(), **fields)
            journal.write(json.dumps(value, ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
            entries.append(value)

        yield entries, record


def pending_resets(entries, account):
    latest = {}
    for entry in entries:
        if entry.get("account") == account["name"] and entry.get("creditId") and entry.get("idempotencyKey"):
            latest[entry["creditId"]] = entry
    return [entry for entry in latest.values()
            if entry.get("event") == "attempt" or entry.get("outcome") == "unknown"]


def consume_recorded_reset(account, credit, slug, cancelled, entries, record):
    key = next((entry["idempotencyKey"] for entry in reversed(entries)
                if entry.get("creditId") == credit["id"] and entry.get("idempotencyKey")), None)
    intent = {"slug": slug, "account": account["name"], "creditId": credit["id"],
              "idempotencyKey": key or str(uuid.uuid4()), "credit": credit}
    record(intent, event="attempt")
    try:
        with accounts.account_rpc(account, cancelled) as call:
            response = call("account/rateLimitResetCredit/consume",
                            {"creditId": intent["creditId"], "idempotencyKey": intent["idempotencyKey"]})
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        unknown = not isinstance(exc, accounts.RPCError) or "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
        record(intent, event="result", outcome="unknown" if unknown else "error", error=str(exc))
        raise
    record(intent, event="result", outcome="success", response=response)
    return response


def reset_credit_action(account, credit_id=None, slug="", cancelled=lambda: False,
                        confirmed=False, pending_only=False, allow_consume=True, quota_cfg=None,
                        credit_id_is_hint=False):
    """Resolve a lost reply before selecting another credit, under the same lock."""
    with reset_journal(cancelled) as (entries, record):
        pending = pending_resets(entries, account)
        if pending and not credit_id_is_hint and credit_id is not None and credit_id != pending[0]["creditId"]:
            pending_id = pending[0]["creditId"]
            raise RuntimeError(f"credit {pending_id} has a pending attempt with an unknown outcome; "
                               f"resolve it first (reset-credit {account['name']} --credit {pending_id} "
                               "--yes retries it with the same key)")
        if pending_only and not pending:
            return None
        info = accounts.account_read(account, cancelled)
        if info.get("logged_in") is not True:
            if pending_only:
                return {"info": info, "outcome": "unavailable"}
            raise RuntimeError(f"account {account['name']} status " +
                               (f"unknown (rpc error: {info.get('error', 'unavailable')})"
                                if info.get("logged_in") is None else f"not logged in: run login {account['name']}"))
        if pending:
            entry = pending[0]
            credit = next((c for c in info.get("reset_credits", {}).get("credits", [])
                           if c.get("id") == entry["creditId"]), None)
            if credit is None or credit.get("status") != "available":
                if confirmed:
                    record(entry, event="result", outcome="consumed (inferred)")
                return {"credit": credit or entry.get("credit") or {"id": entry["creditId"]},
                        "info": info, "outcome": "consumed (inferred)"}
        if quota_cfg is not None and not accounts.exhausted(info, quota_cfg):
            return {"info": info, "outcome": "available"}
        if not pending:
            choices = reset_credits(info)
            credit = next((c for c in choices if c["id"] == credit_id), None) if credit_id else next(iter(choices), None)
            if credit is None:
                raise RuntimeError(f"no available free limit reset for {account['name']}" +
                                   (f" with ID {credit_id}" if credit_id else ""))
        action = {"credit": credit, "info": info, "outcome": "preview"}
        if confirmed and allow_consume:
            action["response"] = consume_recorded_reset(account, credit, slug, cancelled, entries, record)
            action.update(outcome="success", info=accounts.account_read(account, cancelled))
        return action
