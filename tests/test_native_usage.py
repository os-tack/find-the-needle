#!/usr/bin/env python3
"""Unit tests for scripts/native_usage.py — native-arm billed-usage recovery
from vendor CLI telemetry (opencode / kimi / vibe). Fixtures are trimmed from
real runs/ artifacts; no docker, no network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cell_validity
import native_usage
from native_usage import (
    enrich_score_file, parse_kimi_stdout, parse_opencode_stdout,
    parse_vibe_meta, recover_usage,
)

# --- fixtures (shapes lifted from real runs/ raw artifacts) -----------------

OPENCODE_STDOUT = "\n".join([
    json.dumps({"type": "step_start", "part": {}}),
    json.dumps({"type": "tool_use", "part": {"tool": "bash"}}),
    json.dumps({"type": "step_finish", "part": {
        "reason": "tool-calls",
        "tokens": {"total": 7259, "input": 5240, "output": 23, "reasoning": 76,
                   "cache": {"write": 0, "read": 1920}},
        "cost": 0.0071815}}),
    json.dumps({"type": "step_finish", "part": {
        "reason": "stop",
        "tokens": {"total": 11181, "input": 163, "output": 1, "reasoning": 9,
                   "cache": {"write": 40, "read": 11008}},
        "cost": 0.00243035}}),
    "not json at all",
])

KIMI_STDOUT = """
TurnBegin(user_input='find the needle.')
StepBegin(n=1)
StatusUpdate(
    context_usage=0.05,
    context_tokens=6000,
    max_context_tokens=131072,
    token_usage=TokenUsage(
        input_other=5000,
        output=40,
        input_cache_read=1000,
        input_cache_creation=0
    ),
    message_id='chatcmpl-aaa',
    plan_mode=False,
    mcp_status=None
)
StepBegin(n=2)
StatusUpdate(
    context_usage=0.11,
    context_tokens=15567,
    max_context_tokens=131072,
    token_usage=TokenUsage(
        input_other=1231,
        output=58,
        input_cache_read=14336,
        input_cache_creation=0
    ),
    message_id='chatcmpl-bbb',
    plan_mode=False,
    mcp_status=None
)
TurnEnd()
"""

VIBE_META = json.dumps({
    "session_id": "abc",
    "stats": {
        "steps": 41,
        "session_prompt_tokens": 717781,
        "session_completion_tokens": 5968,
        "session_cost": 0.0,
    },
})


def zero_billed_payload(**over) -> dict:
    """A completed native cell of the harness-incomplete class (zero_billed)."""
    p = {
        "benchmark": "b", "agent": "m-native", "arm": "native",
        "control_type": "native", "resolved": True, "stop_reason": "pass",
        "turns_to_fix": 9, "tool_uses": 8,
        "input_tokens": 84630, "output_tokens": 656,
        "fresh_input_tokens": 0, "cache_read_tokens": 0,
        "cache_create_tokens": 0, "billed_tokens": 0,
        "estimated_cost_usd": 0.0292397, "wall_clock": 24.7,
        "timestamp": "2026-06-15T15:50:08Z",
    }
    p.update(over)
    return p


class TestParsers(unittest.TestCase):
    def test_opencode_sums_steps(self):
        u = parse_opencode_stdout(OPENCODE_STDOUT)
        self.assertEqual(u["source"], "opencode_stdout")
        self.assertEqual(u["api_calls"], 2)
        self.assertEqual(u["fresh_input_tokens"], 5403)
        self.assertEqual(u["cache_read_tokens"], 12928)
        self.assertEqual(u["cache_create_tokens"], 40)
        self.assertEqual(u["output_tokens"], 24 + 85)   # output + reasoning
        self.assertEqual(u["reasoning_tokens"], 85)
        self.assertEqual(u["billed_tokens"], 5403 + 12928 + 40)
        self.assertAlmostEqual(u["cost_usd"], 0.009612, places=6)

    def test_opencode_none_when_no_steps(self):
        self.assertIsNone(parse_opencode_stdout("plain text\n"))
        self.assertIsNone(parse_opencode_stdout(KIMI_STDOUT))

    def test_kimi_sums_per_message_id(self):
        u = parse_kimi_stdout(KIMI_STDOUT)
        self.assertEqual(u["source"], "kimi_stdout")
        self.assertEqual(u["api_calls"], 2)
        self.assertEqual(u["fresh_input_tokens"], 6231)
        self.assertEqual(u["cache_read_tokens"], 15336)
        self.assertEqual(u["output_tokens"], 98)
        self.assertEqual(u["billed_tokens"], 6231 + 15336)
        self.assertIsNone(u["cost_usd"])  # kimi CLI reports no cost

    def test_kimi_none_on_foreign_text(self):
        self.assertIsNone(parse_kimi_stdout(OPENCODE_STDOUT))

    def test_vibe_meta_session_totals(self):
        u = parse_vibe_meta(VIBE_META)
        self.assertEqual(u["source"], "vibe_meta")
        self.assertEqual(u["fresh_input_tokens"], 717781)
        self.assertEqual(u["output_tokens"], 5968)
        self.assertEqual(u["billed_tokens"], 717781)
        self.assertIsNone(u["cost_usd"])  # 0.0 session_cost is never trusted

    def test_recover_precedence_and_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "b.raw"
            raw.mkdir()
            (raw / "native.stdout").write_text(KIMI_STDOUT)
            self.assertEqual(recover_usage(raw)["source"], "kimi_stdout")
            (raw / "native.stdout").write_text(OPENCODE_STDOUT)
            self.assertEqual(recover_usage(raw)["source"], "opencode_stdout")
            (raw / "native.stdout").write_text("nothing useful")
            (raw / "vibe-meta.json").write_text(VIBE_META)
            self.assertEqual(recover_usage(raw)["source"], "vibe_meta")
            (raw / "vibe-meta.json").unlink()
            self.assertIsNone(recover_usage(raw))


class TestEnrichment(unittest.TestCase):
    def _cell(self, tmp: Path, payload: dict, stdout: str | None,
              vibe: str | None = None) -> Path:
        score = tmp / "b.score.json"
        score.write_text(json.dumps(payload))
        raw = tmp / "b.raw"
        raw.mkdir(exist_ok=True)
        if stdout is not None:
            (raw / "native.stdout").write_text(stdout)
        if vibe is not None:
            (raw / "vibe-meta.json").write_text(vibe)
        return score

    def test_zero_billed_cell_becomes_valid_after_capture(self):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            score = self._cell(tmp, zero_billed_payload(), OPENCODE_STDOUT)
            before = cell_validity.classify_cell(
                json.loads(score.read_text()), requested_arm="native")
            self.assertIn(cell_validity.REASON_ZERO_BILLED, before.reasons)

            changed, note = enrich_score_file(score)
            self.assertTrue(changed)
            payload = json.loads(score.read_text())
            self.assertEqual(payload["native_usage_source"], "opencode_stdout")
            self.assertEqual(payload["billed_tokens"], 5403 + 12928 + 40)
            self.assertEqual(payload["fresh_input_tokens"], 5403)
            self.assertEqual(payload["cache_read_tokens"], 12928)
            self.assertEqual(payload["output_tokens"], 109)
            # provider-billed cost from the writer is preserved, not clobbered
            self.assertAlmostEqual(payload["estimated_cost_usd"], 0.0292397)
            self.assertEqual(payload["native_usage_prev"]["input_tokens"], 84630)
            after = cell_validity.classify_cell(payload, requested_arm="native")
            self.assertEqual(after.status, cell_validity.VALID)

    def test_enrichment_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            score = self._cell(tmp, zero_billed_payload(), OPENCODE_STDOUT)
            enrich_score_file(score)
            changed, note = enrich_score_file(score)
            self.assertFalse(changed)
            self.assertEqual(note, "already accounted")

    def test_already_accounted_payload_untouched(self):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            payload = zero_billed_payload(billed_tokens=39112,
                                          fresh_input_tokens=39112)
            score = self._cell(tmp, payload, OPENCODE_STDOUT)
            changed, _ = enrich_score_file(score)
            self.assertFalse(changed)
            self.assertEqual(json.loads(score.read_text()), payload)

    def test_unrecoverable_marks_cost_basis_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            score = self._cell(tmp, zero_billed_payload(), "no telemetry here")
            changed, note = enrich_score_file(score)
            self.assertTrue(changed)
            payload = json.loads(score.read_text())
            self.assertEqual(payload["cost_basis"], "unavailable")
            self.assertEqual(payload["native_usage_source"], "none")
            # solve verdict untouched — resolved stands
            self.assertTrue(payload["resolved"])
            # second pass: no churn
            changed2, note2 = enrich_score_file(score)
            self.assertFalse(changed2)

    def test_passive_verify_cell_never_marked_unavailable(self):
        """A passive-verify cell (stop=pass, 0 turns, 0 tokens) has a
        LEGITIMATE $0 cost — no telemetry is expected and no stamp applied."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            payload = zero_billed_payload(
                turns_to_fix=0, tool_uses=0, input_tokens=0, output_tokens=0,
                estimated_cost_usd=0.0, stop_reason="pass", resolved=True)
            score = self._cell(tmp, payload, "no telemetry")
            changed, note = enrich_score_file(score)
            self.assertFalse(changed)
            got = json.loads(score.read_text())
            self.assertNotIn("cost_basis", got)
            self.assertNotIn("native_usage_source", got)

    def test_zero_work_cell_never_marked_unavailable(self):
        """A zero-work fail (solve-axis INVALID upstream) is not a billing
        gap — leave it for the retry/quarantine machinery."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            payload = zero_billed_payload(
                turns_to_fix=0, tool_uses=0, input_tokens=0, output_tokens=0,
                estimated_cost_usd=0.0, stop_reason="fail", resolved=False)
            score = self._cell(tmp, payload, "no telemetry")
            changed, _ = enrich_score_file(score)
            self.assertFalse(changed)
            self.assertNotIn("cost_basis", json.loads(score.read_text()))

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            original = zero_billed_payload()
            score = self._cell(tmp, original, OPENCODE_STDOUT)
            changed, _ = enrich_score_file(score, apply=False)
            self.assertTrue(changed)
            self.assertEqual(json.loads(score.read_text()), original)

    def test_vibe_cell_gets_session_totals(self):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            payload = zero_billed_payload(
                input_tokens=717781, output_tokens=5968, estimated_cost_usd=0.0)
            score = self._cell(tmp, payload, "vibe cli chatter", vibe=VIBE_META)
            changed, _ = enrich_score_file(score)
            self.assertTrue(changed)
            got = json.loads(score.read_text())
            self.assertEqual(got["native_usage_source"], "vibe_meta")
            self.assertEqual(got["billed_tokens"], 717781)
            self.assertEqual(got["fresh_input_tokens"], 717781)
            # no fabricated cost: 0.0 stays 0.0 (consolidator prices buckets)
            self.assertEqual(got["estimated_cost_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
