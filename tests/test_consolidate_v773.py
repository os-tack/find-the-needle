#!/usr/bin/env python3
"""Regression tests for the v7.7.3 publication-blocker fixes in
consolidate_scores.py.

Covers three independent-review defects:
  F2  snapshot mis-attribution — receipt-driven snapshot identity (a v7.7.3
      cell must not fall into the v7.7.1 "july09" bucket) + fail-closed
      receiptless bucketing + NO regression for june16/july02/july09/july09v2.
  F3  retry-cost omission — a multi-sample cell must publish the TRUE TOTAL
      spend summed across every F7 attempt, not just the final attempt.
  F6  sonnet-5 driver — claude-sonnet-5 is a Tier-1 native CpuDriver model and
      must classify as native_driver, not generic_kernel.

Offline, stdlib-only. Runs via either:
    python3 -m unittest discover -s tests
    python3 -m pytest tests/test_consolidate_v773.py
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import consolidate_scores as cs


def _v773_cell(bench="api-version-field-drop"):
    """Canonical .score.json shape for a v7.7.3 cell = the FINAL (3rd) F7
    attempt. Mirrors runs/claude-fable-5-kernel-cpu/api-version-field-drop."""
    return {
        "benchmark": bench,
        "timestamp": "2026-07-09T18:22:10Z",
        "bench_binary": {
            "version": "7.7.3",
            "build_sha": "unknown",
            "path": "frozen-bin/ostk-host-bb432d9879e8",
        },
        "resolved": False,
        "fresh_input_tokens": 688,
        "cache_read_tokens": 19098,
        "cache_create_tokens": 291,
        "input_tokens": 688,
        "output_tokens": 150,
        "estimated_cost_usd": 0.0371155,
        "token_cost": 20227,
        "turns_to_fix": 3,
        "tool_uses": 3,
        "wall_clock": 12.0,
    }


def _three_attempt_sidecar():
    """3 F7 attempts: miss, hit, miss (majority = unsolved). Atomic token
    buckets per attempt, copied from the real fable-5 kernel-cpu cell."""
    return [
        {"sample_attempt": 1, "resolved": False, "fresh_input_tokens": 690,
         "cache_read_tokens": 12678, "cache_create_tokens": 6711,
         "input_tokens": 690, "output_tokens": 146,
         "estimated_cost_usd": 0.11076549999999999, "token_cost": 20225,
         "turns_to_fix": 3, "tool_uses": 3, "wall_clock": 10.0},
        {"sample_attempt": 2, "resolved": True, "fresh_input_tokens": 5301,
         "cache_read_tokens": 86826, "cache_create_tokens": 6403,
         "input_tokens": 5301, "output_tokens": 2035,
         "estimated_cost_usd": 0.32162349999999995, "token_cost": 100565,
         "turns_to_fix": 10, "tool_uses": 12, "wall_clock": 40.0},
        {"sample_attempt": 3, "resolved": False, "fresh_input_tokens": 688,
         "cache_read_tokens": 19098, "cache_create_tokens": 291,
         "input_tokens": 688, "output_tokens": 150,
         "estimated_cost_usd": 0.0371155, "token_cost": 20227,
         "turns_to_fix": 3, "tool_uses": 3, "wall_clock": 12.0},
    ]


def _write_cell(dir_path: Path, bench: str, entry: dict, samples=None):
    score_fp = dir_path / f"{bench}.score.json"
    score_fp.write_text(json.dumps(entry))
    if samples is not None:
        (dir_path / f"{bench}.samples.json").write_text(json.dumps(samples))
    return score_fp


# ---------------------------------------------------------------------------
# F2 — AC1 & AC2: receipt-driven snapshot identity
# ---------------------------------------------------------------------------
class TestSnapshotReceiptIdentity(unittest.TestCase):

    def test_ac1_v773_cell_files_to_july09v3(self):
        """AC1: a 2026-07-09 cell with bench_binary.version 7.7.3 → july09v3,
        whose label resolves to 'v7.7.3'."""
        cell = {"timestamp": "2026-07-09T18:22:10Z",
                "bench_binary": {"version": "7.7.3"}}
        snap = cs.snapshot_of(cell)
        self.assertEqual(snap, "july09v3")
        self.assertEqual(cs.SNAPSHOT_OSTK_VERSION[snap], "v7.7.3")
        # v7.7.3 is the current latest board.
        self.assertEqual(cs.OSTK_VERSION, "v7.7.3")
        # Appended newest-last so SNAPSHOTS.index ranking still works.
        self.assertEqual(cs.SNAPSHOTS[-1], "july09v3")

    def test_ac2_no_regression_v771_v772(self):
        """AC2: version receipts still bucket 7.7.1→july09, 7.7.2→july09v2."""
        v771 = {"timestamp": "2026-07-09T09:00:00Z",
                "bench_binary": {"version": "7.7.1"}}
        v772 = {"timestamp": "2026-07-09T13:00:00Z",
                "bench_binary": {"version": "7.7.2"}}
        self.assertEqual(cs.snapshot_of(v771), "july09")
        self.assertEqual(cs.SNAPSHOT_OSTK_VERSION[cs.snapshot_of(v771)], "v7.7.1")
        self.assertEqual(cs.snapshot_of(v772), "july09v2")
        self.assertEqual(cs.SNAPSHOT_OSTK_VERSION[cs.snapshot_of(v772)], "v7.7.2")

    def test_ac2_receiptless_july09_not_filed_up(self):
        """AC2 fail-closed: a 2026-07-09 cell with NO version receipt stays in
        july09 (v7.7.1, the day's oldest binary) — never promoted to v7.7.3."""
        no_receipt = {"timestamp": "2026-07-09T20:00:00Z"}
        empty_receipt = {"timestamp": "2026-07-09T20:00:00Z",
                         "bench_binary": {"version": ""}}
        self.assertEqual(cs.snapshot_of(no_receipt), "july09")
        self.assertEqual(cs.snapshot_of(empty_receipt), "july09")
        self.assertNotEqual(cs.snapshot_of(no_receipt), "july09v3")

    def test_ac2_no_regression_older_eras(self):
        """AC2: v7.6.0 june16/july02 (one binary, two windows) still split by
        the date ladder; receipt map intentionally omits 7.6.0."""
        june = {"timestamp": "2026-06-16T10:00:00Z",
                "bench_binary": {"version": "7.6.0"}}
        july02 = {"timestamp": "2026-07-02T10:00:00Z",
                  "bench_binary": {"version": "7.6.0"}}
        june_noreceipt = {"timestamp": "2026-06-16T10:00:00Z"}
        self.assertEqual(cs.snapshot_of(june), "june16")
        self.assertEqual(cs.snapshot_of(july02), "july02")
        self.assertEqual(cs.snapshot_of(june_noreceipt), "june16")

    def test_receipt_beats_date_and_prefix_precision(self):
        """Receipt wins even if the payload date is stale; and 7.7.30 must NOT
        collide with the 7.7.3 prefix."""
        # A 7.7.3 receipt with a WRONG/old date still files by receipt.
        stale_date = {"timestamp": "2026-06-16T10:00:00Z",
                      "bench_binary": {"version": "7.7.3"}}
        self.assertEqual(cs.snapshot_of(stale_date), "july09v3")
        # Dotted-suffix build still resolves; unrelated 7.7.30 does not.
        self.assertEqual(cs.snapshot_for_version("7.7.3.1"), "july09v3")
        self.assertIsNone(cs.snapshot_for_version("7.7.30"))
        self.assertIsNone(cs.snapshot_for_version(""))


# ---------------------------------------------------------------------------
# F3 — AC3: retry-cost = summed total spend across attempts
# ---------------------------------------------------------------------------
class TestRetryCostTotal(unittest.TestCase):

    def test_ac3_multi_sample_sums_token_and_cost(self):
        model = "claude-fable-5"
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            bench = "api-version-field-drop"

            # BEFORE: canonical score only (no sidecar) → final-attempt spend.
            _write_cell(d, bench, _v773_cell(bench), samples=None)
            entry_before, skip = cs.load_score(d / f"{bench}.score.json")
            self.assertIsNone(skip)
            sum_before = cs.arm_summary(entry_before, model)
            cost_before = sum_before["cost_usd"]
            billed_before = sum_before["billed_input_tokens"]
            out_before = sum_before["output_tokens"]

            # AFTER: same canonical + a 3-attempt sidecar → total retry spend.
            _write_cell(d, bench, _v773_cell(bench), samples=_three_attempt_sidecar())
            entry_after, skip = cs.load_score(d / f"{bench}.score.json")
            self.assertIsNone(skip)
            sum_after = cs.arm_summary(entry_after, model)
            cost_after = sum_after["cost_usd"]
            billed_after = sum_after["billed_input_tokens"]
            out_after = sum_after["output_tokens"]

            # Resolution stays MAJORITY vote (1 of 3 solved → unsolved).
            self.assertFalse(entry_after["resolved"])
            self.assertEqual(entry_after["samples_count"], 3)
            self.assertEqual(entry_after["retry_attempts"], 3)

            # Token totals are summed across all 3 attempts.
            self.assertEqual(entry_after["output_tokens"], 146 + 2035 + 150)
            self.assertEqual(entry_after["fresh_input_tokens"], 690 + 5301 + 688)
            self.assertEqual(entry_after["cache_read_tokens"], 12678 + 86826 + 19098)
            self.assertEqual(entry_after["cache_create_tokens"], 6711 + 6403 + 291)
            self.assertEqual(entry_after["estimated_cost_usd"],
                             0.11076549999999999 + 0.32162349999999995 + 0.0371155)

            # Final-attempt values preserved for transparency.
            self.assertEqual(entry_after["final_attempt_output_tokens"], 150)
            self.assertEqual(entry_after["final_attempt_estimated_cost_usd"], 0.0371155)

            # Published cost & billed tokens strictly increase to the true total.
            self.assertGreater(cost_after, cost_before)
            self.assertGreater(billed_after, billed_before)
            self.assertEqual(out_after, out_before + 146 + 2035)  # +attempts 1,2

            # Exact repriced totals (bucket-split, fable-5 rate $10/$50 per M).
            # arm_summary rounds cost to 6 places, so compare at 5-place tol.
            self.assertAlmostEqual(cost_before, 0.0371155, places=5)
            self.assertAlmostEqual(cost_after, 0.4695045, places=5)
            self.assertEqual(billed_before, 688 + 19098 + 291)
            self.assertEqual(billed_after, 138686)

            # Surface numbers in the test log for the report.
            print(f"\n[AC3] retry-cost before/after (fable-5, 3 attempts):"
                  f"\n      cost_usd  {cost_before:.6f} -> {cost_after:.6f}"
                  f"  ({cost_after / cost_before:.1f}x)"
                  f"\n      billed_in {billed_before} -> {billed_after}"
                  f"\n      output    {out_before} -> {out_after}")

    def test_single_sample_unchanged(self):
        """N==1 sidecar (or no sidecar) must NOT alter token/cost — no
        retry_attempts key, values identical to the canonical score."""
        model = "claude-fable-5"
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            bench = "single"
            one = [copy.deepcopy(_v773_cell(bench))]
            one[0]["sample_attempt"] = 1
            _write_cell(d, bench, _v773_cell(bench), samples=one)
            entry, skip = cs.load_score(d / f"{bench}.score.json")
            self.assertIsNone(skip)
            self.assertNotIn("retry_attempts", entry)
            self.assertEqual(entry["output_tokens"], 150)
            self.assertEqual(entry["estimated_cost_usd"], 0.0371155)


# ---------------------------------------------------------------------------
# F6 — AC4: sonnet-5 native driver classification
# ---------------------------------------------------------------------------
class TestSonnet5Driver(unittest.TestCase):

    def test_ac4_sonnet5_is_native_driver(self):
        norm = cs.normalize_model("claude-sonnet-5")
        self.assertEqual(norm, "claude-sonnet-5")
        self.assertIn(norm, cs.CPU_DRIVER_MODELS)
        self.assertEqual(cs.kernel_arm_type_for(norm), "native_driver")

    def test_ac4_native_siblings_still_native(self):
        for m in ("claude-opus-4-8", "claude-fable-5"):
            self.assertEqual(cs.kernel_arm_type_for(m), "native_driver")

    def test_ac4_generic_control_unaffected(self):
        # gpt-5.5 has no native driver → must remain generic_kernel.
        self.assertEqual(
            cs.kernel_arm_type_for(cs.normalize_model("gpt-5.5")), "generic_kernel")


# ---------------------------------------------------------------------------
# AC5 — the exercised functions never touch public/
# ---------------------------------------------------------------------------
class TestNoPublicWrite(unittest.TestCase):

    def test_ac5_functions_do_not_write_public(self):
        """snapshot_of / load_score / arm_summary / kernel_arm_type_for are the
        only functions this change touches; none open the published outputs.
        Redirect the module output paths at temp files and confirm neither is
        created by exercising those functions."""
        orig_scores, orig_exp = cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            cs.SCORES_OUTPUT = d / "scores.json"
            cs.EXPERIMENT_OUTPUT = d / "experiment-scores.json"
            try:
                bench = "b"
                _write_cell(d, bench, _v773_cell(bench),
                            samples=_three_attempt_sidecar())
                entry, _ = cs.load_score(d / f"{bench}.score.json")
                cs.arm_summary(entry, "claude-fable-5")
                cs.snapshot_of(entry)
                cs.kernel_arm_type_for("claude-sonnet-5")
                self.assertFalse(cs.SCORES_OUTPUT.exists())
                self.assertFalse(cs.EXPERIMENT_OUTPUT.exists())
            finally:
                cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT = orig_scores, orig_exp


if __name__ == "__main__":
    unittest.main(verbosity=2)
