#!/usr/bin/env python3
"""Regression tests for the P0 native cost-accounting defect.

Two independent guarantees, one per axis of the fix:

  AC1 — cell_validity no longer blesses a zero-ACCOUNTING "pass" (turns==0 AND
        input+output==0, stop=="pass") as fully VALID. It is COST_INVALID: the
        SOLVE verdict is preserved (native resolution is exit-code-gated) while
        the cost axis is UNAVAILABLE and never priced. The former passive_verify
        exemption let native error_max_turns results — written as 0-turn/0-token
        passes with the real >$1 spend discarded — count as legitimate $0 cells.

  AC3 — the native parser extracts num_turns AND token usage from a Claude Code
        result envelope EVEN WHEN is_error:true / subtype:error_max_turns, and
        from the Gemini CLI end-of-run stats block. Fixtures are the real
        nginx-upstream-port-mismatch launcher payload and the real
        silent-data-corruption Gemini stats shape.

Hermetic: no docker, no network, no dependency on runs/ contents.
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cell_validity as cv
import native_usage as nu

# --- fixtures ----------------------------------------------------------------

# The REAL Claude Code result envelope for
# runs/claude-sonnet-5-native/nginx-upstream-port-mismatch (verbatim from its
# launcher.log / .raw/native.stdout): error_max_turns after 41 turns, $1.40
# billed — every byte of which the native writer discarded to a 0/0 "pass".
NGINX_RESULT_JSON = '{"type":"result","subtype":"error_max_turns","duration_ms":283992,"duration_api_ms":251052,"is_error":true,"num_turns":41,"stop_reason":"tool_use","session_id":"495cc7e5-d22e-4192-92ba-4ce2d14827a6","total_cost_usd":1.4020935000000005,"usage":{"input_tokens":2409,"cache_creation_input_tokens":29104,"cache_read_input_tokens":1617801,"output_tokens":15946,"server_tool_use":{"web_search_requests":0,"web_fetch_requests":0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":29104},"inference_geo":"global","iterations":[{"input_tokens":2,"output_tokens":1312,"cache_read_input_tokens":52632,"cache_creation_input_tokens":390,"cache_creation":{"ephemeral_5m_input_tokens":390,"ephemeral_1h_input_tokens":0},"type":"message"}],"speed":"standard"},"modelUsage":{"claude-sonnet-5":{"inputTokens":2409,"outputTokens":15946,"cacheReadInputTokens":1617801,"cacheCreationInputTokens":29104,"costUSD":1.4014955000000004}},"permission_denials":[],"terminal_reason":"max_turns","fast_mode_state":"off","uuid":"ffff0421-0ea4-4bce-a1c7-016bf394f246","errors":["Reached maximum number of turns (40)"]}'

# As it appears in launcher.log — prefixed by the harness log decoration.
NGINX_LAUNCHER_LINE = "  [native]   harness error: " + NGINX_RESULT_JSON


def _gemini_stdout(model="gemini-3.1-pro-preview",
                   *, requests=7, inp=20015, prompt=68320, candidates=838,
                   cached=48305, thoughts=797):
    """Reproduce the Gemini CLI native.stdout shape: noise preamble + a single
    pretty-printed stats object keyed on session_id (silent-data-corruption)."""
    tokens = {"input": inp, "prompt": prompt, "candidates": candidates,
              "total": prompt + candidates + thoughts, "cached": cached,
              "thoughts": thoughts, "tool": 0}
    api = {"totalRequests": requests, "totalErrors": 1, "totalLatencyMs": 38194}
    stats = {
        "session_id": "a1c15e91-8306-435e-bf9a-e0fc885905d5",
        "response": "Fixed the silent truncation bug; test.sh passes.",
        "stats": {"models": {model: {"api": api, "tokens": tokens}}},
    }
    return ("Warning: 256-color support not detected.\n"
            "YOLO mode is enabled.\n"
            "Attempt 1 failed with status 503. Retrying... {\"error\":503}\n"
            + json.dumps(stats, indent=2))


# The zero-ACCOUNTING pass exactly as the native writer persists it: resolved
# via test.sh exit code (trustworthy solve), but turns/tokens/cost all zeroed.
def _zero_accounting_pass():
    return {
        "benchmark": "nginx-upstream-port-mismatch",
        "agent": "claude-sonnet-5-native", "arm": "native",
        "resolved": True, "turns_to_fix": 0, "token_cost": 0,
        "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0,
        "fresh_input_tokens": 0, "cache_read_tokens": 0, "cache_create_tokens": 0,
        "billed_tokens": 0, "tool_uses": 0, "stop_reason": "pass",
    }


# ---------------------------------------------------------------------------
# AC1 — cost-axis classification of a zero-accounting pass
# ---------------------------------------------------------------------------
class TestZeroAccountingPassIsCostInvalid(unittest.TestCase):
    def test_zero_accounting_pass_is_cost_invalid_not_valid(self):
        r = cv.classify_cell(_zero_accounting_pass(), requested_arm="native")
        # The defect: this used to return VALID (fully cost-valid). It must not.
        self.assertEqual(r.status, cv.COST_INVALID)
        self.assertIn(cv.REASON_ZERO_ACCOUNTING_PASS, r.reasons)
        self.assertEqual(cv.reason_axis(cv.REASON_ZERO_ACCOUNTING_PASS), "cost")

    def test_solve_verdict_is_preserved(self):
        # AC2: the solve axis stays trustworthy — the cell still counts as a
        # solve; only the cost axis is unavailable.
        r = cv.classify_cell(_zero_accounting_pass(), requested_arm="native")
        self.assertTrue(r.solve_valid)
        self.assertFalse(r.cost_valid)
        self.assertEqual(r.solve_reasons, [])

    def test_recovered_payload_becomes_valid(self):
        # Once telemetry is recovered (backfill), the same cell is fully VALID.
        d = _zero_accounting_pass()
        d.update(fresh_input_tokens=2409, cache_read_tokens=1617801,
                 cache_create_tokens=29104, input_tokens=2409,
                 output_tokens=15946, billed_tokens=1649314, turns_to_fix=41)
        r = cv.classify_cell(d, requested_arm="native")
        self.assertEqual(r.status, cv.VALID)

    def test_token_bearing_pass_stays_valid(self):
        # Guard against over-firing: a real pass WITH accounting is untouched.
        d = _zero_accounting_pass()
        d.update(fresh_input_tokens=2409, cache_read_tokens=100,
                 input_tokens=2409, output_tokens=15946, billed_tokens=2509,
                 turns_to_fix=41)
        self.assertEqual(cv.classify_cell(d, requested_arm="native").status,
                         cv.VALID)

    def test_zero_work_nonpass_stays_solve_invalid(self):
        # A 0/0 cell that did NOT pass is still solve-axis INVALID (unchanged).
        d = _zero_accounting_pass()
        d.update(resolved=False, stop_reason="end_turn")
        r = cv.classify_cell(d, requested_arm="native")
        self.assertEqual(r.status, cv.INVALID)
        self.assertIn(cv.REASON_ZERO_WORK, r.reasons)


# ---------------------------------------------------------------------------
# AC3 — native parser recovers num_turns + usage from error results
# ---------------------------------------------------------------------------
class TestClaudeResultParser(unittest.TestCase):
    def _assert_nginx(self, u):
        self.assertIsNotNone(u)
        self.assertEqual(u["source"], "claude_result_json")
        self.assertEqual(u["num_turns"], 41)           # <-- extracted despite is_error
        self.assertEqual(u["fresh_input_tokens"], 2409)
        self.assertEqual(u["cache_read_tokens"], 1617801)
        self.assertEqual(u["cache_create_tokens"], 29104)
        self.assertEqual(u["output_tokens"], 15946)
        self.assertEqual(u["billed_tokens"], 2409 + 1617801 + 29104)  # 1_649_314
        self.assertAlmostEqual(u["cost_usd"], 1.402094, places=5)

    def test_parses_error_max_turns_result(self):
        self._assert_nginx(nu.parse_claude_result_json(NGINX_RESULT_JSON))

    def test_parses_from_launcher_log_prefixed_line(self):
        # Same envelope, but embedded in a decorated launcher.log line.
        self._assert_nginx(nu.parse_claude_result_json(NGINX_LAUNCHER_LINE))

    def test_ignores_non_result_lines(self):
        noise = '{"type":"message","content":"hi"}\nplain text\n'
        self.assertIsNone(nu.parse_claude_result_json(noise))


class TestGeminiStatsParser(unittest.TestCase):
    def test_parses_gemini_end_of_run_stats(self):
        u = nu.parse_gemini_stats(_gemini_stdout())
        self.assertIsNotNone(u)
        self.assertEqual(u["source"], "gemini_stats")
        self.assertEqual(u["num_turns"], 7)                 # api.totalRequests
        self.assertEqual(u["fresh_input_tokens"], 20015)    # prompt - cached
        self.assertEqual(u["cache_read_tokens"], 48305)     # cached
        self.assertEqual(u["cache_create_tokens"], 0)       # no split on Gemini
        self.assertEqual(u["output_tokens"], 838 + 797)     # candidates+thoughts
        self.assertEqual(u["reasoning_tokens"], 797)
        self.assertEqual(u["billed_tokens"], 68320)
        self.assertIsNone(u["cost_usd"])                    # priced by consolidator


# ---------------------------------------------------------------------------
# End-to-end: enrich a native score from a .raw/native.stdout, then classify.
# ---------------------------------------------------------------------------
class TestEnrichThenClassify(unittest.TestCase):
    def _run(self, stdout_text, score):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bench = score["benchmark"]
            raw = root / f"{bench}.raw"
            raw.mkdir()
            (raw / "native.stdout").write_text(stdout_text)
            sp = root / f"{bench}.score.json"
            sp.write_text(json.dumps(score))
            changed, note = nu.enrich_score_file(sp, apply=True)
            return changed, note, json.loads(sp.read_text())

    def test_claude_error_result_enriches_and_validates(self):
        changed, note, patched = self._run(NGINX_RESULT_JSON,
                                            _zero_accounting_pass())
        self.assertTrue(changed)
        self.assertEqual(patched["billed_tokens"], 1649314)
        self.assertEqual(patched["output_tokens"], 15946)
        self.assertEqual(patched["turns_to_fix"], 41)      # turn count recovered
        self.assertEqual(patched["native_usage_source"], "claude_result_json")
        self.assertEqual(patched["native_usage_prev"]["turns_to_fix"], 0)
        # After recovery the cost axis is trustworthy again -> fully VALID.
        self.assertEqual(cv.classify_cell(patched, requested_arm="native").status,
                         cv.VALID)

    def test_gemini_stats_enriches_and_validates(self):
        score = _zero_accounting_pass()
        score["benchmark"] = "silent-data-corruption"
        score["agent"] = "gemini-3.1-pro-preview-native"
        changed, note, patched = self._run(_gemini_stdout(), score)
        self.assertTrue(changed)
        self.assertEqual(patched["billed_tokens"], 68320)
        self.assertEqual(patched["turns_to_fix"], 7)
        self.assertEqual(cv.classify_cell(patched, requested_arm="native").status,
                         cv.VALID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
