#!/usr/bin/env python3
"""Unit tests for cell_validity.py — fail-closed cell classification.

Offline, stdlib-only:  python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cell_validity as cv


def kernel_cell(**over):
    """A healthy rust-writer kernel-cpu cell (June schema)."""
    base = {
        "benchmark": "off-by-one-pagination",
        "agent": "claude-sonnet-4-6-kernel-cpu",
        "arm": "kernel",  # rust writer records 'kernel' even for kernel-cpu runs
        "resolved": True,
        "turns_to_fix": 9,
        "input_tokens": 2873,
        "output_tokens": 1017,
        "fresh_input_tokens": 2873,
        "cache_read_tokens": 59316,
        "cache_create_tokens": 3517,
        "billed_tokens": 65706,
        "tool_uses": 10,
        "stop_reason": "end_turn",
    }
    base.update(over)
    return base


class TestArmReceipt(unittest.TestCase):
    def test_agent_suffix_beats_arm_field(self):
        # rust writer: dir/agent say kernel-cpu, payload arm says 'kernel' —
        # the agent receipt wins, so the cell is VALID for a kernel-cpu request.
        cls = cv.classify_cell(kernel_cell(), requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_arm_mismatch_is_invalid(self):
        cell = kernel_cell(agent="claude-sonnet-4-6-native", arm="native")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertTrue(any(r.startswith(cv.REASON_ARM_MISMATCH) for r in cls.reasons))

    def test_no_receipt_is_invalid(self):
        # bare '<model>' agent + arm='default' (the pre-fix arm-drop residue)
        cell = kernel_cell(agent="claude-opus-4-8", arm="default")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_NO_ARM_RECEIPT, cls.reasons)

    def test_cpu_alias_normalizes(self):
        self.assertEqual(cv.normalize_arm("cpu"), "kernel-cpu")

    def test_no_requested_arm_skips_receipt_check(self):
        cell = kernel_cell(agent="claude-opus-4-8", arm="default")
        cls = cv.classify_cell(cell, requested_arm=None)
        self.assertEqual(cls.status, cv.VALID)


class TestTokenAccounting(unittest.TestCase):
    def test_zero_output_on_completed_run_is_invalid(self):
        # kimi-native shape: resolved, 5 turns, input recorded, output/billed 0.
        cell = kernel_cell(
            agent="kimi-k2.6-native", arm="native", stop_reason="pass",
            turns_to_fix=5, resolved=True,
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=12962, output_tokens=0,
        )
        cls = cv.classify_cell(cell, requested_arm="native")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_ZERO_TOKEN_OUTPUT, cls.reasons)

    def test_zero_input_on_completed_run_is_invalid(self):
        cell = kernel_cell(
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=0, output_tokens=1017,
        )
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_ZERO_TOKEN_INPUT, cls.reasons)

    def test_zero_billed_on_completed_run_is_invalid(self):
        # deepseek/grok-native shape: billing-aware writer, billed_tokens=0,
        # empty buckets, only a folded total in input_tokens. The harness
        # never recorded billing — must be INVALID, never priced at the rate
        # card (the old acceptance synthesized native cost 3-29x over the
        # provider-billed truth, flattering the kernel arm).
        cell = kernel_cell(
            agent="deepseek-v4-pro-native", arm="native", stop_reason="pass",
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=56327, output_tokens=762,
        )
        cls = cv.classify_cell(cell, requested_arm="native")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_ZERO_BILLED, cls.reasons)

    def test_legacy_payload_without_billed_key_stays_valid(self):
        # pre-→2062 writers never wrote billed_tokens; their input_tokens IS
        # the billing-era record — exempt from the zero-billed rule.
        cell = kernel_cell(
            agent="deepseek-v4-pro-native", arm="native", stop_reason="pass",
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            input_tokens=56327, output_tokens=762,
        )
        del cell["billed_tokens"]
        cls = cv.classify_cell(cell, requested_arm="native")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_deadline_is_invalid_not_silent(self):
        cell = kernel_cell(resolved=False, stop_reason="deadline_exceeded",
                           turns_to_fix=0, tool_uses=0,
                           fresh_input_tokens=0, cache_read_tokens=0,
                           cache_create_tokens=0, billed_tokens=0,
                           input_tokens=0, output_tokens=0)
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_DEADLINE, cls.reasons)

    def test_zero_work_is_invalid(self):
        cell = kernel_cell(resolved=True, stop_reason="exit_code: 1",
                           turns_to_fix=0, tool_uses=0,
                           fresh_input_tokens=0, cache_read_tokens=0,
                           cache_create_tokens=0, billed_tokens=0,
                           input_tokens=0, output_tokens=0)
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_ZERO_WORK, cls.reasons)

    def test_passive_verify_zero_tokens_is_valid(self):
        # stop_reason='pass' with 0 turns / 0 tokens: passive verification.
        cell = kernel_cell(agent="qwen3-coder-plus-native", arm="native",
                           resolved=True, stop_reason="pass",
                           turns_to_fix=0, tool_uses=0,
                           fresh_input_tokens=0, cache_read_tokens=0,
                           cache_create_tokens=0, billed_tokens=0,
                           input_tokens=0, output_tokens=0)
        cls = cv.classify_cell(cell, requested_arm="native")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_july_schema_without_stop_reason_is_valid(self):
        # run_arms.py July schema has no stop_reason at all.
        cell = kernel_cell(agent="claude-opus-4-8-kernel-cpu", arm="kernel-cpu")
        del cell["stop_reason"]
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)


class TestMalformed(unittest.TestCase):
    def test_not_a_dict(self):
        cls = cv.classify_cell(["not", "a", "score"], requested_arm="native")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_MALFORMED, cls.reasons)

    def test_missing_benchmark(self):
        cls = cv.classify_cell({"resolved": True}, requested_arm="native")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_MALFORMED, cls.reasons)


class TestHelpers(unittest.TestCase):
    def test_split_agent_arm(self):
        self.assertEqual(cv.split_agent_arm("claude-sonnet-4-6-kernel-cpu"),
                         ("claude-sonnet-4-6", "kernel-cpu"))
        self.assertEqual(cv.split_agent_arm("gpt-5.5-kernel"), ("gpt-5.5", "kernel"))
        self.assertEqual(cv.split_agent_arm("grok-4.3-native"), ("grok-4.3", "native"))
        self.assertIsNone(cv.split_agent_arm("claude-opus-4-8"))
        self.assertIsNone(cv.split_agent_arm(None))

    def test_token_accounting_max_of_representations(self):
        inp, out = cv.token_accounting(kernel_cell())
        self.assertEqual(inp, 65706)  # billed == bucket sum here
        self.assertEqual(out, 1017)


if __name__ == "__main__":
    unittest.main()
