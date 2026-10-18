"""Decode rollout JSON lines and token_count data without choosing quota windows."""
import json
import math


TOKEN_FIELDS = {"input": "input_tokens", "cached_input": "cached_input_tokens",
                "output": "output_tokens", "reasoning": "reasoning_output_tokens"}


def read_events(lines, offset=None):
    """Read JSON values, skipping malformed lines; optionally seek an open stream.

    Callers own the stream and can pass a snapshot of lines in either order.
    """
    if offset is not None:
        lines.seek(offset)
    for line in lines:
        try:
            yield json.loads(line)
        except ValueError:
            continue


class TokenCount:
    """One token_count payload, retaining raw data alongside validated counters.

    Decode fields on access: malformed usage must not invalidate quota-only
    readers, and stats retains its original handling of raw cumulative usage.
    """

    def __init__(self, event, payload):
        self.event_type = event.get("type")
        self.payload = payload

    @property
    def total_token_usage(self):
        return (self.payload.get("info") or {}).get("total_token_usage")

    @property
    def tokens(self):
        usage = self.total_token_usage
        tokens = {key: None for key in TOKEN_FIELDS}
        for key, field in TOKEN_FIELDS.items():
            value = usage.get(field) if isinstance(usage, dict) else None
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                tokens[key] = value
        return tokens

    @property
    def rate_limits(self):
        return self.payload.get("rate_limits")

    @property
    def windows(self):
        limits = self.rate_limits or {}
        return tuple(limits.get(name) for name in ("primary", "secondary"))


def decode_token_count(event):
    """Return a token_count sample, or None for other rollout events."""
    payload = event.get("payload") or {}
    if payload.get("type") != "token_count":
        return None
    return TokenCount(event, payload)
