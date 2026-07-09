#!/usr/bin/env python3
"""Unit tests for cell_validity.py — fail-closed, PER-AXIS cell classification.

Offline, stdlib-only:  python3 -m unittest discover -s tests

AXIS SEMANTICS UNDER TEST (the trust doctrine):
  - SOLVE-axis faults (malformed / no receipt / arm mismatch / deadline /
    zero-work / duplicate-column) → fully INVALID, both axes dead.
  - COST-axis faults (zero token accounting on a completed run) → COST_INVALID:
    the solve verdict STANDS, the cost axis is unavailable and never priced.
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
        self.assertTrue(cls.solve_valid)
        self.assertTrue(cls.cost_valid)

    def test_arm_mismatch_is_fully_invalid(self):
        # SOLVE-axis fault: a wrong-arm receipt has no trustworthy verdict AND
        # no attributable spend — both axes dead.
        cell = kernel_cell(agent="claude-sonnet-4-6-native", arm="native")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertFalse(cls.cost_valid)
        self.assertTrue(any(r.startswith(cv.REASON_ARM_MISMATCH) for r in cls.solve_reasons))

    def test_no_receipt_is_fully_invalid(self):
        # bare '<model>' agent + arm='default' (the pre-fix arm-drop residue)
        cell = kernel_cell(agent="claude-opus-4-8", arm="default")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertIn(cv.REASON_NO_ARM_RECEIPT, cls.solve_reasons)

    def test_cpu_alias_normalizes(self):
        self.assertEqual(cv.normalize_arm("cpu"), "kernel-cpu")

    def test_no_requested_arm_skips_receipt_check(self):
        cell = kernel_cell(agent="claude-opus-4-8", arm="default")
        cls = cv.classify_cell(cell, requested_arm=None)
        self.assertEqual(cls.status, cv.VALID)


class TestCostAxis(unittest.TestCase):
    """Zero token accounting on a completed run: the cost axis is unavailable
    but the oracle verdict + arm receipt stand — COST_INVALID, not INVALID.
    This is the axis split that redeems the zero-billed native solve cells."""

    def test_zero_output_on_completed_run_is_cost_invalid(self):
        # kimi-native shape: resolved, 5 turns, input recorded, output/billed 0.
        cell = kernel_cell(
            agent="kimi-k2.6-native", arm="native", stop_reason="pass",
            turns_to_fix=5, resolved=True,
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=12962, output_tokens=0,
        )
        cls = cv.classify_cell(cell, requested_arm="native")
        self.assertEqual(cls.status, cv.COST_INVALID)
        self.assertTrue(cls.solve_valid)          # solve verdict stands
        self.assertFalse(cls.cost_valid)          # cost axis unavailable
        self.assertIn(cv.REASON_ZERO_TOKEN_OUTPUT, cls.cost_reasons)
        self.assertEqual(cls.solve_reasons, [])

    def test_zero_input_on_completed_run_is_cost_invalid(self):
        cell = kernel_cell(
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=0, output_tokens=1017,
        )
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.COST_INVALID)
        self.assertTrue(cls.solve_valid)
        self.assertIn(cv.REASON_ZERO_TOKEN_INPUT, cls.cost_reasons)

    def test_zero_billed_on_completed_run_is_cost_invalid(self):
        # deepseek/grok-native shape: billing-aware writer, billed_tokens=0,
        # empty buckets, only a folded total in input_tokens. The harness
        # never recorded billing — cost axis UNAVAILABLE, never priced at the
        # rate card (the old acceptance synthesized native cost 3-29x over
        # the provider-billed truth). The SOLVE verdict (oracle + receipt)
        # survives: these are the 130 redeemed native solve cells.
        cell = kernel_cell(
            agent="deepseek-v4-pro-native", arm="native", stop_reason="pass",
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=56327, output_tokens=762,
        )
        cls = cv.classify_cell(cell, requested_arm="native")
        self.assertEqual(cls.status, cv.COST_INVALID)
        self.assertTrue(cls.solve_valid)
        self.assertFalse(cls.cost_valid)
        self.assertIn(cv.REASON_ZERO_BILLED, cls.cost_reasons)

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

    def test_cost_fault_never_rescues_a_solve_fault(self):
        # arm mismatch + zero accounting → fully INVALID (solve axis rules).
        cell = kernel_cell(
            agent="claude-sonnet-4-6-native", arm="native",
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=0, output_tokens=1017,
        )
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertFalse(cls.cost_valid)
        self.assertTrue(cls.solve_reasons)
        self.assertTrue(cls.cost_reasons)   # both recorded, both visible


class TestSolveAxis(unittest.TestCase):
    def test_deadline_is_fully_invalid_not_silent(self):
        # An infra stall is not a model verdict — no oracle claim survives.
        cell = kernel_cell(resolved=False, stop_reason="deadline_exceeded",
                           turns_to_fix=0, tool_uses=0,
                           fresh_input_tokens=0, cache_read_tokens=0,
                           cache_create_tokens=0, billed_tokens=0,
                           input_tokens=0, output_tokens=0)
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertFalse(cls.cost_valid)
        self.assertIn(cv.REASON_DEADLINE, cls.solve_reasons)

    def test_stall_is_fully_invalid(self):
        # stall_watch kill: silent waiting is a defect — the record is loud
        # and the cell has no model verdict on either axis.
        cell = kernel_cell(resolved=False, stop_reason="stall_watch_killed",
                           turns_to_fix=3, output_tokens=200)
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertIn(cv.REASON_STALL, cls.solve_reasons)

    def test_zero_work_is_fully_invalid(self):
        cell = kernel_cell(resolved=True, stop_reason="exit_code: 1",
                           turns_to_fix=0, tool_uses=0,
                           fresh_input_tokens=0, cache_read_tokens=0,
                           cache_create_tokens=0, billed_tokens=0,
                           input_tokens=0, output_tokens=0)
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_ZERO_WORK, cls.solve_reasons)

    def test_passive_verify_zero_tokens_is_cost_invalid(self):
        # stop_reason='pass' with 0 turns / 0 tokens is a zero-ACCOUNTING pass.
        # A genuine passive-verify $0 cell is indistinguishable from a run whose
        # real spend was lost (native error_max_turns results are stamped as a
        # 0-turn/0-token pass), so it fails closed on COST: COST_INVALID with
        # the solve verdict preserved. (Previously this exemption returned VALID
        # — the P0 cost-accounting defect.)
        cell = kernel_cell(agent="qwen3-coder-plus-native", arm="native",
                           resolved=True, stop_reason="pass",
                           turns_to_fix=0, tool_uses=0,
                           fresh_input_tokens=0, cache_read_tokens=0,
                           cache_create_tokens=0, billed_tokens=0,
                           input_tokens=0, output_tokens=0)
        cls = cv.classify_cell(cell, requested_arm="native")
        self.assertEqual(cls.status, cv.COST_INVALID, cls.reasons)
        self.assertIn(cv.REASON_ZERO_ACCOUNTING_PASS, cls.cost_reasons)
        self.assertTrue(cls.solve_valid)   # solve verdict preserved
        self.assertFalse(cls.cost_valid)

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
        self.assertFalse(cls.solve_valid)
        self.assertIn(cv.REASON_MALFORMED, cls.solve_reasons)

    def test_missing_benchmark(self):
        cls = cv.classify_cell({"resolved": True}, requested_arm="native")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(cv.REASON_MALFORMED, cls.reasons)


class TestAxisMachinery(unittest.TestCase):
    def test_reason_axis_mapping(self):
        self.assertEqual(cv.reason_axis(cv.REASON_ZERO_BILLED), "cost")
        self.assertEqual(cv.reason_axis(cv.REASON_ZERO_TOKEN_INPUT), "cost")
        self.assertEqual(cv.reason_axis(cv.REASON_ZERO_TOKEN_OUTPUT), "cost")
        self.assertEqual(cv.reason_axis(cv.REASON_DEADLINE), "solve")
        self.assertEqual(cv.reason_axis(cv.REASON_NATIVE_DRIVER_DUP), "solve")
        self.assertEqual(
            cv.reason_axis(f"{cv.REASON_ARM_MISMATCH}:requested=kernel-cpu,executed=native"),
            "solve")

    def test_unknown_reason_fails_closed_to_solve_axis(self):
        # An unrecognized reason must poison the whole cell, never leak into
        # solve aggregates as a mere cost annotation.
        self.assertEqual(cv.reason_axis("some_future_reason"), "solve")
        cls = cv.classify_reasons(["some_future_reason"])
        self.assertEqual(cls.status, cv.INVALID)

    def test_classify_reasons_dup_is_fully_invalid(self):
        # Consolidator-appended dedup reason: one execution in two columns is
        # a solve-axis fault (whole-cell presentation fraud, not a billing gap).
        cls = cv.classify_reasons([cv.REASON_NATIVE_DRIVER_DUP])
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertFalse(cls.cost_valid)

    def test_classify_reasons_cost_only(self):
        cls = cv.classify_reasons([cv.REASON_ZERO_BILLED])
        self.assertEqual(cls.status, cv.COST_INVALID)
        self.assertTrue(cls.solve_valid)
        self.assertFalse(cls.cost_valid)

    def test_classify_reasons_empty_is_valid(self):
        cls = cv.classify_reasons([])
        self.assertEqual(cls.status, cv.VALID)
        self.assertTrue(cls.solve_valid and cls.cost_valid)


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
