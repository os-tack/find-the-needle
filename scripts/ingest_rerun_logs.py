#!/usr/bin/env python3
"""
ingest_rerun_logs.py — recover the stranded June kernel-arm scores.

The June 16–17 kernel(-cpu) reruns for five models wrote their score payloads
directly into runs/<model>-kernel-cpu/ while the *-logs/ dirs captured only
the ostk bench stdout per cell. Two of those payload sets were later
destroyed on disk (claude-opus-4-8: rm -f'd then overwritten by the July-2
rerun; gemini-3.1-pro-preview: replaced by the July-2 post-quarantine rerun)
— but the June payloads survive as git-HEAD blobs (and, for gemini, in the
explicitly-safe runs-archive/*.pre-rerun-20260702-181539/ archive).

This script joins every per-cell run log to its score payload and:

  already_ingested      — the working-tree score IS the run the log records
                          (field-by-field receipt match on the log's
                          `[kernel] complete:` line). Left untouched.
  emitted               — the working-tree score belongs to a DIFFERENT run
                          (July-2 rerun payloads vs June logs): the June
                          payload is recovered from the git HEAD blob,
                          verified against the log receipts, and written as
                          <bench>.recovered-june16.score.json alongside the
                          July file (the consolidator's documented
                          keep-best-per-cell rule then merges them; nothing
                          pre-existing is ever overwritten).
  skipped_retired_bench — haystack-boot/haystack-mint: deliberately deleted
                          from the working tree (retired, PUBLISHED_EXCLUDE);
                          never resurrected.
  skipped_ambiguous     — any receipt mismatch or missing payload. NEVER
                          fabricated: cells are only emitted from a real,
                          verified JSON payload (git blob or safe archive) —
                          never synthesized from stdout lines.

Every emitted score carries provenance: ingested_by, source_log,
teardown_masked (derived from the watchdog line in ITS OWN log),
recovered_from, recovery_cross_check (gemini: HEAD blob must equal the safe
archive copy byte-for-JSON), and binary_identity (the mutable build path the
log records + the run header's ostk version — status 'unverified-path-only';
frozen-bin/ hashes are plausible for June runs but not provable, so they are
NOT asserted).

Idempotent: re-running compares JSON content and rewrites nothing that is
already identical; filenames are deterministic so no duplicates can appear.
Offline, stdlib-only. Does NOT read runs-archive/ contaminated/invalid sets
(only the one explicitly-safe pre-rerun archive, as a cross-check).
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"

WATCHDOG_MARKER = "teardown blocks on an orphaned pipe holder"
COMPLETE_RE = re.compile(
    r"\[kernel\] complete: (\d+) turns?, (\d+)\+(\d+) tokens, "
    r"\$([0-9.]+), (\d+) tools, stop=(.+?)\s*$", re.MULTILINE)
PASSED_RE = re.compile(r"^([01])/1 passed, (\d+)ms total", re.MULTILINE)
BINARY_RE = re.compile(r"\[kernel\] copying local binary: (\S+)")
OSTK_VERSION_RE = re.compile(r"ostk=ostk ([\w.\-]+)")
RESULTS_RE = re.compile(r"^DONE\|(?P<model>[^|]+)\|(?P<bench>[^|]+)\|resolved=(?P<resolved>true|false)")

# Benches deliberately retired by the operator (working-tree deletions +
# PUBLISHED_EXCLUDE + benchmarks/ moved to _retired/). Never resurrected.
RETIRED_BENCHES = {"haystack-boot", "haystack-mint"}

# June run acceptance window for recovered payload timestamps.
JUNE_DATES = ("2026-06-16", "2026-06-17")

# The single archive dir that is explicitly SAFE (the good June-16 gemini
# data). All other runs-archive/ sets (contaminated / openrouter-invalid /
# legacy / retired) are out of bounds and never read.
SAFE_GEMINI_ARCHIVE = (
    ROOT / "runs-archive" / "gemini-3.1-pro-preview-kernel-cpu.pre-rerun-20260702-181539")

SOURCES = [
    # (model, log_dir, run_dir_name)
    ("claude-sonnet-4-6", "rerun-opus-sonnet-kernel-logs", "claude-sonnet-4-6-kernel-cpu"),
    ("claude-opus-4-8", "rerun-opus-sonnet-kernel-logs", "claude-opus-4-8-kernel-cpu"),
    ("gemini-3.1-pro-preview", "rerun-gemini-kernel-logs", "gemini-3.1-pro-preview-kernel-cpu"),
    ("gemini-3.5-flash", "run-flash-both-logs", "gemini-3.5-flash-kernel-cpu"),
    ("gpt-5.5", "run-gpt-kernel-logs", "gpt-5.5-kernel-cpu"),
]

RECOVERED_SUFFIX = ".recovered-june16.score.json"


def parse_log(log_path: Path) -> dict | None:
    """Extract the per-cell receipts from an ostk bench stdout log.

    Returns None when the log has no `[kernel] complete:` line at all (no
    per-benchmark payload evidence — such a cell is never emitted)."""
    text = log_path.read_text(errors="replace")
    completes = COMPLETE_RE.findall(text)
    if not completes:
        return None
    turns, tin, tout, cost, tools, stop = completes[-1]
    passed = PASSED_RE.findall(text)
    binary = BINARY_RE.search(text)
    return {
        "turns": int(turns),
        "input_tokens": int(tin),
        "output_tokens": int(tout),
        "cost_str": cost,
        "tool_uses": int(tools),
        "stop_reason": stop.strip(),
        "passed": (passed[-1][0] == "1") if passed else None,
        "wall_ms": int(passed[-1][1]) if passed else None,
        "teardown_masked": WATCHDOG_MARKER in text,
        "binary_path": binary.group(1) if binary else None,
    }


def parse_results_txt(log_dir: Path, model: str) -> dict[str, bool]:
    """resolved= receipts from _results.txt (may be truncated — gpt-5.5's tee
    died at 15 lines; missing benches fall back to the log's passed line)."""
    out: dict[str, bool] = {}
    fp = log_dir / "_results.txt"
    if not fp.exists():
        return out
    for line in fp.read_text().splitlines():
        m = RESULTS_RE.match(line)
        if m and m.group("model") == model:
            out[m.group("bench")] = m.group("resolved") == "true"
    return out


def receipts_match(payload: dict, log: dict, model: str, bench: str) -> tuple[bool, str]:
    """Field-by-field join of a score payload to the log's complete-line.
    All receipts must agree — this is what makes emission non-fabricable."""
    checks = [
        ("benchmark", payload.get("benchmark"), bench),
        ("agent", payload.get("agent"), f"{model}-kernel-cpu"),
        ("turns", payload.get("turns_to_fix"), log["turns"]),
        ("input_tokens", payload.get("input_tokens"), log["input_tokens"]),
        ("output_tokens", payload.get("output_tokens"), log["output_tokens"]),
        ("cost", f"{payload.get('estimated_cost_usd') or 0:.4f}", log["cost_str"]),
        ("tool_uses", payload.get("tool_uses"), log["tool_uses"]),
        ("stop_reason", payload.get("stop_reason"), log["stop_reason"]),
    ]
    if log["passed"] is not None:
        checks.append(("resolved", bool(payload.get("resolved")), log["passed"]))
    for name, got, want in checks:
        if got != want:
            return False, f"{name}: payload={got!r} log={want!r}"
    return True, ""


def git_head_payload(run_dir_name: str, bench: str) -> dict | None:
    """The June payload as committed at git HEAD (survives the on-disk rm)."""
    ref = f"HEAD:runs/{run_dir_name}/{bench}.score.json"
    proc = subprocess.run(["git", "show", ref], cwd=ROOT,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def ingest_model(model: str, log_dir_name: str, run_dir_name: str,
                 dry_run: bool) -> dict:
    log_dir = ROOT / log_dir_name
    run_dir = RUNS / run_dir_name
    results = parse_results_txt(log_dir, model)
    ostk_version = None
    nohup = log_dir / "_nohup.log"
    if nohup.exists():
        m = OSTK_VERSION_RE.search(nohup.read_text(errors="replace"))
        if m:
            ostk_version = m.group(1)

    report = {"model": model, "cells": {}, "counts": {}}
    counts = report["counts"]

    def tally(state: str, bench: str, detail: str = ""):
        counts[state] = counts.get(state, 0) + 1
        report["cells"][bench] = {"state": state, **({"detail": detail} if detail else {})}

    prefix = model + "-"
    for log_path in sorted(log_dir.glob(f"{prefix}*.log")):
        bench = log_path.name[len(prefix):-len(".log")]
        log = parse_log(log_path)
        if log is None:
            tally("skipped_ambiguous", bench, "no complete-line payload in log")
            continue
        # _results.txt receipt beats the log passed-line when both exist and
        # agree; a disagreement is ambiguity (fail closed).
        if bench in results and log["passed"] is not None and results[bench] != log["passed"]:
            tally("skipped_ambiguous", bench,
                  f"_results.txt resolved={results[bench]} vs log passed={log['passed']}")
            continue

        wt_path = run_dir / f"{bench}.score.json"
        if wt_path.exists():
            try:
                wt = json.loads(wt_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                tally("skipped_ambiguous", bench, f"unreadable working-tree score: {e}")
                continue
            ok, _why = receipts_match(wt, log, model, bench)
            if ok:
                tally("already_ingested", bench)
                continue
            # Working-tree score is a DIFFERENT run (July-2 rerun) → recover
            # the June payload from the git HEAD blob. Never overwrite.
        elif bench in RETIRED_BENCHES:
            tally("skipped_retired_bench", bench,
                  "deliberately deleted from working tree; PUBLISHED_EXCLUDE")
            continue

        recovered = git_head_payload(run_dir_name, bench)
        if recovered is None:
            tally("skipped_ambiguous", bench, "no recoverable payload (git HEAD miss)")
            continue
        if bench in RETIRED_BENCHES:
            tally("skipped_retired_bench", bench,
                  "deliberately deleted from working tree; PUBLISHED_EXCLUDE")
            continue
        ok, why = receipts_match(recovered, log, model, bench)
        if not ok:
            tally("skipped_ambiguous", bench, f"HEAD payload fails log receipts — {why}")
            continue
        ts = str(recovered.get("timestamp", ""))
        if not ts.startswith(JUNE_DATES):
            tally("skipped_ambiguous", bench, f"HEAD payload timestamp {ts!r} outside June run window")
            continue

        recovered_from = f"git:HEAD:runs/{run_dir_name}/{bench}.score.json"
        cross_check = None
        if model == "gemini-3.1-pro-preview":
            arch_fp = SAFE_GEMINI_ARCHIVE / f"{bench}.score.json"
            if arch_fp.exists():
                try:
                    arch = json.loads(arch_fp.read_text())
                except (json.JSONDecodeError, OSError) as e:
                    tally("skipped_ambiguous", bench, f"safe-archive copy unreadable: {e}")
                    continue
                if arch != recovered:
                    tally("skipped_ambiguous", bench, "git HEAD blob != safe-archive copy")
                    continue
                cross_check = str(arch_fp.relative_to(ROOT)) + " (identical)"

        out = dict(recovered)
        out["teardown_masked"] = log["teardown_masked"]
        out["ingested_by"] = "ingest_rerun_logs"
        out["source_log"] = str(log_path.relative_to(ROOT))
        out["recovered_from"] = recovered_from
        if cross_check:
            out["recovery_cross_check"] = cross_check
        out["binary_identity"] = {
            "path": log["binary_path"],
            "ostk_version": ostk_version,
            "status": "unverified-path-only",
            "note": ("mutable build path recorded in run log; frozen-bin/ "
                     "hashes plausible for June runs but not provable"),
        }

        out_path = run_dir / f"{bench}{RECOVERED_SUFFIX}"
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
            except (json.JSONDecodeError, OSError):
                existing = None
            if existing == out:
                tally("already_emitted", bench)
                continue
        if dry_run:
            tally("would_emit", bench, str(out_path.relative_to(ROOT)))
            continue
        out_path.write_text(json.dumps(out, indent=2) + "\n")
        tally("emitted", bench, str(out_path.relative_to(ROOT)))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover stranded June kernel scores from the rerun log dirs.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the plan without writing any file")
    args = parser.parse_args()

    grand: dict[str, int] = {}
    ambiguous = []
    for model, log_dir_name, run_dir_name in SOURCES:
        rep = ingest_model(model, log_dir_name, run_dir_name, args.dry_run)
        counts = ", ".join(f"{k}={v}" for k, v in sorted(rep["counts"].items()))
        print(f"{model}: {counts}")
        for state in ("emitted", "would_emit"):
            for bench, cell in sorted(rep["cells"].items()):
                if cell["state"] == state:
                    print(f"  {state}: {cell['detail']}")
        for bench, cell in sorted(rep["cells"].items()):
            if cell["state"] == "skipped_ambiguous":
                ambiguous.append((model, bench, cell.get("detail", "")))
        for k, v in rep["counts"].items():
            grand[k] = grand.get(k, 0) + v

    print("\nTOTALS:", ", ".join(f"{k}={v}" for k, v in sorted(grand.items())))
    if ambiguous:
        print("\nAMBIGUOUS (skipped, fail-closed — investigate before trusting these cells):")
        for model, bench, detail in ambiguous:
            print(f"  {model}/{bench}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
