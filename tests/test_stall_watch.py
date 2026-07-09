#!/usr/bin/env python3
"""Unit tests for scripts/stall_watch.py — merciless per-cell stall detection.

Covers the three detectors with fakes (no docker, no network, no paid calls):
  - journal-completion: fixture journal with a terminal end_turn row + a child
    process that hangs (sleep) -> grace -> SIGKILL -> score synthesized from
    the journal with teardown_masked=true.
  - score-landed completion: score.json appears but the child hangs -> grace
    -> SIGKILL -> payload stamped teardown_masked=true.
  - no-progress watchdog: a journal that stops advancing + a silent child ->
    WARN -> probe -> SIGKILL -> cell written INVALID reason 'stall'.
  - clean exit: fast child -> no kill, no masking.
  - journal metric parsing + status ledger rendering.
"""

import io
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tempfile

import cell_validity
import stall_watch
from stall_watch import (
    Thresholds, WatchResult, journal_terminal_row, parse_journal_metrics,
    render_status, score_from_journal, stall_score, watch_cell,
)

API_CALL_ROW = {
    "event": "api.call", "input_tokens": 200, "output_tokens": 50,
    "cache_read_tokens": 1000, "cache_create_tokens": 30,
    "cache_create_5m_tokens": 30, "cache_create_1h_tokens": 0,
    "billed_tokens": 1230, "cost_usd": 0.0125, "stop_reason": "tool_use",
}
TOOL_ROW = {"event": "tool.dispatch", "tool": "sh"}
TEST_BASH_ROW = {"event": "tool.bash", "cmd": "cd /app && sh test.sh", "exit_code": 0}
TERMINAL_DECODED_ROW = {
    "event": "cpu.response.decoded", "stop_reason": "end_turn", "tool_calls": 0,
}


def journal_text(rows) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def fast_thresholds() -> Thresholds:
    return Thresholds(warn_s=0.3, probe_s=0.6, kill_s=1.2, grace_s=0.4,
                      tick_s=0.05, heartbeat_s=60.0,
                      container_probe_every_ticks=1)


class FakeProbe:
    """Stands in for DockerCellProbe: serves a scripted journal, records
    kill/salvage calls, answers the oracle re-run."""

    def __init__(self, journal: str | None = None, resolved: bool | None = None,
                 activity: bool = False):
        self.journal = journal
        self.resolved = resolved
        self.activity = activity
        self.removed = False
        self.salvaged_to = None

    def journal_size(self):
        return len(self.journal) if self.journal is not None else None

    def journal_text(self):
        return self.journal

    def activity_since(self, epoch):
        return self.activity

    def container_state(self):
        return "running"

    def stats_line(self):
        return "cpu=0.00%"

    def run_resolution_test(self):
        return self.resolved

    def salvage_journal(self, dest: Path) -> bool:
        if not self.journal:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.journal)
        self.salvaged_to = dest
        return True

    def remove(self):
        self.removed = True


class TestJournalParsing(unittest.TestCase):
    def test_terminal_row_detection(self):
        rows = [API_CALL_ROW, TOOL_ROW]
        self.assertIsNone(journal_terminal_row(journal_text(rows)))
        rows.append(TERMINAL_DECODED_ROW)
        self.assertIsNotNone(journal_terminal_row(journal_text(rows)))

    def test_lifecycle_event_is_terminal(self):
        rows = [API_CALL_ROW, {"event": "agent.completed", "exit_code": 0}]
        term = journal_terminal_row(journal_text(rows))
        self.assertEqual(term["event"], "agent.completed")

    def test_end_turn_with_pending_tool_calls_not_terminal(self):
        row = dict(TERMINAL_DECODED_ROW, tool_calls=2)
        self.assertIsNone(journal_terminal_row(journal_text([row])))

    def test_metrics_sums_mirror_bench_rs(self):
        rows = [API_CALL_ROW, API_CALL_ROW, TOOL_ROW, TOOL_ROW, TOOL_ROW,
                TEST_BASH_ROW, TERMINAL_DECODED_ROW]
        m = parse_journal_metrics(journal_text(rows))
        self.assertEqual(m["turns"], 2)
        self.assertEqual(m["tool_uses"], 3)
        self.assertEqual(m["input_tokens"], 400)
        self.assertEqual(m["output_tokens"], 100)
        self.assertEqual(m["cache_read_tokens"], 2000)
        self.assertEqual(m["billed_tokens"], 2460)
        self.assertAlmostEqual(m["cost_usd"], 0.025)
        self.assertTrue(m["end_turn_reached"])
        self.assertEqual(m["stop_reason"], "end_turn")
        self.assertEqual(m["last_test_exit"], 0)

    def test_score_from_journal_oracle_beats_journal_heuristic(self):
        text = journal_text([API_CALL_ROW, TEST_BASH_ROW, TERMINAL_DECODED_ROW])
        score = score_from_journal(text, model="m", bench="b", arm="kernel-cpu",
                                   resolved=False, wall_clock_s=12.0)
        self.assertFalse(score["resolved"])
        self.assertEqual(score["resolution_source"], "container:test.sh")
        self.assertTrue(score["teardown_masked"])
        self.assertEqual(score["agent"], "m-kernel-cpu")
        # oracle unavailable -> falls back to journaled test.sh exit
        score2 = score_from_journal(text, model="m", bench="b", arm="kernel-cpu",
                                    resolved=None, wall_clock_s=12.0)
        self.assertTrue(score2["resolved"])
        self.assertEqual(score2["resolution_source"], "journal:last_test_exit")
        self.assertEqual(score2["billed_tokens"], 1230)


