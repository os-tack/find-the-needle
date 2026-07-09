#!/usr/bin/env python3
"""backfill_boards.py — publish historical boards from git history, AS-WERE.

READ-ONLY over git (git show only; never checkout/reset). Each backfilled
board is wrapped, not rewritten:

  {"schema_era": ..., "era_note": ..., "source_commit": ..., "source_path": ...,
   "validity": "pre-validity-era — published as-were; NOT re-judged",
   "faults": [ids → boards/FAULTS.json], "summary": {mechanical extraction of
   the artifact's OWN recorded aggregates}, "data": <artifact exactly as-was>}

DOCTRINE: historical numbers publish as-were, clearly era-tagged, and are
NEVER re-judged by today's axis-split validity rules — the wrapper says so and
attaches the era faults instead. The summary block reads only what the
artifact itself recorded (no re-scoring, no re-pricing).

Idempotent + append-only: an existing identical file is kept; a differing one
is refused (these are historical records — they must not drift).

Usage:  python3 backfill_boards.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
BOARDS_DIR = ROOT / "boards"

PRE_VALIDITY = ("pre-validity-era — published as-were; NOT re-judged by "
                "axis-split validity rules")

# (era_dir, run_date, commit, source_path, schema_era, faults, era_note)
BACKFILLS = [
    ("pre-v2", "2026-03-30", "69a8eea4", "public/experiment-scores.json",
     "v1-bare-silent-flat",
     ["pre-validity-era", "pre-073d9154-cache-misreporting"],
     "First published board: 26 models on the bare/silent two-arm design "
     "(pre-kernel-arm naming), flat per-cell list, 1664 runs. Commit "
     "69a8eea4 'bench: final results — 26 models, 1664 runs, +33.6pp mean'."),
    ("bench-v2", "2026-04-03", "67c8e588", "public/experiment-scores.json",
     "bench-v2-three-arm",
     ["pre-validity-era", "pre-073d9154-cache-misreporting", "arm-none-era"],
     "bench-v2 final: 31 models, native/kernel/cpu three-arm aggregates, "
     "3,641 scores. Commit 67c8e588 'bench-v2 final: 3,641 scores, fixes, "
     "native backfill'."),
    ("v4.6.1", "2026-04-30", "c7fee7c1", "public/experiment-scores.json",
     "bench-v2-three-arm",
     ["pre-validity-era", "pre-073d9154-cache-misreporting"],
     "v4.6.1 sweep: 9 models, 313 cells, scoped to a single-version run. "
     "Commit c7fee7c1 'scores: publish v4.6.1 sweep'."),
    # v7.6.0 June era AS-WAS: what the astro page actually served June 17 -
    # July 8 (the pre-fix harden/emit pipeline). bb4aa6d5 (2026-06-15) is the
    # run-publish commit; its experiment-scores.json was a STALE v4.6.1
    # carry-over (byte-identical to c7fee7c1's), so the honest June v7.6.0
    # artifacts are 27228dfe's board-v760.json + cells-v760.json. The same
    # June cells re-judged by TODAY's rules live in
    # boards/v7.6.0/2026-06-16-experiment-scores.json (publish_boards.py).
    ("v7.6.0", "2026-06-17", "27228dfe", "public/board-v760.json",
     "harden-board-v760",
     ["pre-validity-era", "june16-teardown-masked", "arm-none-era"],
     "June-17 leaderboard as served (consolidate_harden.py emitter, retired). "
     "Run published at bb4aa6d5 (2026-06-15), board refreshed at 27228dfe "
     "'gpt-5.5 cache fix (0%→43%) + gemini-3.5-flash'."),
    ("v7.6.0", "2026-06-17", "27228dfe", "public/cells-v760.json",
     "harden-cells-v760",
     ["pre-validity-era", "june16-teardown-masked", "arm-none-era"],
     "June-17 per-cell heatmap as served (emit_cells.py emitter, retired)."),
]


def git_show(commit: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
        check=True, capture_output=True, text=True).stdout


def summarize(data, schema_era: str) -> dict:
    """Mechanical extraction of the artifact's OWN recorded aggregates —
    reads what the era wrote, computes nothing new beyond counting/summing
    the era's own fields. Never re-judges, never re-prices."""
    models: dict[str, dict] = {}
    if schema_era == "v1-bare-silent-flat":
        for e in data:
            row = models.setdefault(e["model"], {
                "bare": {"solved": 0, "total": 0, "cost_usd": 0.0},
                "silent": {"solved": 0, "total": 0, "cost_usd": 0.0}})
            for arm in ("bare", "silent"):
                a = e.get(arm)
                if a is None:
                    continue
                row[arm]["total"] += 1
                row[arm]["solved"] += 1 if a.get("resolved") else 0
                row[arm]["cost_usd"] += a.get("estimated_cost_usd", 0) or 0
        for row in models.values():
            for arm in row.values():
                arm["cost_usd"] = round(arm["cost_usd"], 4)
    elif schema_era == "bench-v2-three-arm":
        for m in data.get("models", []):
            row = {}
            for arm in ("native", "kernel", "cpu"):
                a = m.get(arm) or {}
                if not a.get("total"):
                    continue
                row[arm] = {"solved": a.get("solved", 0), "total": a.get("total", 0),
                            "cost_usd": round(a.get("cost", 0) or 0, 4)}
            if row:
                models[m["model"]] = row
    elif schema_era in ("harden-board-v760", "harden-cells-v760"):
        for m in data.get("models", []):
            models[m["model"]] = {
                "native": {"solved": m.get("native_solved"),
                           "total": data.get("published_tasks")},
                "kernel": {"solved": m.get("kernel_solved"),
                           "total": data.get("published_tasks")},
            }
    return {
        "models": models,
        "note": "aggregates read from the artifact's own recorded fields — "
                "not re-judged, not re-priced",
        "invalid_counts": "unavailable (pre-validity era — validity was not "
                          "judged when this board was published)",
        "masked_counts": "unavailable (teardown masking annotation postdates "
                         "this era)" if schema_era != "harden-board-v760" else
                         "not recorded by the harden emitter",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill historical boards from git (read-only).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for era_dir, run_date, commit, src_path, schema_era, faults, note in BACKFILLS:
        raw = git_show(commit, src_path)
        data = json.loads(raw)
        basename = Path(src_path).name.removesuffix(".json")
        out_path = BOARDS_DIR / era_dir / f"{run_date}-{basename}.json"
        wrapper = {
            "schema_era": schema_era,
            "era_note": note,
            "source_commit": commit,
            "source_path": src_path,
            "captured_from_git": True,
            "validity": PRE_VALIDITY,
            "faults": faults,
            "summary": summarize(data, schema_era),
            "data": data,
        }
        if args.dry_run:
            nm = len(wrapper["summary"]["models"])
            print(f"[dry-run] {out_path.relative_to(ROOT)} ← {commit}:{src_path} "
                  f"({nm} models, {len(raw):,} bytes)")
            continue
        if out_path.exists():
            existing = json.loads(out_path.read_text())
            if existing == wrapper:
                print(f"[backfill] {out_path.relative_to(ROOT)} unchanged — kept")
                continue
            sys.exit(f"REFUSED: {out_path} exists with different content — "
                     f"historical records are append-only; resolve manually.")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(wrapper, f, indent=1)
            f.write("\n")
        print(f"[backfill] wrote {out_path.relative_to(ROOT)} "
              f"({len(wrapper['summary']['models'])} models, era={schema_era})")


if __name__ == "__main__":
    main()
