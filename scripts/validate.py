#!/usr/bin/env python3
"""
validate.py — standalone fail-closed checker for a runs/ subtree.

Wraps cell_validity.classify_cell (the SAME module the consolidator uses — no
re-implementation) over every *.score.json under the given root and prints a
VALID / INVALID / MASKED table per model-arm, plus every INVALID cell with its
reason codes. Exits nonzero if any publishable cell is INVALID.

This is the arm-drop insurance at the bench layer: it catches kernel-arm
silent failures regardless of kernel behavior —
  * a score whose payload carries no executed-arm receipt, or whose receipt
    names a different arm than the directory it sits in (the historical
    haystack bench.rs `arm = None` bug wrote scores into a BARE model dir
    with no arm suffix — such directories are flagged as
    unrecognized_arm_directory and fail the run);
  * completed runs with zero token accounting (never a silent $0 cell);
  * deadline / zero-work phantoms;
  * one-execution-two-columns duplicates (native_driver vs generic_kernel).

Benches in consolidate_scores.PUBLISHED_EXCLUDE are classified and shown but
never affect the exit code (they are unpublishable by policy either way).

Usage:
    scripts/validate.py [runs-root] [--model MODEL ...] [--no-details]

Exit codes: 0 = no publishable INVALID cells; 1 = INVALID cells present;
2 = usage / root missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cell_validity
from consolidate_scores import (
    ARM_PATTERN,
    PUBLISHED_EXCLUDE,
    duplicate_column_reason,
    kernel_arm_type_for,
    load_teardown_sidecar,
    normalize_model,
)

REASON_UNRECOGNIZED_DIR = "unrecognized_arm_directory"


def discover(root: Path, models: list[str]) -> tuple[list, list]:
    """Return (arm_dirs, orphan_dirs).
    arm_dirs:    [(model, arm, path)] for <model>-<arm> directories.
    orphan_dirs: [(dirname, path)] for directories that hold score files but
                 carry NO arm suffix — the arm-drop signature."""
    arm_dirs, orphans = [], []
    # Allow pointing directly at one <model>-<arm> dir (or one orphan dir).
    m = ARM_PATTERN.match(root.name)
    if any(root.glob("*.score.json")):
        if m:
            return [(m.group(1), m.group(2), root)], []
        return [], [(root.name, root)]
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        m = ARM_PATTERN.match(entry.name)
        if m:
            model, arm = m.group(1), m.group(2)
            if models and model not in models:
                continue
            arm_dirs.append((model, arm, entry))
        else:
            if models and entry.name not in models:
                continue
            if any(entry.glob("*.score.json")):
                orphans.append((entry.name, entry))
    return arm_dirs, orphans


def main() -> int:
    ap = argparse.ArgumentParser(description="fail-closed validity check over a runs/ subtree")
    ap.add_argument("root", nargs="?", default=str(ROOT / "runs"),
                    help="runs/ subtree to check (default: repo runs/)")
    ap.add_argument("--model", action="append", default=[],
                    help="restrict to model(s), exact on-disk spelling; repeatable")
    ap.add_argument("--no-details", action="store_true",
                    help="suppress the per-cell INVALID listing")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 2

    arm_dirs, orphan_dirs = discover(root, args.model)
    arms_by_model: dict[str, set] = defaultdict(set)
    for model, arm, _ in arm_dirs:
        arms_by_model[normalize_model(model)].add(arm)

    # tallies[(label, arm)] = {"valid": n, "invalid": n, "masked": n, "excluded": n}
    tallies: dict[tuple[str, str], Counter] = defaultdict(Counter)
    invalid_cells: list[dict] = []   # publishable INVALID (drives exit code)
    excluded_invalid: list[dict] = []  # INVALID but policy-excluded (shown only)
    reason_hist: Counter = Counter()

    def classify_file(fpath: Path, model: str, arm: str | None,
                      dup_reason: str | None, masked_set: frozenset) -> None:
        bench = fpath.name.removesuffix(".score.json")
        try:
            entry = json.loads(fpath.read_text())
        except (json.JSONDecodeError, OSError):
            entry = None
        if isinstance(entry, dict) and entry.get("benchmark"):
            bench = entry["benchmark"]
        masked = bool(
            isinstance(entry, dict)
            and (entry.get("teardown_masked") or bench in masked_set)
        )
        excluded = bench in PUBLISHED_EXCLUDE

        if arm is None:
            # Orphan dir: no arm suffix — the arm=None signature. Fail closed.
            reasons = [REASON_UNRECOGNIZED_DIR]
            cls = cell_validity.classify_cell(entry, model=model)
            reasons += cls.reasons
        else:
            cls = cell_validity.classify_cell(entry, requested_arm=arm, model=model)
            reasons = list(cls.reasons)
            if dup_reason:
                reasons.append(dup_reason)

        key = (model, arm or "?")
        if masked:
            tallies[key]["masked"] += 1
        if excluded:
            tallies[key]["excluded"] += 1
        if reasons:
            tallies[key]["invalid"] += 1
            cell = {"model": model, "arm": arm or "?", "bench": bench,
                    "file": str(fpath), "reasons": reasons, "excluded": excluded}
            (excluded_invalid if excluded else invalid_cells).append(cell)
            for r in reasons:
                reason_hist[r.split(":", 1)[0]] += 1
        else:
            tallies[key]["valid"] += 1

    for model, arm, dpath in arm_dirs:
        norm = normalize_model(model)
        dup = duplicate_column_reason(arm, kernel_arm_type_for(norm), arms_by_model[norm])
        masked_set = load_teardown_sidecar(dpath)
        for fpath in sorted(dpath.glob("*.score.json")):
            classify_file(fpath, model, arm, dup, masked_set)

    for name, dpath in orphan_dirs:
        masked_set = load_teardown_sidecar(dpath)
        for fpath in sorted(dpath.glob("*.score.json")):
            classify_file(fpath, name, None, None, masked_set)

    if not tallies:
        print(f"no score files found under {root}"
              + (f" for model(s) {args.model}" if args.model else ""))
        return 0

    # ---- table ----
    width = max(len(f"{m}-{a}") for m, a in tallies)
    width = max(width, len("model-arm"))
    print(f"{'model-arm':<{width}}  {'VALID':>6} {'INVALID':>8} {'MASKED':>7} {'EXCLUDED':>9}")
    print("-" * (width + 36))
    tv = ti = tm = te = 0
    for (model, arm), c in sorted(tallies.items()):
        label = f"{model}-{arm}"
        flag = "  <-- NO ARM SUFFIX (arm-drop?)" if arm == "?" else ""
        print(f"{label:<{width}}  {c['valid']:>6} {c['invalid']:>8} "
              f"{c['masked']:>7} {c['excluded']:>9}{flag}")
        tv += c["valid"]; ti += c["invalid"]; tm += c["masked"]; te += c["excluded"]
    print("-" * (width + 36))
    print(f"{'TOTAL':<{width}}  {tv:>6} {ti:>8} {tm:>7} {te:>9}")

    if reason_hist:
        print("\ninvalid reasons:")
        for reason, n in sorted(reason_hist.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4}  {reason}")

    if not args.no_details and (invalid_cells or excluded_invalid):
        print("\nINVALID cells (excluded from any published aggregate):")
        for cell in invalid_cells + excluded_invalid:
            suffix = "  [policy-excluded bench]" if cell["excluded"] else ""
            print(f"  {cell['model']}/{cell['arm']}/{cell['bench']}: "
                  f"{', '.join(cell['reasons'])}{suffix}")

    if invalid_cells:
        print(f"\nFAIL CLOSED: {len(invalid_cells)} publishable INVALID cell(s) "
              f"present — see reasons above. (absent files are NOT failures; "
              f"these are score files that cannot be trusted.)")
        return 1
    if excluded_invalid:
        print(f"\nOK (with {len(excluded_invalid)} INVALID cell(s) confined to "
              f"policy-excluded benches — never published, not fatal).")
    else:
        print("\nOK: every score file present is VALID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