class TestWatchCell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name) / "runs"
        self.score = self.runs / "m-kernel-cpu" / "b.score.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _watch(self, cmd, probe, arm="kernel-cpu", deadline=30.0,
               th=None) -> WatchResult:
        return watch_cell(cmd, model="m", bench="b", arm=arm,
                          score_path=self.score, runs_root=self.runs,
                          deadline_s=deadline, thresholds=th or fast_thresholds(),
                          probe=probe)

    def test_clean_exit_no_kill_no_mask(self):
        probe = FakeProbe(journal=journal_text([API_CALL_ROW]))
        res = self._watch(["true"], probe)
        self.assertEqual(res.status, "exit")
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.teardown_masked)
        self.assertFalse(probe.removed)

    def test_terminal_journal_hanging_child_scored_and_masked(self):
        """The operator's 20-minute defect: terminal end_turn journaled,
        teardown hangs. Grace, SIGKILL, score from journal, masked."""
        probe = FakeProbe(
            journal=journal_text([API_CALL_ROW, TOOL_ROW, TEST_BASH_ROW,
                                  TERMINAL_DECODED_ROW]),
            resolved=True)
        t0 = time.time()
        res = self._watch(["sleep", "60"], probe)
        self.assertLess(time.time() - t0, 20, "watchdog must not ride the sleep")
        self.assertEqual(res.status, "completed_kill")
        self.assertTrue(res.teardown_masked)
        self.assertTrue(probe.removed, "container must be removed")
        payload = json.loads(self.score.read_text())
        self.assertTrue(payload["resolved"])
        self.assertTrue(payload["teardown_masked"])
        self.assertEqual(payload["scored_by"], "stall_watch:journal")
        self.assertEqual(payload["input_tokens"], 200)
        self.assertEqual(payload["billed_tokens"], 1230)
        # salvaged journal persisted for re-derivation
        self.assertTrue((self.score.parent / "b.raw" / "journal.jsonl").exists())
        # the synthesized cell is VALID per cell_validity (real accounting)
        cls = cell_validity.classify_cell(payload, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cell_validity.VALID)

    def test_score_landed_hanging_child_stamped_masked(self):
        """bench wrote score.json then hung in teardown: kill + stamp, and the
        payload records teardown_masked ONLY because the kill was needed."""
        self.score.parent.mkdir(parents=True, exist_ok=True)
        self.score.write_text(json.dumps({
            "benchmark": "b", "agent": "m-kernel-cpu", "arm": "kernel-cpu",
            "resolved": True, "turns_to_fix": 3, "input_tokens": 100,
            "output_tokens": 20, "billed_tokens": 120, "stop_reason": "end_turn",
        }))
        probe = FakeProbe(journal=None)
        res = self._watch(["sleep", "60"], probe)
        self.assertEqual(res.status, "completed_kill")
        payload = json.loads(self.score.read_text())
        self.assertTrue(payload["teardown_masked"])
        self.assertEqual(payload["teardown_masked_by"], "stall_watch")

    def test_clean_exit_after_score_is_not_masked(self):
        """Score lands and the child exits inside grace: no kill, no mask."""
        self.score.parent.mkdir(parents=True, exist_ok=True)
        self.score.write_text(json.dumps({"benchmark": "b", "resolved": True}))
        probe = FakeProbe(journal=None)
        res = self._watch(["sleep", "0.1"], probe)
        self.assertEqual(res.status, "exit")
        payload = json.loads(self.score.read_text())
        self.assertNotIn("teardown_masked", payload)

    def test_static_journal_silent_child_is_stall_killed(self):
        """Fake journal that stops advancing + silent child -> WARN -> probe ->
        SIGKILL -> INVALID cell, reason 'stall'."""
        probe = FakeProbe(journal=journal_text([API_CALL_ROW]))  # never grows
        t0 = time.time()
        res = self._watch(["sleep", "60"], probe)
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(res.status, "stall_kill")
        self.assertFalse(res.teardown_masked)
        payload = json.loads(self.score.read_text())
        self.assertEqual(payload["stop_reason"], "stall")
        cls = cell_validity.classify_cell(payload, requested_arm="kernel-cpu")
        self.assertEqual(cls.status, cell_validity.INVALID)
        self.assertIn(cell_validity.REASON_STALL, cls.reasons)
        # escalation ladder recorded loudly
        states = [r["state"] for r in res.timeline]
        self.assertIn("warn_no_progress", states)
        self.assertIn("probing", states)
        self.assertIn("killed_stall", states)

    def test_native_arm_stall_kill_without_docker(self):
        """Native arm, no journal, probe returns nothing: output silence alone
        drives the ladder."""
        probe = FakeProbe(journal=None, activity=False)
        res = self._watch(["sleep", "60"], probe, arm="native")
        self.assertEqual(res.status, "stall_kill")
        payload = json.loads(self.score.read_text())
        self.assertEqual(payload["stop_reason"], "stall")

    def test_native_arm_container_activity_defers_kill(self):
        """Container filesystem activity counts as progress for native arms —
        a quiet-but-working vendor CLI is NOT killed."""
        probe = FakeProbe(journal=None, activity=True)
        th = fast_thresholds()
        res = watch_cell(["sleep", "1.0"], model="m", bench="b", arm="native",
                         score_path=self.score, runs_root=self.runs,
                         deadline_s=30.0, thresholds=th, probe=probe)
        self.assertEqual(res.status, "exit")

    def test_hard_deadline_kill(self):
        th = fast_thresholds()
        # keep the stall ladder from firing first: activity keeps progress fresh
        probe = FakeProbe(journal=None, activity=True)
        res = watch_cell(["sleep", "60"], model="m", bench="b", arm="native",
                         score_path=self.score, runs_root=self.runs,
                         deadline_s=1.0, thresholds=th, probe=probe)
        self.assertEqual(res.status, "deadline_kill")
        payload = json.loads(self.score.read_text())
        self.assertEqual(payload["stop_reason"], "stall")
        self.assertIn("hard deadline", payload["summary"])

    def test_growing_journal_is_progress(self):
        """A journal that grows keeps the cell alive past the kill threshold."""
        probe = FakeProbe(journal=journal_text([API_CALL_ROW]))
        th = fast_thresholds()

        real_size = probe.journal_size

        def growing_size():
            probe.journal = probe.journal + " "  # grow every poll
            return real_size()

        probe.journal_size = growing_size
        res = watch_cell(["sleep", "2.5"], model="m", bench="b", arm="kernel-cpu",
                         score_path=self.score, runs_root=self.runs,
                         deadline_s=30.0, thresholds=th, probe=probe)
        self.assertEqual(res.status, "exit")

    def test_every_transition_lands_in_status_ledger(self):
        probe = FakeProbe(journal=None)
        self._watch(["sleep", "60"], probe, arm="native")
        ledger = self.runs / stall_watch.STATUS_BASENAME
        rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        states = [r["state"] for r in rows]
        self.assertIn("running", states)
        self.assertIn("warn_no_progress", states)
        self.assertIn("killed_stall", states)
        for r in rows:
            self.assertIn("ts", r)
            self.assertEqual((r["model"], r["bench"], r["arm"]),
                             ("m", "b", "native"))


