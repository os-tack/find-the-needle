#!/usr/bin/env python3
"""
annotate_teardown.py — derive `teardown_masked` visibility for historical runs.

Some kernel-arm cells were scored by a journal + SIGKILL watchdog after the
kernel teardown hang (futex deadlock, orphaned pipe holder — see
diag-teardown-hang/). The score payloads written at the time carry NO record
of that masking; the only evidence is the watchdog line on the ostk bench
stdout captured in the per-cell run logs:

    [kernel] agent journaled terminal end_turn; teardown blocks on an
    orphaned pipe holder and will not exit — scoring from journal + SIGKILL
    process tree

This script joins those logs to the score files and writes a
`_teardown_masked.json` sidecar into each run directory (it NEVER rewrites a
score payload). consolidate_scores.py reads the sidecar and publishes
`teardown_masked: true` per cell + per-model masked counts (a score payload's
own `teardown_masked` field, once writers stamp it, always wins).

HONESTY GUARD: a log only stamps a score when the score's own timestamp falls
within --window-hours of the log file's mtime. This matters because two run
dirs (claude-opus-4-8-kernel-cpu, gemini-3.1-pro-preview-kernel-cpu) hold
July-2 rerun payloads while the surviving watchdog logs are from the June-16
run whose payloads were deleted — those must NOT be stamped from June logs;
they are reported as skipped_time_mismatch instead.

Offline, stdlib-only, idempotent (sidecars are regenerated wholesale).
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

MARKER = "teardown blocks on an orphaned pipe holder"
DEFAULT_WINDOW_HOURS = 72.0
SIDECAR_NAME = "_teardown_masked.json"

# (log dir, log filename prefix, run dir) — the four historical watchdog-scored
# kernel runs (June 16-17 2026) plus the two July-2 rerun dirs, which are
# included so the time-mismatch skip is REPORTED rather than silent.
DEFAULT_PAIRS = [
    ("rerun-opus-sonnet-kernel-logs", "claude-sonnet-4-6", "runs/claude-sonnet-4-6-kernel-cpu"),
    ("rerun-opus-sonnet-kernel-logs", "claude-opus-4-8", "runs/claude-opus-4-8-kernel-cpu"),
    ("rerun-gemini-kernel-logs", "gemini-3.1-pro-preview", "runs/gemini-3.1-pro-preview-kernel-cpu"),
    ("run-gpt-kernel-logs", "gpt-5.5", "runs/gpt-5.5-kernel-cpu"),
    ("run-flash-both-logs", "gemini-3.5-flash", "runs/gemini-3.5-flash-kernel-cpu"),
]


def parse_score_ts(score: dict) -> datetime | None:
    ts = score.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def annotate_pair(log_dir: Path, prefix: str, run_dir: Path,
                  window_hours: float, dry_run: bool) -> dict | None:
    """Build (and optionally write) one run dir's sidecar. Returns the sidecar
    dict, or None when the pair has nothing to say (missing dirs / no logs)."""
    if not log_dir.is_dir() or not run_dir.is_dir():
        print(f"  skip: missing dir ({log_dir if not log_dir.is_dir() else run_dir})")
        return None

    masked: dict[str, dict] = {}
    skipped_time_mismatch: dict[str, dict] = {}
    no_score: list[str] = []
    logs_seen = 0

    for log_fp in sorted(log_dir.glob(f"{prefix}-*.log")):
        bench = log_fp.name[len(prefix) + 1:-len(".log")]
        logs_seen += 1
        try:
            if MARKER not in log_fp.read_text(errors="replace"):
                continue
        except OSError as e:
            print(f"  WARN: unreadable log {log_fp} — {e}", file=sys.stderr)
            continue
        score_fp = run_dir / f"{bench}.score.json"
        if not score_fp.exists():
            no_score.append(bench)
            continue
        try:
            score = json.loads(score_fp.read_text())
        except (json.JSONDecodeError, OSError):
            no_score.append(bench)
            continue
        score_ts = parse_score_ts(score)
        log_mtime = datetime.fromtimestamp(log_fp.stat().st_mtime, tz=timezone.utc)
        detail = {
            "log": str(log_fp.relative_to(ROOT)),
            "log_mtime": log_mtime.isoformat(timespec="seconds"),
            "score_timestamp": score.get("timestamp"),
        }
        if score_ts is None:
            # No timestamp to verify against — fail closed, do not stamp.
            skipped_time_mismatch[bench] = {**detail, "why": "score has no parseable timestamp"}
            continue
        delta_h = abs((score_ts - log_mtime).total_seconds()) / 3600.0
        detail["delta_hours"] = round(delta_h, 1)
        if delta_h <= window_hours:
            masked[bench] = detail
        else:
            # e.g. June-16 watchdog log vs July-2 rerun payload: the masked
            # run's scores were deleted; the current payload is NOT provably
            # masked by this log. Reported, never stamped.
            skipped_time_mismatch[bench] = detail

    sidecar = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "marker": MARKER,
        "source_log_dir": str(log_dir.relative_to(ROOT)),
        "window_hours": window_hours,
        "note": ("benchmarks = cells whose score timestamp matches a watchdog "
                 "log within window_hours; skipped_time_mismatch = watchdog "
                 "evidence exists but for a DIFFERENT (deleted/rerun) execution "
                 "of this cell — not stamped."),
        "benchmarks": masked,
        "skipped_time_mismatch": skipped_time_mismatch,
        "marker_without_score": sorted(no_score),
    }

    out_fp = run_dir / SIDECAR_NAME
    print(f"  {run_dir.name}: {logs_seen} logs, {len(masked)} masked, "
          f"{len(skipped_time_mismatch)} time-mismatch skipped, {len(no_score)} no-score")
    if dry_run:
        print(f"    [dry-run] would write {out_fp}")
    elif masked or skipped_time_mismatch:
        out_fp.write_text(json.dumps(sidecar, indent=2) + "\n")
        print(f"    wrote {out_fp}")
    else:
        print("    nothing to record — sidecar not written")
    return sidecar


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive teardown_masked sidecars from watchdog run logs.")
    ap.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS,
                    help="max |score timestamp - log mtime| to accept a stamp (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="report without writing sidecars")
    ap.add_argument("--pair", nargs=3, action="append", metavar=("LOG_DIR", "PREFIX", "RUN_DIR"),
                    help="extra/override (log_dir, filename_prefix, run_dir) triple; "
                         "may repeat. Replaces the built-in list when given.")
    args = ap.parse_args()

    pairs = args.pair if args.pair else DEFAULT_PAIRS
    print(f"annotate_teardown: window={args.window_hours}h, {len(pairs)} pairs")
    for log_dir, prefix, run_dir in pairs:
        annotate_pair(ROOT / log_dir, prefix, ROOT / run_dir, args.window_hours, args.dry_run)


if __name__ == "__main__":
    main()
