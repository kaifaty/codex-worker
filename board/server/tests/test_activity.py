import json

import pytest

from codex_worker_board.activity import EventTail, TAIL_BYTES


@pytest.mark.parametrize("event_type", ["item.started", "item.completed"])
@pytest.mark.parametrize("item,kind,text", [
    ({"type": "command_execution", "command": "python3\n check.py"}, "command", "python3 check.py"),
    ({"type": "file_change", "changes": [{"path": "src/main.py", "kind": "update"}]}, "edit", "src/main.py"),
    ({"type": "agent_message", "text": "A" * 250}, "message", "A" * 200),
    ({"type": "reasoning", "text": "Checking\n the state"}, "thinking", "Checking the state"),
    ({"type": "collab_tool_call", "tool": "spawn_agent", "prompt": "Inspect tests"}, "tool", "spawn_agent Inspect tests"),
])
def test_codex_activity(tmp_path, event_type, item, kind, text):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"type": event_type, "item": item, "timestamp": 123.5}) + "\n")
    assert EventTail(path).read() == {"at": 123.5, "kind": kind, "text": text}


@pytest.mark.parametrize("block,kind,text", [
    ({"type": "tool_use", "name": "Bash", "input": {"command": "git\n status"}}, "command", "git status"),
    ({"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}, "edit", "a.py"),
    ({"type": "tool_use", "name": "Write", "input": {"file_path": "b.py"}}, "edit", "b.py"),
    ({"type": "tool_use", "name": "Read", "input": {"file_path": "c.py"}}, "tool", "Read"),
    ({"type": "text", "text": "Ready\n for review"}, "message", "Ready for review"),
    ({"type": "thinking", "thinking": "Consider\n the result"}, "thinking", "Consider the result"),
])
def test_claude_activity(tmp_path, block, kind, text):
    path = tmp_path / "claude-events.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"content": [block]},
                               "timestamp": "1970-01-01T00:02:03.500Z"}) + "\n")
    assert EventTail(path).read() == {"at": 123.5, "kind": kind, "text": text}


def test_incremental_tail_ignores_bad_json_and_defers_partial_line(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    first = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "First"}}).encode() + b"\n"
    second = json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "Second"}}).encode() + b"\n"
    path.write_bytes(b"x" * (450 * 1024) + b"\n" + first + b"{broken json}\n" + second[:30])
    tail = EventTail(path)
    assert tail.read() == {"at": None, "kind": "message", "text": "First"}
    old_offset = tail.offset
    assert old_offset == path.stat().st_size
    assert tail.read() is None

    # The old prefix must never be decoded again after the file grows.
    parsed = []
    original_loads = json.loads

    def capture_loads(value):
        parsed.append(value)
        return original_loads(value)

    monkeypatch.setattr("codex_worker_board.activity.json.loads", capture_loads)
    with path.open("ab") as stream:
        stream.write(second[30:])
    assert tail.read() == {"at": None, "kind": "thinking", "text": "Second"}
    assert parsed == [second.rstrip(b"\n")]
    assert tail.offset - old_offset == len(second) - 30
    assert tail.read() is None


def test_tail_bounds_initial_read_and_resets_after_replacement_and_truncation(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Latest"}}).encode() + b"\n"
    path.write_bytes(b"z" * (TAIL_BYTES * 2) + b"\n" + event)
    tail = EventTail(path)
    reads = []
    original_fdopen = __import__("os").fdopen

    class TrackedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, count):
            start = self.stream.tell()
            data = self.stream.read(count)
            reads.append((start, len(data)))
            return data

    monkeypatch.setattr("codex_worker_board.activity.os.fdopen", lambda *args: TrackedStream(original_fdopen(*args)))
    assert tail.read()["text"] == "Latest"
    assert reads == [(path.stat().st_size - TAIL_BYTES, TAIL_BYTES)]
    path.write_bytes(event)
    assert tail.read()["text"] == "Latest"
    replacement = tmp_path / "replacement"
    replacement.write_bytes(event.replace(b"Latest", b"Replaced"))
    replacement.replace(path)
    assert tail.read()["text"] == "Replaced"
    missing = EventTail(tmp_path / "missing.jsonl")
    assert missing.read() is None
