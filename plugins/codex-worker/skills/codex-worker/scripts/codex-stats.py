#!/usr/bin/env python3
"""Per-turn cost of a Codex thread, read from its rollout file.

Usage: codex-stats.py <thread-id | rollout.jsonl> [--json]

For every turn: reasoning effort, wall time, tokens (input, cached, uncached, output, reasoning),
tool calls and the weekly limit's used percent at the start and end (or the longest reported
window if no weekly window is available). The limit is shared by all Codex threads, so its
delta is only this thread's when nothing else ran meanwhile.
"""
import json
import runpy
import sys
from datetime import datetime
from pathlib import Path

ROLLOUT = runpy.run_path(str(Path(__file__).with_name("rollout.py")))
CODEX_HOME = Path.home() / ".codex"
TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def rollout_path(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    for root in (CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"):
        found = sorted(root.rglob(f"*{arg}*.jsonl"))
        if found:
            return found[-1]
    sys.exit(f"no rollout for {arg}")


def when(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def limit_window(windows) -> dict:
    windows = [window for window in windows if window]
    # Any weekly window (at least 10080 minutes) is longer than every nonweekly window.
    return max(windows, key=lambda window: window.get("window_minutes") or 0, default={})


def limit_label(minutes) -> str:
    if not minutes:
        return "limit %"
    if minutes >= 10080:
        return "weekly %"
    return f"{minutes / 60:g}h %" if minutes >= 60 else f"{minutes:g}m %"


def turns(path: Path) -> list[dict]:
    out, cur = [], None
    effort = model = None
    usage: dict = {}
    limit = None
    limit_minutes = None
    for o in ROLLOUT["read_events"](path.open(encoding="utf-8")):
        p, ts = o.get("payload", {}), o.get("timestamp", "")
        kind = p.get("type")
        if o.get("type") == "turn_context":
            settings = (p.get("collaboration_mode") or {}).get("settings") or {}
            effort = settings.get("reasoning_effort") or p.get("effort")
            model = settings.get("model") or p.get("model")
            if cur:
                cur["effort"], cur["model"] = effort, model
        elif kind == "token_count":
            sample = ROLLOUT["decode_token_count"](o)
            usage = sample.total_token_usage or usage
            window = limit_window(sample.windows)
            if window.get("used_percent") is not None:
                limit = window["used_percent"]
                limit_minutes = window.get("window_minutes")
        elif kind in ("task_started", "turn_started"):
            cur = {"start": ts, "effort": effort, "model": model, "usage0": dict(usage), "limit0": limit,
                   "tool_calls": 0}
        elif o.get("type") == "response_item" and kind in ("function_call", "custom_tool_call") and cur:
            cur["tool_calls"] += 1
        elif kind in ("task_complete", "turn_complete", "turn_aborted", "task_aborted") and cur:
            row = {
                "start": cur["start"], "end": ts, "status": "aborted" if "aborted" in kind else "complete",
                "effort": cur["effort"], "model": cur["model"],
                "wall_min": round((when(ts) - when(cur["start"])).total_seconds() / 60, 1),
                "tool_calls": cur["tool_calls"], "limit_start": cur["limit0"], "limit_end": limit,
                "limit_window_minutes": limit_minutes,
                "last_message": (p.get("last_agent_message") or "")[:300],
            }
            for k in TOKENS:
                row[k] = usage.get(k, 0) - cur["usage0"].get(k, 0)
            row["uncached_input_tokens"] = row["input_tokens"] - row["cached_input_tokens"]
            out.append(row)
            cur = None
    return out


def totals(rows: list[dict]) -> dict:
    keys = (*TOKENS, "uncached_input_tokens", "tool_calls", "wall_min")
    return {k: round(sum(r[k] for r in rows), 1) for k in keys} | {"turns": len(rows)}


def main() -> None:
    path = rollout_path(sys.argv[1])
    rows = turns(path)
    if "--json" in sys.argv:
        print(json.dumps({"rollout": str(path), "turns": rows, "totals": totals(rows)}, ensure_ascii=False))
        return
    print(f"{'start (UTC)':16} {'min':>6} {'effort':7} {'tools':>5} {'Mtok':>6} {'uncached k':>10} "
          f"{'out k':>6} {'reason k':>8}  limit window %")
    for r in rows:
        print(f"{r['start'][5:16]:16} {r['wall_min']:6.1f} {str(r['effort']):7} {r['tool_calls']:5} "
              f"{r['total_tokens'] / 1e6:6.2f} {r['uncached_input_tokens'] / 1e3:10.0f} "
              f"{r['output_tokens'] / 1e3:6.1f} {r['reasoning_output_tokens'] / 1e3:8.1f}  "
              f"{limit_label(r['limit_window_minutes'])} {r['limit_start']}→{r['limit_end']}"
              f"{'  (aborted)' if r['status'] == 'aborted' else ''}")
    t = totals(rows)
    print(f"total: {t['turns']} turns, {t['wall_min']} min, {t['tool_calls']} tool calls, "
          f"{t['total_tokens'] / 1e6:.1f} M tokens ({t['uncached_input_tokens'] / 1e6:.2f} M uncached input)")


if __name__ == "__main__":
    main()