class TestStatusRender(unittest.TestCase):
    def test_render_shows_last_state_per_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / ".cell_status.jsonl"
            rows = [
                {"ts": "2026-07-09T10:00:00+00:00", "model": "m1", "bench": "b1",
                 "arm": "native", "state": "running", "detail": "pid=1"},
                {"ts": "2026-07-09T10:01:00+00:00", "model": "m1", "bench": "b1",
                 "arm": "native", "state": "done", "detail": "exit rc=0"},
                {"ts": "2026-07-09T10:02:00+00:00", "model": "m1", "bench": "b2",
                 "arm": "kernel-cpu", "state": "warn_no_progress",
                 "detail": "no progress for 92s"},
            ]
            ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            buf = io.StringIO()
            rc = render_status(ledger, out=buf)
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("m1/b1/native", text)
            self.assertIn("done", text)
            self.assertIn("warn_no_progress", text)
            self.assertIn("1 live cell(s)", text)

    def test_missing_ledger_is_not_an_error(self):
        buf = io.StringIO()
        rc = render_status(Path("/nonexistent/.cell_status.jsonl"), out=buf)
        self.assertEqual(rc, 0)


class TestStallScoreShape(unittest.TestCase):
    def test_stall_score_is_solve_axis_invalid(self):
        payload = stall_score(model="m", bench="b", arm="kernel",
                              wall_clock_s=301.0, detail="no progress for 300s",
                              timeline=[])
        cls = cell_validity.classify_cell(payload, requested_arm="kernel")
        self.assertEqual(cls.status, cell_validity.INVALID)
        self.assertFalse(cls.solve_valid)
        self.assertIn(cell_validity.REASON_STALL, cls.solve_reasons)


if __name__ == "__main__":
    unittest.main()
