"""Read the latest activity from bounded, incremental executor JSONL tails."""

import json
import math
import os
import stat
from datetime import datetime
from pathlib import Path


TAIL_BYTES = 256 * 1024


def _line(value: str) -> str:
    return " ".join(value.split())[:200]


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            pass
    return None


def _activity(event):
    if not isinstance(event, dict):
        return None
    at = _timestamp(event.get("timestamp", event.get("at")))
    kind, text = None, ""
    if event.get("type") in ("item.started", "item.completed"):
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "command_execution":
            kind, text = "command", item.get("command", "")
        elif item_type == "file_change":
            kind = "edit"
            changes = item.get("changes")
            changes = changes if isinstance(changes, list) else []
            text = ", ".join(change["path"] for change in changes
                             if isinstance(change, dict) and isinstance(change.get("path"), str))
        elif item_type in ("agent_message", "reasoning"):
            kind = "message" if item_type == "agent_message" else "thinking"
            text = item.get("text", "")
        elif item_type == "collab_tool_call":
            kind = "tool"
            text = " ".join(value for value in (item.get("tool"), item.get("prompt"))
                            if isinstance(value, str))
    elif event.get("type") == "assistant":
        message = event.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return None
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                name = block.get("name", "")
                inputs = block.get("input")
                inputs = inputs if isinstance(inputs, dict) else {}
                if name == "Bash":
                    kind, text = "command", inputs.get("command", "")
                elif name in ("Edit", "Write"):
                    kind, text = "edit", inputs.get("file_path", "")
                else:
                    kind, text = "tool", name
            elif block_type == "text":
                kind, text = "message", block.get("text", "")
            elif block_type == "thinking":
                kind, text = "thinking", block.get("thinking", "")
    if kind is None:
        return None
    return {"at": at, "kind": kind, "text": _line(text if isinstance(text, str) else "")}


class EventTail:
    """Keep a byte cursor; read at most the last 256 KiB after a reset or large append.

    An unterminated last line is retained until its newline arrives. A malformed
    complete line is discarded. Inode replacement and truncation reset the cursor.
    """

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.signature = None
        self._pending = b""

    def read(self):
        try:
            metadata = self.path.lstat()
            signature = (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
            if not stat.S_ISREG(metadata.st_mode) or signature == self.signature:
                return None
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    return None
                signature = (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
                reset = (self.signature is None or signature[:2] != self.signature[:2]
                         or metadata.st_size < self.offset
                         or (metadata.st_size == self.offset and signature[2] != self.signature[2]))
                if reset:
                    self.offset = 0
                    self._pending = b""
                skip_first = metadata.st_size - self.offset > TAIL_BYTES
                if skip_first:
                    self.offset = metadata.st_size - TAIL_BYTES
                    self._pending = b""
                stream.seek(self.offset)
                chunk = stream.read(TAIL_BYTES)
                self.offset = stream.tell()
                self.signature = signature
        except OSError:
            return None

        lines = (self._pending + chunk).split(b"\n")
        self._pending = lines.pop()
        if skip_first and lines:
            lines.pop(0)
        latest = None
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            activity = _activity(event)
            if activity is not None:
                latest = activity
        return latest
