#!/usr/bin/env python3
"""Tests for consolidate_scores.py — cost-inversion fix, fail-closed PER-AXIS
validity (solve vs cost), native_driver dedup, teardown_masked passthrough,
absent-vs-INVALID-vs-COST_INVALID, run-date snapshot selection.

Offline, stdlib-only, runs in seconds:  python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import consolidate_scores as cs
import cell_validity as cv
import publish_boards as pb


# ---------------------------------------------------------------------------
# Fixture payloads (shapes copied from real runs/ cells)
# ---------------------------------------------------------------------------

def sonnet_native_cell(bench="bench-a"):
    """claude-code native: full cache split (fresh 7 / read 115613 / create 9403)."""
    return {
        "benchmark": bench, "agent": "claude-sonnet-4-6-native", "arm": "native",
        "resolved": True, "turns_to_fix": 7, "tool_uses": 6,
        "input_tokens": 7, "output_tokens": 951, "fresh_input_tokens": 7,
        "cache_read_tokens": 115613, "cache_create_tokens": 9403,
        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        "billed_tokens": 125023, "estimated_cost_usd": 0.0848,
        "stop_reason": "pass", "wall_clock": 37.2, "token_cost": 125974,
        "timestamp": "2026-06-15T18:35:16Z",
    }


def sonnet_kernel_cell(bench="bench-a"):
    """rust-writer kernel-cpu cell: arm says 'kernel', agent says '-kernel-cpu'."""
    return {
        "benchmark": bench, "agent": "claude-sonnet-4-6-kernel-cpu", "arm": "kernel",
        "resolved": True, "turns_to_fix": 9, "tool_uses": 10,
        "input_tokens": 2873, "output_tokens": 1017, "fresh_input_tokens": 2873,
        "cache_read_tokens": 59316, "cache_create_tokens": 3517,
        "cache_create_5m_tokens": 3517, "cache_create_1h_tokens": 0,
        "billed_tokens": 65706, "estimated_cost_usd": 0.0549,
        "stop_reason": "end_turn", "wall_clock": 48.0, "token_cost": 66723,
        "timestamp": "2026-06-16T16:37:30Z",
    }


def deepseek_native_cell(bench="bench-a"):
    """Present-but-zero buckets; real total in input_tokens (inversion leg 1)."""
    return {
        "benchmark": bench, "agent": "deepseek-v4-pro-native", "arm": "native",
        "resolved": True, "turns_to_fix": 6, "tool_uses": 13,
        "input_tokens": 56327, "output_tokens": 762, "fresh_input_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        "billed_tokens": 0, "estimated_cost_usd": 0.0026,
        "stop_reason": "pass", "wall_clock": 22.9, "token_cost": 762,
        "timestamp": "2026-06-15T18:19:50Z",
    }


def kimi_native_cell(bench="bench-a"):
    """Harness-incomplete: completed run, zero output/billed → must be INVALID."""
    return {
        "benchmark": bench, "agent": "kimi-k2.6-native", "arm": "native",
        "resolved": True, "turns_to_fix": 5, "tool_uses": 7,
        "input_tokens": 12962, "output_tokens": 0, "fresh_input_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "billed_tokens": 0, "estimated_cost_usd": 0.0,
        "stop_reason": "pass", "wall_clock": 23.6, "token_cost": 0,
        "timestamp": "2026-06-15T18:38:13Z",
    }


# ---------------------------------------------------------------------------
# Cost computation (unit) — the bb4aa6d5 cost-inversion regression tests
# ---------------------------------------------------------------------------

class TestCostInversionFix(unittest.TestCase):
    def test_present_but_zero_prices_input_tokens(self):
        """Leg 1: empty buckets + real input_tokens and NO provider-billed
        cost must price the real total, not collapse to output-only ≈ $0
        (which made native look free)."""
        cell = deepseek_native_cell()
        cell["estimated_cost_usd"] = 0.0  # no provider-billed number
        cost, basis = cs.compute_cost_ex(cell, "deepseek-v4-pro")
        expected = (56327 * 1.74 + 762 * 3.48) / 1_000_000  # 0.10066074
        self.assertAlmostEqual(cost, expected, places=9)
        self.assertEqual(basis, "total-as-fresh")
        # regression: the broken aggregator produced output-only cost
        broken = 762 * 3.48 / 1_000_000  # 0.00265176
        self.assertGreater(cost, broken * 10)

    def test_present_but_zero_prefers_provider_billed_cost(self):
        """A provider-billed estimated_cost_usd in the same payload beats
        rate-card synthesis — total-as-fresh published deepseek native 29x
        (grok 3.0x) over the harness-billed truth, flattering the kernel on
        the $/task ratio. Same ordering as the legacy branch."""
        cost, basis = cs.compute_cost_ex(deepseek_native_cell(), "deepseek-v4-pro")
        self.assertEqual((cost, basis), (0.0026, "self-reported"))

    def test_inversion_gone_native_vs_kernel(self):
        """The headline inversion: a cheap kernel run must not price ABOVE a
        native run whose spend was 20x larger but hidden in input_tokens."""
        native = deepseek_native_cell()
        native["estimated_cost_usd"] = 0.0  # force the total-as-fresh floor
        native_cost = cs.compute_cost(native, "deepseek-v4-pro")
        kernel_cell = deepseek_native_cell()
        kernel_cell.update({
            "agent": "deepseek-v4-pro-kernel", "arm": "kernel",
            "fresh_input_tokens": 3000, "cache_read_tokens": 8000,
            "cache_create_tokens": 1000, "billed_tokens": 12000,
            "input_tokens": 3000, "output_tokens": 900,
        })
        kernel_cost = cs.compute_cost(kernel_cell, "deepseek-v4-pro")
        self.assertGreater(native_cost, kernel_cost)  # old code inverted this

    def test_self_reported_fallback(self):
        """All buckets AND input_tokens zero, but provider-billed cost exists
        (opencode) → use it, flagged — never a silent $0."""
        cell = deepseek_native_cell()
        cell.update({"input_tokens": 0, "estimated_cost_usd": 0.0206})
        cost, basis = cs.compute_cost_ex(cell, "deepseek-v4-pro")
        self.assertEqual(cost, 0.0206)
        self.assertEqual(basis, "self-reported")

    def test_legacy_folded_prefers_harness_cost(self):
        """Leg 2: pre-→2062 folded input_tokens must not be priced at full
        1.0x rate when the harness recorded a cache-aware cost."""
        cell = {"benchmark": "b", "resolved": True, "turns_to_fix": 3,
                "input_tokens": 500_000, "output_tokens": 2_000,
                "estimated_cost_usd": 0.21}  # cache-aware; full-rate would be ~$1.53
        cost, basis = cs.compute_cost_ex(cell, "claude-sonnet-4-6")
        self.assertEqual(cost, 0.21)
        self.assertEqual(basis, "self-reported-legacy")

    def test_legacy_folded_without_cost_is_flagged_floor(self):
        cell = {"benchmark": "b", "input_tokens": 1000, "output_tokens": 100}
        cost, basis = cs.compute_cost_ex(cell, "claude-sonnet-4-6")
        self.assertAlmostEqual(cost, (1000 * 3.00 + 100 * 15.00) / 1e6, places=12)
        self.assertEqual(basis, "folded-as-fresh")

    def test_bucket_split_unchanged(self):
        cost, basis = cs.compute_cost_ex(sonnet_kernel_cell(), "claude-sonnet-4-6")
        expected = ((2873 * 1.0 + 59316 * 0.10 + 3517 * 1.25) * 3.00 + 1017 * 15.00) / 1e6
        self.assertAlmostEqual(cost, expected, places=9)
        self.assertEqual(basis, "bucket-split")

    def test_no_rate_card_never_silent_zero(self):
        cell = {"benchmark": "b", "input_tokens": 100, "output_tokens": 10,
                "estimated_cost_usd": 0.05}
        cost, basis = cs.compute_cost_ex(cell, "unknown-model-9000")
        self.assertEqual((cost, basis), (0.05, "self-reported"))
        cell["estimated_cost_usd"] = 0
        cost, basis = cs.compute_cost_ex(cell, "unknown-model-9000")
        self.assertEqual(basis, "unpriced")

    def test_total_input_tokens_present_but_zero_aware(self):
        total, split = cs.total_input_tokens(deepseek_native_cell())
        self.assertEqual((total, split), (56327, False))
        total, split = cs.total_input_tokens(sonnet_native_cell())
        self.assertEqual((total, split), (125023, True))


# ---------------------------------------------------------------------------
# Full consolidation (integration over a fixture runs/ tree)
# ---------------------------------------------------------------------------

class TestConsolidateTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.runs = root / "runs"
        self.runs.mkdir()
        self._saved = (cs.RUNS_DIR, cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT)
        cs.RUNS_DIR = self.runs
        cs.SCORES_OUTPUT = root / "public" / "scores.json"
        cs.EXPERIMENT_OUTPUT = root / "public" / "experiment-scores.json"

    def tearDown(self):
        cs.RUNS_DIR, cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT = self._saved
        self._tmp.cleanup()

    # -- fixture helpers --
    def write_cell(self, dirname, bench, payload):
        d = self.runs / dirname
        d.mkdir(exist_ok=True)
        (d / f"{bench}.score.json").write_text(json.dumps(payload))

    def consolidate(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cs.consolidate_all(dry_run=False)
        board = json.loads(cs.EXPERIMENT_OUTPUT.read_text())
        scores = json.loads(cs.SCORES_OUTPUT.read_text())
        return board, scores, out.getvalue()

    @staticmethod
    def model_row(board, norm):
        for row in board["models"]:
            if row["model_normalized"] == norm:
                return row
        return None

    def build_default_tree(self):
        # claude-sonnet-4-6: native_driver, both arms + a duplicate -kernel dir
        self.write_cell("claude-sonnet-4-6-native", "bench-a", sonnet_native_cell())
        self.write_cell("claude-sonnet-4-6-kernel-cpu", "bench-a", sonnet_kernel_cell())
        dup = sonnet_kernel_cell()
        dup["agent"] = "claude-sonnet-4-6-kernel"
        self.write_cell("claude-sonnet-4-6-kernel", "bench-a", dup)
        # deadline cell — must be VISIBLY invalid, not silently dropped
        dl = sonnet_kernel_cell("bench-deadline")
        dl.update({"resolved": False, "stop_reason": "deadline_exceeded",
                   "turns_to_fix": 0, "tool_uses": 0, "input_tokens": 0,
                   "output_tokens": 0, "fresh_input_tokens": 0,
                   "cache_read_tokens": 0, "cache_create_tokens": 0,
                   "billed_tokens": 0})
        self.write_cell("claude-sonnet-4-6-kernel-cpu", "bench-deadline", dl)
        # teardown sidecar marks bench-a in the kernel-cpu dir
        (self.runs / "claude-sonnet-4-6-kernel-cpu" / "_teardown_masked.json").write_text(
            json.dumps({"benchmarks": {"bench-a": {"log": "x.log"}}}))
        # gpt-5.5: generic_kernel whose only ostk data is the -kernel-cpu dir;
        # payload-level teardown_masked passthrough
        gpt = sonnet_kernel_cell()
        gpt.update({"agent": "gpt-5.5-kernel-cpu", "teardown_masked": True})
        self.write_cell("gpt-5.5-kernel-cpu", "bench-a", gpt)
        # grok-4.3: native cell is INVALID (zero-billed harness-incomplete:
        # billed_tokens=0 + empty buckets on a completed run); kernel arm
        # ABSENT (legitimately not run)
        grok = deepseek_native_cell()
        grok["agent"] = "grok-4.3-native"
        self.write_cell("grok-4.3-native", "bench-a", grok)
        # devstral-2512: native fail with 0 turns/tool_uses (NOT "completed",
        # so the zero-billed rule does not fire) and no provider-billed cost
        # — stays VALID and is priced via the explicit total-as-fresh floor.
        dev = deepseek_native_cell()
        dev.update({"agent": "devstral-2512-native", "resolved": False,
                    "turns_to_fix": 0, "tool_uses": 0, "stop_reason": "fail",
                    "estimated_cost_usd": 0.0})
        self.write_cell("devstral-2512-native", "bench-a", dev)
        # kimi-k2.6: native cell is INVALID (zero output on completed run)
        self.write_cell("kimi-k2.6-native", "bench-a", kimi_native_cell())

    def test_full_board(self):
        self.build_default_tree()
        board, scores, _ = self.consolidate()

        # ---- trust block present & per-axis counts correct ----
        trust = board["trust"]
        # SOLVE-valid: sonnet native + sonnet cpu + gpt cpu + devstral native
        #              + grok native (cost-invalid) + kimi native (cost-invalid) = 6
        self.assertEqual(trust["solve_valid_cells"], 6)
        # ...of which both-axes valid = 4 (grok/kimi native cost axis unavailable)
        self.assertEqual(trust["cost_valid_cells"], 4)
        self.assertEqual(trust["cost_invalid_cells"], 2)
        # fully INVALID (solve axis): sonnet dup kernel + sonnet deadline = 2.
        # AXIS SPLIT: kimi zero-output + grok zero-billed are NO LONGER here —
        # their solve verdicts are redeemed; only their cost axis is dead.
        self.assertEqual(trust["invalid_cells"], 2)
        self.assertEqual(trust["invalid_by_reason"].get(cv.REASON_NATIVE_DRIVER_DUP), 1)
        self.assertEqual(trust["invalid_by_reason"].get(cv.REASON_DEADLINE), 1)
        self.assertNotIn("zero_token_accounting", trust["invalid_by_reason"])
        # cost-axis histogram keeps the full subtype (visible per class)
        self.assertEqual(trust["cost_invalid_by_reason"].get(cv.REASON_ZERO_BILLED), 1)
        self.assertEqual(trust["cost_invalid_by_reason"].get(cv.REASON_ZERO_TOKEN_OUTPUT), 1)

        # ---- scores.json holds every SOLVE-valid cell; cost axis flagged ----
        self.assertEqual(len(scores), 6)
        for s in scores:
            self.assertIn("cost_basis", s)
            self.assertIn("teardown_masked", s)
            self.assertIn("cost_valid", s)
        by_agent = {s["agent"]: s for s in scores}
        # cost-invalid cells are published with an UNAVAILABLE cost axis —
        # never a rate-card synthesis, never a silent $0
        for agent in ("grok-4.3-native", "kimi-k2.6-native"):
            self.assertFalse(by_agent[agent]["cost_valid"])
            self.assertIsNone(by_agent[agent]["computed_cost_usd"])
            self.assertEqual(by_agent[agent]["cost_basis"], "unavailable")

        # ---- DEDUP: one execution never in two columns ----
        sonnet = self.model_row(board, "claude-sonnet-4-6")
        self.assertEqual(sonnet["cpu"]["total"], 1)
        self.assertEqual(sonnet["kernel"]["total"], 0)      # dup cell excluded
        self.assertEqual(sonnet["kernel"]["invalid"], 1)    # ...and counted
        self.assertEqual(sonnet["ostk"]["source_arm"], "cpu")
        self.assertEqual(sonnet["ostk"]["arm_type"], "native_driver")
        self.assertEqual(sonnet["ostk"]["total"], 1)
        # the misleading 0/0 twin is annotated, not bare
        self.assertIn("not_applicable", sonnet["kernel"].get("status", ""))

        # ---- deadline cell is visible ----
        dl = [c for c in board["invalid_cells"] if c["benchmark"] == "bench-deadline"]
        self.assertEqual(len(dl), 1)
        self.assertIn(cv.REASON_DEADLINE, dl[0]["reasons"])

        # ---- TEARDOWN: sidecar + payload passthrough, counted per model/arm ----
        self.assertEqual(sonnet["cpu"]["masked"], 1)        # via sidecar
        gpt = self.model_row(board, "gpt-5-5")
        self.assertEqual(gpt["cpu"]["masked"], 1)           # via payload field
        self.assertEqual(board["trust"]["teardown_masked_cells"], 2)
        bench_a = [b for b in board["benchmarks"]
                   if b["model_normalized"] == "claude-sonnet-4-6" and b["benchmark"] == "bench-a"][0]
        self.assertTrue(bench_a["cpu"]["teardown_masked"])
        self.assertFalse(bench_a["native"]["teardown_masked"])

        # ---- gpt-5.5: generic_kernel sourced from cpu dir, flagged ----
        self.assertEqual(gpt["kernel_arm_type"], "generic_kernel")
        self.assertEqual(gpt["ostk"]["source_arm"], "cpu")
        self.assertIn("note", gpt["ostk"])

        # ---- absent vs INVALID vs COST_INVALID (the axis split) ----
        grok = self.model_row(board, "grok-4-3")
        self.assertEqual(grok["kernel"]["total"], 0)
        self.assertEqual(grok["kernel"]["invalid"], 0)      # absent = never run
        # grok native: zero-billed harness-incomplete → SOLVE verdict REDEEMED
        # (enters solve columns), cost axis unavailable (excluded from cost
        # columns, counted visibly, never priced).
        self.assertEqual(grok["native"]["total"], 1)
        self.assertEqual(grok["native"]["solved"], 1)
        self.assertEqual(grok["native"]["invalid"], 0)
        self.assertEqual(grok["native"]["cost_invalid"], 1)
        self.assertEqual(grok["native"]["cost_cells"], 0)
        self.assertEqual(grok["native"]["cost"], 0)
        self.assertEqual(grok["native"]["tokens"], 0)       # cost axis excluded
        self.assertEqual(grok["native"]["cost_bases"], {"unavailable": 1})
        grok_ci = [c for c in board["cost_invalid_cells"] if c["model"] == "grok-4.3"]
        self.assertEqual(len(grok_ci), 1)
        self.assertIn(cv.REASON_ZERO_BILLED, grok_ci[0]["reasons"])
        self.assertEqual(grok_ci[0]["axis"], "cost")
        # ...and NOT in the fully-invalid list
        self.assertFalse([c for c in board["invalid_cells"] if c["model"] == "grok-4.3"])
        kimi = self.model_row(board, "kimi-k2-6")
        self.assertIsNotNone(kimi)
        self.assertEqual(kimi["native"]["total"], 1)        # solve verdict kept
        self.assertEqual(kimi["native"]["solved"], 1)
        self.assertEqual(kimi["native"]["cost_invalid"], 1)
        self.assertEqual(kimi["native"]["cost_cells"], 0)
        # per-bench arm summary carries the axis flag + unavailable cost
        kimi_bench = [b for b in board["benchmarks"]
                      if b["model_normalized"] == "kimi-k2-6"][0]
        self.assertTrue(kimi_bench["native"]["resolved"])
        self.assertFalse(kimi_bench["native"]["cost_valid"])
        self.assertIsNone(kimi_bench["native"]["cost_usd"])
        self.assertEqual(kimi_bench["native"]["cost_basis"], "unavailable")

        # ---- token/cost inversion fixed at board level ----
        # native billed 125,974 total vs kernel 66,723: the fresh-only column
        # used to read 958 vs 3,890 (inverted).
        self.assertEqual(sonnet["native"]["tokens"], 125023 + 951)
        self.assertEqual(sonnet["cpu"]["tokens"], 65706 + 1017)
        self.assertGreater(sonnet["native"]["tokens"], sonnet["cpu"]["tokens"])
        self.assertGreater(sonnet["native"]["cost"], sonnet["cpu"]["cost"])
        # cost_cells makes the $/task denominator honest (== cost-valid cells)
        self.assertEqual(sonnet["native"]["cost_cells"], 1)
        # devstral native (present-but-zero, no est, NOT completed so the
        # zero-billed rule does not fire) priced from input_tokens via the
        # explicit total-as-fresh floor, not ~$0
        dev = self.model_row(board, "devstral-2512")
        self.assertGreater(dev["native"]["cost"], 0.02)
        self.assertEqual(dev["native"]["cost_bases"], {"total-as-fresh": 1})

    def test_arm_mismatch_cell_is_invalid(self):
        # A receipt failure is a SOLVE-axis fault: fully INVALID on both axes
        # (never redeemed by good token accounting).
        bad = sonnet_native_cell()
        self.write_cell("claude-sonnet-4-6-kernel-cpu", "bench-a", bad)  # native receipt in cpu dir
        board, scores, _ = self.consolidate()
        self.assertEqual(board["trust"]["solve_valid_cells"], 0)
        self.assertEqual(board["trust"]["cost_invalid_cells"], 0)
        self.assertEqual(board["trust"]["invalid_cells"], 1)
        self.assertEqual(board["trust"]["invalid_by_reason"].get(cv.REASON_ARM_MISMATCH), 1)
        self.assertEqual(scores, [])

    def test_malformed_payload_is_invalid_not_crash(self):
        d = self.runs / "claude-sonnet-4-6-native"
        d.mkdir()
        (d / "bench-a.score.json").write_text("{ not json !!")
        with contextlib.redirect_stdout(io.StringIO()):
            cs.consolidate_all(dry_run=False)
        board = json.loads(cs.EXPERIMENT_OUTPUT.read_text())
        self.assertEqual(board["trust"]["invalid_by_reason"].get(cv.REASON_MALFORMED), 1)

    def test_published_exclude_is_policy_not_invalid(self):
        self.write_cell("claude-sonnet-4-6-native", "haystack-boot", sonnet_native_cell("haystack-boot"))
        board, _, _ = self.consolidate()
        self.assertEqual(board["trust"]["policy_excluded_cells"], 1)
        self.assertEqual(board["trust"]["invalid_cells"], 0)
        self.assertEqual(board["trust"]["cost_invalid_cells"], 0)

    def test_generic_kernel_dup_when_both_dirs(self):
        # generic_kernel model with BOTH -kernel and -kernel-cpu → kernel-cpu
        # cells are the duplicates; kernel column is canonical.
        k = sonnet_kernel_cell()
        k["agent"] = "deepseek-v4-pro-kernel"
        self.write_cell("deepseek-v4-pro-kernel", "bench-a", k)
        kc = sonnet_kernel_cell()
        kc["agent"] = "deepseek-v4-pro-kernel-cpu"
        self.write_cell("deepseek-v4-pro-kernel-cpu", "bench-a", kc)
        board, _, _ = self.consolidate()
        row = self.model_row(board, "deepseek-v4-pro")
        self.assertEqual(row["kernel"]["total"], 1)
        self.assertEqual(row["cpu"]["total"], 0)
        self.assertEqual(row["cpu"]["invalid"], 1)
        self.assertEqual(row["ostk"]["source_arm"], "kernel")
        self.assertEqual(board["trust"]["invalid_by_reason"].get(cv.REASON_GENERIC_KERNEL_DUP), 1)

    def test_scores_json_is_best_per_cell_not_per_file(self):
        # Two score FILES for one (model, bench, arm) cell — e.g. a canonical
        # payload plus a *.recovered-june16.score.json twin — must collapse to
        # ONE scores.json entry (the keep-best winner), matching the module
        # docstring contract, so consumers summing the flat file never
        # double-count a cell.
        worse = sonnet_kernel_cell()
        worse.update({"resolved": False, "turns_to_fix": 20})
        d = self.runs / "claude-sonnet-4-6-kernel-cpu"
        d.mkdir(exist_ok=True)
        (d / "bench-a.recovered-june16.score.json").write_text(json.dumps(worse))
        self.write_cell("claude-sonnet-4-6-kernel-cpu", "bench-a", sonnet_kernel_cell())
        board, scores, _ = self.consolidate()
        self.assertEqual(board["trust"]["solve_valid_cells"], 2)  # two valid FILES...
        self.assertEqual(len(scores), 1)                     # ...ONE published cell
        self.assertTrue(scores[0]["resolved"])               # the keep-best winner


class TestSnapshots(unittest.TestCase):
    """Run-date snapshot selection: the LATEST board is single-snapshot per
    (model, arm) column — a re-run eclipses the earlier run outright (even a
    worse one; keep-best never mixes run dates), and the earlier run remains
    available as its own versioned snapshot board."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.runs = root / "runs"
        self.runs.mkdir()
        self._saved = (cs.RUNS_DIR, cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT)
        cs.RUNS_DIR = self.runs
        cs.SCORES_OUTPUT = root / "public" / "scores.json"
        cs.EXPERIMENT_OUTPUT = root / "public" / "experiment-scores.json"
        d = self.runs / "claude-sonnet-4-6-kernel-cpu"
        d.mkdir()
        june = sonnet_kernel_cell()          # resolved=True, 2026-06-16
        july = sonnet_kernel_cell()
        july.update({"resolved": False, "turns_to_fix": 12,
                     "timestamp": "2026-07-02T13:00:00Z"})
        (d / "bench-a.recovered-june16.score.json").write_text(json.dumps(june))
        (d / "bench-a.score.json").write_text(json.dumps(july))
        # native arm ran ONLY in June — its column stays june16 in the latest
        # board (single-snapshot per column, not per board).
        (self.runs / "claude-sonnet-4-6-native").mkdir()
        (self.runs / "claude-sonnet-4-6-native" / "bench-a.score.json").write_text(
            json.dumps(sonnet_native_cell()))

    def tearDown(self):
        cs.RUNS_DIR, cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT = self._saved
        self._tmp.cleanup()

    def collect(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cs.collect_cells()

    def test_latest_is_single_snapshot_per_column(self):
        cells, pe, _ = self.collect()
        board, scores = cs.build_experiment_output(cells, pe, snapshot="latest", quiet=True)
        row = [m for m in board["models"] if m["model_normalized"] == "claude-sonnet-4-6"][0]
        # July eclipses June even though June's cell was resolved and July's
        # is not — a re-run replaces, keep-best never mixes run dates.
        self.assertEqual(row["cpu"]["total"], 1)
        self.assertEqual(row["cpu"]["solved"], 0)
        self.assertEqual(board["column_snapshots"]["claude-sonnet-4-6/cpu"], "july02")
        self.assertEqual(board["column_snapshots"]["claude-sonnet-4-6/native"], "june16")
        cpu_scores = [s for s in scores if s.get("agent", "").endswith("kernel-cpu")]
        self.assertEqual(len(cpu_scores), 1)
        self.assertEqual(cpu_scores[0]["snapshot"], "july02")

    def test_snapshot_boards_are_pure(self):
        cells, pe, _ = self.collect()
        june_board, june_scores = cs.build_experiment_output(
            cells, pe, snapshot="june16", quiet=True)
        july_board, july_scores = cs.build_experiment_output(
            cells, pe, snapshot="july02", quiet=True)
        jrow = [m for m in june_board["models"] if m["model_normalized"] == "claude-sonnet-4-6"][0]
        self.assertEqual(jrow["cpu"]["solved"], 1)          # June cell preserved
        self.assertEqual(jrow["native"]["total"], 1)        # native ran in June
        yrow = [m for m in july_board["models"] if m["model_normalized"] == "claude-sonnet-4-6"][0]
        self.assertEqual(yrow["cpu"]["total"], 1)
        self.assertEqual(yrow["cpu"]["solved"], 0)
        self.assertEqual(yrow["native"]["total"], 0)        # native absent in July
        self.assertTrue(all(s["snapshot"] == "june16" for s in june_scores))
        self.assertTrue(all(s["snapshot"] == "july02" for s in july_scores))

    def test_snapshot_faults_annotated(self):
        cells, pe, _ = self.collect()
        board, _ = cs.build_experiment_output(cells, pe, snapshot="july02", quiet=True)
        self.assertIn("july02-cache-read-zero", [f["id"] for f in board["faults"]])
        latest, _ = cs.build_experiment_output(cells, pe, snapshot="latest", quiet=True)
        ids = [f["id"] for f in latest["faults"]]
        self.assertIn("june16-teardown-masked", ids)   # native column is june16
        self.assertIn("july02-cache-read-zero", ids)   # cpu column is july02

    def test_column_supersessions_disclosed(self):
        # The July re-run displaced the June cpu cell: the eviction must be
        # DECLARED in the latest board (a partial re-run can never silently
        # shrink a column — the gemini-3.5-flash 1-cell smoke evicted 36).
        cells, pe, _ = self.collect()
        latest, _ = cs.build_experiment_output(cells, pe, snapshot="latest", quiet=True)
        sup = latest["column_supersessions"]
        self.assertEqual(sup["claude-sonnet-4-6/cpu"], {
            "published_snapshot": "july02",
            "published_cells": 1,
            "displaced_cells": {"june16": 1},
        })
        self.assertNotIn("claude-sonnet-4-6/native", sup)  # single-snapshot column
        # same-version columns → no mixed-version trust split needed
        self.assertNotIn("by_ostk_version", latest["trust"])
        self.assertEqual(latest["ostk_versions"],
                         {"june16": "v7.6.0", "july02": "v7.6.0"})

    def test_versioned_boards_carry_no_latest_riders(self):
        # Versioned snapshot boards are append-only: the latest-only honesty
        # riders must never perturb their payloads.
        cells, pe, _ = self.collect()
        for snap in ("june16", "july02"):
            board, _ = cs.build_experiment_output(cells, pe, snapshot=snap, quiet=True)
            for key in ("column_supersessions", "ostk_versions", "ostk_version_note"):
                self.assertNotIn(key, board, f"{key} leaked into {snap} board")
            self.assertNotIn("by_ostk_version", board["trust"])
            self.assertNotIn("mixed_versions_note", board["trust"])


class TestMixedVersionBoard(unittest.TestCase):
    """Cross-VERSION machinery: when the latest board's columns span binary
    versions (june16 = v7.6.0 vs july09 = v7.7.1), the trust totals must carry
    a per-version split, and the board-v760 adapter must SUPPRESS the pair
    delta outright (boards/index.json doctrine: no cross-version delta is
    published anywhere). Same-version cross-snapshot deltas stay published
    flagged (gemini-3.1 native june16 vs cpu july02, both v7.6.0)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.runs = root / "runs"
        self.runs.mkdir()
        self._saved = (cs.RUNS_DIR, cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT)
        cs.RUNS_DIR = self.runs
        cs.SCORES_OUTPUT = root / "public" / "scores.json"
        cs.EXPERIMENT_OUTPUT = root / "public" / "experiment-scores.json"
        # native ran in June (v7.6.0); cpu re-ran on the v7.7.1 pin (july09).
        (self.runs / "claude-sonnet-4-6-native").mkdir()
        (self.runs / "claude-sonnet-4-6-native" / "bench-a.score.json").write_text(
            json.dumps(sonnet_native_cell()))
        d = self.runs / "claude-sonnet-4-6-kernel-cpu"
        d.mkdir()
        july9 = sonnet_kernel_cell()
        july9["timestamp"] = "2026-07-09T04:00:00Z"
        (d / "bench-a.score.json").write_text(json.dumps(july9))

    def tearDown(self):
        cs.RUNS_DIR, cs.SCORES_OUTPUT, cs.EXPERIMENT_OUTPUT = self._saved
        self._tmp.cleanup()

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cells, pe, _ = cs.collect_cells()
        latest, _ = cs.build_experiment_output(cells, pe, snapshot="latest", quiet=True)
        by_col = pb.best_by_column(pb.latest_cells(cells))
        return latest, pb.emit_board(latest, by_col, published=1)

    def test_mixed_version_trust_split_sums_to_totals(self):
        latest, _ = self.build()
        self.assertEqual(latest["ostk_versions"],
                         {"june16": "v7.6.0", "july09": "v7.7.1"})
        by_ver = latest["trust"]["by_ostk_version"]
        self.assertEqual(set(by_ver), {"v7.6.0", "v7.7.1"})
        for k in ("solve_valid_cells", "cost_valid_cells",
                  "cost_invalid_cells", "invalid_cells"):
            self.assertEqual(sum(v[k] for v in by_ver.values()),
                             latest["trust"][k], k)
        self.assertIn("mixed_versions_note", latest["trust"])

    def test_cross_version_pair_delta_suppressed(self):
        _, board = self.build()
        row = [m for m in board["models"] if m["model"] == "claude-sonnet-4-6"][0]
        self.assertTrue(row["cross_snapshot_delta"])
        self.assertTrue(row["cross_version_pair"])
        self.assertIsNone(row["cost_delta"])
        self.assertIsNone(row["tok_delta"])
        self.assertTrue(row["delta_note"].startswith("SUPPRESSED"))
        self.assertEqual(row["both_cost_n"], 1)   # the pair itself stays visible

    def test_same_version_cross_snapshot_delta_still_published(self):
        # Move the cpu re-run back to july02 (same v7.6.0 binary as june16):
        # the delta must publish, flagged — behavior unchanged from before.
        d = self.runs / "claude-sonnet-4-6-kernel-cpu"
        july2 = sonnet_kernel_cell()
        july2["timestamp"] = "2026-07-02T13:00:00Z"
        (d / "bench-a.score.json").write_text(json.dumps(july2))
        _, board = self.build()
        row = [m for m in board["models"] if m["model"] == "claude-sonnet-4-6"][0]
        self.assertTrue(row["cross_snapshot_delta"])
        self.assertFalse(row["cross_version_pair"])
        self.assertIsNotNone(row["cost_delta"])
        self.assertIsNotNone(row["tok_delta"])
        self.assertIn("different run-date snapshots", row["delta_note"])


if __name__ == "__main__":
    unittest.main()
