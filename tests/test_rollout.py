#!/usr/bin/env python3
"""Focused coverage for the shared rollout reader and token_count decoder."""
import io
import json
import runpy
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "plugins/codex-worker/skills/codex-worker/scripts/rollout.py"


class RolloutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        decoder = runpy.run_path(str(MODULE))
        cls.read_events = staticmethod(decoder["read_events"])
        cls.decode = staticmethod(decoder["decode_token_count"])

    @staticmethod
    def event(**payload):
        return {"type": "event_msg", "payload": {"type": "token_count", **payload}}

    def test_reader_skips_malformed_lines_and_preserves_order_and_values(self):
        lines = ['{"id": 1}\n', "broken\n", "\n", '[2, 3]\n', 'null\n', '{"id": 4}\n']
        expected = [{"id": 1}, [2, 3], None, {"id": 4}]
        self.assertEqual(list(self.read_events(lines)), expected)
        self.assertEqual(list(self.read_events(reversed(lines))), list(reversed(expected)))
        self.assertEqual(list(self.read_events(line.encode() for line in lines)), expected)

    def test_reader_seeks_to_requested_stream_offset(self):
        first = json.dumps({"id": 1}) + "\n"
        rest = 'broken\n{"id": 2}\n'
        for stream in (io.StringIO(first + rest), io.BytesIO((first + rest).encode())):
            with self.subTest(stream=type(stream).__name__):
                self.assertEqual(list(self.read_events(stream, offset=len(first))), [{"id": 2}])

    def test_reader_uses_current_position_when_offset_is_omitted(self):
        stream = io.BytesIO(b'{"id": 1}\n{"id": 2}\n')
        stream.readline()
        self.assertEqual(list(self.read_events(stream)), [{"id": 2}])

    def test_non_token_events_are_ignored_without_inspecting_info_or_limits(self):
        for event in ({}, {"payload": None}, {"payload": {}},
                      {"payload": {"type": "task_started", "info": [1], "rate_limits": [1]}}):
            with self.subTest(event=event):
                self.assertIsNone(self.decode(event))

    def test_sample_keeps_raw_usage_windows_and_event_type(self):
        usage = {"input_tokens": 42, "total_tokens": 99, "future_counter": 7}
        primary = {"window_minutes": 300, "used_percent": 5}
        secondary = {"window_minutes": 10080, "used_percent": 10}
        limits = {"primary": primary, "secondary": secondary, "credits": {"balance": "1"}}
        event = self.event(info={"total_token_usage": usage}, rate_limits=limits)
        event["type"] = "response_item"
        sample = self.decode(event)
        self.assertEqual(sample.event_type, "response_item")
        self.assertIs(sample.total_token_usage, usage)
        self.assertIs(sample.rate_limits, limits)
        self.assertEqual(sample.windows, (primary, secondary))
        self.assertIs(sample.windows[0], primary)
        self.assertIs(sample.windows[1], secondary)

    def test_limit_only_sample_has_no_counters_and_preserves_single_windows(self):
        for name, expected in (("primary", (7, None)), ("secondary", (None, 7))):
            with self.subTest(name=name):
                sample = self.decode(self.event(rate_limits={name: 7}))
                self.assertEqual(sample.windows, expected)
                self.assertIsNone(sample.total_token_usage)
                self.assertEqual(sample.tokens, {"input": None, "cached_input": None,
                                                 "output": None, "reasoning": None})

    def test_counters_accept_only_nonnegative_finite_ints_and_floats(self):
        fields = {"input": "input_tokens", "cached_input": "cached_input_tokens",
                  "output": "output_tokens", "reasoning": "reasoning_output_tokens"}
        for value in (0, 3, 0.0, 1.5, -0.0, None, True, False, "3", -1, -0.5,
                      float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                sample = self.decode(self.event(info={"total_token_usage": dict.fromkeys(fields.values(), value)}))
                accepted = type(value) in (int, float) and value >= 0 and value < float("inf")
                self.assertEqual(sample.tokens, dict.fromkeys(fields, value if accepted else None))
        self.assertEqual(self.decode(self.event(info={"total_token_usage": {"input_tokens": 7}})).tokens,
                         {"input": 7, "cached_input": None, "output": None, "reasoning": None})

    def test_non_mapping_usage_stays_raw_but_has_no_valid_counters(self):
        usage = [1, 2]
        sample = self.decode(self.event(info={"total_token_usage": usage}))
        self.assertIs(sample.total_token_usage, usage)
        self.assertEqual(sample.tokens, {"input": None, "cached_input": None,
                                         "output": None, "reasoning": None})

    def test_malformed_info_does_not_prevent_limits_consumers(self):
        primary = {"window_minutes": 300}
        sample = self.decode(self.event(info=[1], rate_limits={"primary": primary}))
        self.assertEqual(sample.windows, (primary, None))
        self.assertEqual(sample.event_type, "event_msg")
        with self.assertRaises(AttributeError):
            _ = sample.total_token_usage
        with self.assertRaises(AttributeError):
            _ = sample.tokens

    def test_malformed_limits_do_not_prevent_counter_consumers(self):
        limits = [1]
        sample = self.decode(self.event(info={"total_token_usage": {"input_tokens": 5}}, rate_limits=limits))
        self.assertIs(sample.rate_limits, limits)
        self.assertEqual(sample.tokens["input"], 5)
        with self.assertRaises(AttributeError):
            _ = sample.windows

    def test_huge_counter_overflow_is_lazy(self):
        primary = {"window_minutes": 300}
        sample = self.decode(self.event(info={"total_token_usage": {"input_tokens": 10 ** 1000}},
                                        rate_limits={"primary": primary}))
        self.assertEqual(sample.windows, (primary, None))
        self.assertEqual(sample.total_token_usage["input_tokens"], 10 ** 1000)
        with self.assertRaises(OverflowError):
            _ = sample.tokens


if __name__ == "__main__":
    unittest.main()
