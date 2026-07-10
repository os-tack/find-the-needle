#!/usr/bin/env python3
"""Unit tests for →GPT-review F5 — arm-receipt RECONCILIATION.

cell_validity's pre-existing REASON_NO_ARM_RECEIPT / REASON_ARM_MISMATCH
reconcile a payload's OWN receipts against each other (agent suffix vs arm
field) for requested-vs-executed self-consistency. This file covers the
three checks that reconcile the receipt against the cell's PUBLIC claims
instead:

  (a) COLUMN <-> RECEIPT   — REASON_COLUMN_ARM_MISMATCH
      the payload's literal `executed_arm` field vs the runs/ directory it
      publishes under.
  (b) PROVIDER <-> EXPECTATION — REASON_PROVIDER_MISMATCH
      `executed_provider` vs the model's derived native-provider expectation
      (kernel/kernel-cpu only; B* generic-OpenRouter masquerading as Tier-1).
  (c) MODEL IDENTITY <-> OBSERVED — REASON_MODEL_MISMATCH
      `observed_models` (vendor-CLI telemetry) vs the cell's claimed model
      (the 2026-07-09 fable-5 -> opus-4-8 mid-session fallback incident).

Every check fails OPEN on an absent field/expectation — none of the 875
current runs/ score files carry executed_provider or observed_models yet, so
these checks must be complete no-ops on today's data (see the zero-drift
acceptance gate run via consolidate_scores.py / scripts/check_boards.py).

Also covers the scripts/native_usage.py extension that populates
observed_models from the Claude CLI result envelope's modelUsage map,
independently of cost-accounting state.

Hermetic: no docker, no network, no dependency on runs/ contents.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cell_validity as cv
import native_usage


def base_cell(**over):
    """A healthy, fully-accounted claude-opus-4-8 kernel-cpu cell carrying
    BOTH receipt layers: the agent-suffix/arm-field pair the pre-existing
    arm_mismatch check reconciles, and the literal requested_arm/
    executed_arm fields the rust host's bench_binary receipt writes
    (verbatim shape of runs/claude-opus-4-8-kernel-cpu/
    missing-input-validation.score.json)."""
    base = {
        "benchmark": "off-by-one-pagination",
        "agent": "claude-opus-4-8-kernel-cpu",
        "arm": "kernel",
        "requested_arm": "kernel",
        "executed_arm": "kernel-cpu",
        "resolved": True,
        "turns_to_fix": 9,
        "input_tokens": 4760,
        "output_tokens": 1778,
        "fresh_input_tokens": 4760,
        "cache_read_tokens": 82414,
        "cache_create_tokens": 5963,
        "billed_tokens": 93137,
        "tool_uses": 8,
        "stop_reason": "end_turn",
    }
    base.update(over)
    return base


# =============================================================================
# (a) COLUMN <-> RECEIPT
# =============================================================================
class TestColumnArmMismatch(unittest.TestCase):
    def test_generic_fallback_execution_is_invalid(self):
        # dir is kernel-cpu, agent suffix says kernel-cpu (passes the
        # pre-existing self-consistency check) — but the literal executed_arm
        # receipt says a generic 'kernel' fallback actually ran.
        cell = base_cell(executed_arm="kernel")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertFalse(cls.cost_valid)
        self.assertIn("column_arm_mismatch:dir=kernel-cpu:executed=kernel",
                      cls.reasons)
        self.assertIn("column_arm_mismatch:dir=kernel-cpu:executed=kernel",
                      cls.solve_reasons)
        # the agent-suffix-derived check is untouched (still consistent) —
        # only the NEW literal-field check fires.
        self.assertFalse(any(r == cv.REASON_ARM_MISMATCH
                             or r.startswith(cv.REASON_ARM_MISMATCH + ":")
                             for r in cls.reasons))

    def test_matching_literal_executed_arm_is_valid(self):
        cell = base_cell()  # executed_arm="kernel-cpu" matches the dir
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_cpu_alias_normalizes_both_sides(self):
        # dir arm passed as the 'cpu' alias; literal field says 'kernel-cpu'.
        cell = base_cell(executed_arm="kernel-cpu")
        cls = cv.classify_cell(cell, requested_arm="cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_literal_cpu_alias_also_normalizes(self):
        cell = base_cell(executed_arm="cpu")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_absent_literal_field_is_noop(self):
        # Older writers never set executed_arm at all.
        cell = base_cell()
        del cell["executed_arm"]
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)
        self.assertEqual(cls.reasons, [])

    def test_no_requested_arm_skips_check_entirely(self):
        cell = base_cell(executed_arm="kernel")
        cls = cv.classify_cell(cell, requested_arm=None, model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_native_dir_column_mismatch(self):
        cell = base_cell(agent="claude-opus-4-8-native", arm="native",
                         requested_arm="native", executed_arm="kernel-cpu")
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn("column_arm_mismatch:dir=native:executed=kernel-cpu",
                      cls.reasons)


# =============================================================================
# (b) PROVIDER <-> EXPECTATION
# =============================================================================
class TestProviderMismatch(unittest.TestCase):
    def test_openrouter_masquerade_is_invalid(self):
        # A Tier-1 (native-provider) model whose kernel-cpu cell actually
        # executed via OpenRouter — a B* execution masquerading as B.
        cell = base_cell(executed_provider="openrouter")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn("provider_mismatch:expected=anthropic:executed=openrouter",
                      cls.reasons)
        self.assertIn("provider_mismatch:expected=anthropic:executed=openrouter",
                      cls.solve_reasons)

    def test_matching_provider_is_valid(self):
        cell = base_cell(executed_provider="anthropic")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_provider_comparison_is_case_insensitive(self):
        cell = base_cell(executed_provider="Anthropic")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_native_arm_skips_provider_check_even_on_mismatch(self):
        # The native arm's CLI identity ('claude') is not a provider name —
        # never checked, no matter what executed_provider says.
        cell = base_cell(agent="claude-opus-4-8-native", arm="native",
                         requested_arm="native", executed_arm="native",
                         executed_provider="openrouter")
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_absent_executed_provider_is_noop(self):
        cell = base_cell()
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_model_with_no_native_expectation_is_noop(self):
        # deepseek/grok/kimi/qwen: no single native provider to reconcile
        # against — an unmapped model never poisons a cell by itself.
        cell = base_cell(agent="deepseek-v4-pro-kernel-cpu", executed_arm="kernel-cpu",
                         executed_provider="openrouter")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="deepseek-v4-pro")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_gpt_kernel_via_openrouter_is_invalid(self):
        cell = base_cell(agent="gpt-5.5-kernel-cpu", executed_arm="kernel-cpu",
                         executed_provider="openrouter")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="gpt-5.5")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn("provider_mismatch:expected=openai:executed=openrouter", cls.reasons)

    def test_model_threaded_via_agent_fallback(self):
        # No model= param passed — falls back to the agent-suffix parse.
        cell = base_cell(executed_provider="openrouter")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu")  # no model=
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn("provider_mismatch:expected=anthropic:executed=openrouter",
                      cls.reasons)


class TestExpectedProviderHeuristic(unittest.TestCase):
    def test_claude_expects_anthropic(self):
        self.assertEqual(cv.expected_provider("claude-opus-4-8"), "anthropic")
        self.assertEqual(cv.expected_provider("claude-fable-5"), "anthropic")

    def test_gpt_and_o_series_expect_openai(self):
        self.assertEqual(cv.expected_provider("gpt-4.1"), "openai")
        self.assertEqual(cv.expected_provider("gpt-5.5-codex"), "openai")
        self.assertEqual(cv.expected_provider("o4-mini"), "openai")
        self.assertEqual(cv.expected_provider("o3"), "openai")

    def test_gemini_expects_gemini(self):
        self.assertEqual(cv.expected_provider("gemini-3.1-pro-preview"), "gemini")

    def test_mistral_family_expects_mistral(self):
        self.assertEqual(cv.expected_provider("devstral-2512"), "mistral")
        self.assertEqual(cv.expected_provider("codestral-2508"), "mistral")
        self.assertEqual(cv.expected_provider("mistral-medium"), "mistral")

    def test_unmapped_model_has_no_expectation(self):
        self.assertIsNone(cv.expected_provider("deepseek-v4-pro"))
        self.assertIsNone(cv.expected_provider("grok-4.3"))
        self.assertIsNone(cv.expected_provider("kimi-k2.6"))
        self.assertIsNone(cv.expected_provider(None))
        self.assertIsNone(cv.expected_provider(""))


# =============================================================================
# (c) MODEL IDENTITY <-> OBSERVED
# =============================================================================
class TestModelIdentityMismatch(unittest.TestCase):
    def _native_fable_cell(self, **over):
        cell = base_cell(agent="claude-fable-5-native", arm="native",
                         requested_arm="native", executed_arm="native")
        cell.update(over)
        return cell

    def test_opus_fallback_observed_is_invalid(self):
        # The 2026-07-09 incident shape: modelUsage showed both fable AND
        # opus token buckets in one session.
        cell = self._native_fable_cell(
            observed_models=["claude-fable-5", "claude-opus-4-8"])
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertIn("model_mismatch:observed=claude-fable-5,claude-opus-4-8",
                      cls.reasons)
        self.assertIn("model_mismatch:observed=claude-fable-5,claude-opus-4-8",
                      cls.solve_reasons)

    def test_pure_single_model_session_is_valid(self):
        cell = self._native_fable_cell(observed_models=["claude-fable-5"])
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_date_suffixed_pure_session_is_valid(self):
        cell = self._native_fable_cell(
            observed_models=["claude-fable-5-20260709"])
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_date_suffixed_fallback_still_detected(self):
        cell = self._native_fable_cell(
            observed_models=["claude-fable-5-20260709", "claude-opus-4-8-20260709"])
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertIn(
            "model_mismatch:observed=claude-fable-5-20260709,claude-opus-4-8-20260709",
            cls.reasons)

    def test_absent_observed_models_is_noop(self):
        cell = self._native_fable_cell()
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)
        self.assertEqual(cls.reasons, [])

    def test_empty_observed_models_list_is_noop(self):
        cell = self._native_fable_cell(observed_models=[])
        cls = cv.classify_cell(cell, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_model_falls_back_to_agent_suffix_when_not_threaded(self):
        cell = self._native_fable_cell(
            observed_models=["claude-fable-5", "claude-opus-4-8"])
        cls = cv.classify_cell(cell, requested_arm="native")  # no model=
        self.assertEqual(cls.status, cv.INVALID)

    def test_check_is_independent_of_arm_kind(self):
        # kernel-cpu cell, same fault shape — still fires (not native-only).
        cell = base_cell(observed_models=["claude-opus-4-8", "claude-fable-5"])
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertTrue(any(r.startswith(cv.REASON_MODEL_MISMATCH) for r in cls.reasons))


class TestModelIdNormalization(unittest.TestCase):
    def test_dotted_and_dashed_forms_agree(self):
        self.assertEqual(cv.normalize_model_id("gemini-3.1-pro-preview"),
                         cv.normalize_model_id("gemini-3-1-pro-preview"))
        self.assertEqual(cv.normalize_model_id("gemini-3.1-pro-preview"),
                         "gemini-3-1-pro-preview")

    def test_underscore_form_normalizes_too(self):
        self.assertEqual(cv.normalize_model_id("gpt_5_5"), "gpt-5-5")

    def test_strips_trailing_date_suffix(self):
        self.assertEqual(cv.normalize_model_id("claude-fable-5-20260709"),
                         "claude-fable-5")

    def test_does_not_strip_a_non_date_numeric_suffix(self):
        # '-4-8' is not an 8-digit date — must survive untouched.
        self.assertEqual(cv.normalize_model_id("claude-opus-4-8"), "claude-opus-4-8")

    def test_none_in_none_out(self):
        self.assertIsNone(cv.normalize_model_id(None))

    def test_case_insensitive(self):
        self.assertEqual(cv.normalize_model_id("Claude-Fable-5"), "claude-fable-5")


class TestClaimedModelHelper(unittest.TestCase):
    def test_prefers_threaded_param(self):
        self.assertEqual(
            cv._claimed_model({"agent": "gpt-5.5-kernel-cpu"}, "claude-opus-4-8"),
            "claude-opus-4-8")

    def test_falls_back_to_agent_suffix(self):
        self.assertEqual(
            cv._claimed_model({"agent": "gpt-5.5-kernel-cpu"}, None), "gpt-5.5")

    def test_none_when_unparseable_and_no_param(self):
        self.assertIsNone(cv._claimed_model({"agent": "gpt-5.5"}, None))
        self.assertIsNone(cv._claimed_model({}, None))


# =============================================================================
# Combined / axis-machinery checks
# =============================================================================
class TestCombinedReasonsAndAxis(unittest.TestCase):
    def test_column_and_provider_mismatch_both_collected(self):
        cell = base_cell(executed_arm="kernel", executed_provider="openrouter")
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertTrue(any(r.startswith(cv.REASON_COLUMN_ARM_MISMATCH) for r in cls.reasons))
        self.assertTrue(any(r.startswith(cv.REASON_PROVIDER_MISMATCH) for r in cls.reasons))

    def test_new_reasons_are_solve_axis(self):
        self.assertEqual(
            cv.reason_axis(f"{cv.REASON_COLUMN_ARM_MISMATCH}:dir=kernel-cpu:executed=kernel"),
            "solve")
        self.assertEqual(
            cv.reason_axis(f"{cv.REASON_PROVIDER_MISMATCH}:expected=anthropic:executed=openrouter"),
            "solve")
        self.assertEqual(
            cv.reason_axis(f"{cv.REASON_MODEL_MISMATCH}:observed=a,b"),
            "solve")

    def test_untouched_legacy_cell_stays_fully_valid(self):
        # No new receipt fields at all (today's universal case) -> no-op on
        # all three checks; this is the shape of every currently-published
        # cell and MUST classify identically to before F5.
        cell = base_cell()
        del cell["executed_arm"]
        cls = cv.classify_cell(cell, requested_arm="kernel-cpu", model="claude-opus-4-8")
        self.assertEqual(cls.status, cv.VALID)
        self.assertEqual(cls.reasons, [])


# =============================================================================
# scripts/native_usage.py — observed_models extraction
# =============================================================================
def _result_json(model_usage: dict, **over) -> str:
    obj = {"type": "result", "subtype": "success", "is_error": False,
          "num_turns": 12, "total_cost_usd": 0.42,
          "usage": {"input_tokens": 100, "output_tokens": 50,
                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
          "modelUsage": model_usage}
    obj.update(over)
    return json.dumps(obj)


# The 2026-07-09 fable-5 incident shape: haiku sidecar + partial fable +
# partial opus, all billable in one session (boards/FAULTS.json
# july09-fable5-native-opus-fallback).
FABLE_OPUS_FALLBACK_RESULT_JSON = _result_json({
    "claude-haiku-4-5": {"inputTokens": 500, "outputTokens": 20,
                         "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                         "costUSD": 0.001},
    "claude-fable-5": {"inputTokens": 1815, "outputTokens": 400,
                       "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                       "costUSD": 0.03},
    "claude-opus-4-8": {"inputTokens": 141, "outputTokens": 60,
                        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                        "costUSD": 0.004},
})


class TestObservedModelsFromResultJson(unittest.TestCase):
    def test_extracts_fallback_models_excluding_haiku_sidecar(self):
        observed = native_usage.observed_models_from_result_json(
            FABLE_OPUS_FALLBACK_RESULT_JSON)
        self.assertEqual(observed, ["claude-fable-5", "claude-opus-4-8"])

    def test_zero_usage_entries_excluded(self):
        text = _result_json({
            "claude-fable-5": {"inputTokens": 100, "outputTokens": 10,
                               "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
            "claude-opus-4-8": {"inputTokens": 0, "outputTokens": 0,
                                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
        })
        self.assertEqual(native_usage.observed_models_from_result_json(text),
                         ["claude-fable-5"])

    def test_single_model_session_returns_that_model_only(self):
        text = _result_json({
            "claude-sonnet-5": {"inputTokens": 10, "outputTokens": 5,
                               "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}})
        self.assertEqual(native_usage.observed_models_from_result_json(text),
                         ["claude-sonnet-5"])

    def test_only_haiku_sidecar_returns_none(self):
        text = _result_json({
            "claude-haiku-4-5": {"inputTokens": 50, "outputTokens": 5,
                                 "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}})
        self.assertIsNone(native_usage.observed_models_from_result_json(text))

    def test_no_modelusage_returns_none(self):
        self.assertIsNone(native_usage.observed_models_from_result_json(
            '{"type":"message","content":"hi"}\n'))
        self.assertIsNone(native_usage.observed_models_from_result_json("plain text\n"))

    def test_from_raw_reads_native_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "b.raw"
            raw.mkdir()
            (raw / "native.stdout").write_text(FABLE_OPUS_FALLBACK_RESULT_JSON)
            self.assertEqual(native_usage.observed_models_from_raw(raw),
                             ["claude-fable-5", "claude-opus-4-8"])

    def test_from_raw_absent_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(native_usage.observed_models_from_raw(Path(td) / "b.raw"))


class TestEnrichmentAddsObservedModelsIndependently(unittest.TestCase):
    """The fable-5/opus-4-8 incident cells already carry REAL cost accounting
    (the CLI writer bills correctly from the start) — enrich_score_file must
    still extract observed_models for them, because gating that behind the
    cost-accounted early-return would mean it never fires for exactly the
    cells the receipt exists to catch."""

    def _run(self, stdout_text, payload):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bench = payload["benchmark"]
            raw = root / f"{bench}.raw"
            raw.mkdir()
            (raw / "native.stdout").write_text(stdout_text)
            sp = root / f"{bench}.score.json"
            sp.write_text(json.dumps(payload))
            changed, note = native_usage.enrich_score_file(sp, apply=True)
            return changed, note, json.loads(sp.read_text())

    def _accounted_fable_payload(self, **over):
        p = {
            "benchmark": "auth-bypass-path-traversal",
            "agent": "claude-fable-5-native", "arm": "native",
            "resolved": True, "turns_to_fix": 8, "tool_uses": 6,
            "stop_reason": "end_turn",
            "fresh_input_tokens": 1956, "cache_read_tokens": 500,
            "cache_create_tokens": 0, "billed_tokens": 2456,
            "input_tokens": 1956, "output_tokens": 300,
        }
        p.update(over)
        return p

    def test_already_accounted_cell_still_gets_observed_models(self):
        changed, note, patched = self._run(
            FABLE_OPUS_FALLBACK_RESULT_JSON, self._accounted_fable_payload())
        self.assertTrue(changed)
        self.assertEqual(patched["observed_models"],
                         ["claude-fable-5", "claude-opus-4-8"])
        # cost fields are UNTOUCHED — cost recovery never ran (already accounted).
        self.assertEqual(patched["billed_tokens"], 2456)
        self.assertNotIn("native_usage_source", patched)
        # cell_validity now catches the fallback as solve-INVALID.
        cls = cv.classify_cell(patched, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.INVALID)
        self.assertTrue(any(r.startswith(cv.REASON_MODEL_MISMATCH) for r in cls.solve_reasons))

    def test_pure_single_model_cell_gets_receipt_but_stays_valid(self):
        text = _result_json({
            "claude-fable-5": {"inputTokens": 1956, "outputTokens": 300,
                              "cacheReadInputTokens": 500, "cacheCreationInputTokens": 0}})
        changed, note, patched = self._run(text, self._accounted_fable_payload())
        self.assertTrue(changed)
        self.assertEqual(patched["observed_models"], ["claude-fable-5"])
        cls = cv.classify_cell(patched, requested_arm="native", model="claude-fable-5")
        self.assertEqual(cls.status, cv.VALID, cls.reasons)

    def test_idempotent_does_not_overwrite_existing_receipt(self):
        payload = self._accounted_fable_payload(observed_models=["claude-fable-5"])
        changed, note, patched = self._run(FABLE_OPUS_FALLBACK_RESULT_JSON, payload)
        self.assertFalse(changed)
        self.assertEqual(patched["observed_models"], ["claude-fable-5"])

    def test_dry_run_does_not_persist_observed_models(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = self._accounted_fable_payload()
            raw = root / f"{payload['benchmark']}.raw"
            raw.mkdir()
            (raw / "native.stdout").write_text(FABLE_OPUS_FALLBACK_RESULT_JSON)
            sp = root / f"{payload['benchmark']}.score.json"
            sp.write_text(json.dumps(payload))
            changed, note = native_usage.enrich_score_file(sp, apply=False)
            self.assertTrue(changed)
            self.assertNotIn("observed_models", json.loads(sp.read_text()))

    def test_no_claude_shaped_raw_is_untouched(self):
        # opencode/kimi/vibe raw shapes carry no modelUsage envelope at all.
        changed, note, patched = self._run(
            "not a claude result envelope", self._accounted_fable_payload())
        self.assertFalse(changed)
        self.assertNotIn("observed_models", patched)
        self.assertEqual(note, "already accounted")

    def test_zero_billed_cell_gets_both_cost_and_model_receipt(self):
        # A cell that's ALSO cost-blind (zero_billed class): both
        # enrichments should land in one call.
        payload = self._accounted_fable_payload(
            fresh_input_tokens=0, cache_read_tokens=0, cache_create_tokens=0,
            billed_tokens=0, input_tokens=0)
        changed, note, patched = self._run(FABLE_OPUS_FALLBACK_RESULT_JSON, payload)
        self.assertTrue(changed)
        self.assertEqual(patched["observed_models"],
                         ["claude-fable-5", "claude-opus-4-8"])
        self.assertEqual(patched["native_usage_source"], "claude_result_json")
        self.assertGreater(patched["billed_tokens"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
