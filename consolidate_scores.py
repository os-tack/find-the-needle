#!/usr/bin/env python3
"""
consolidate_scores.py — Aggregate individual run score files into the public leaderboard.

Walks runs/ to find all *.score.json and */score.json files, deduplicates,
picks the best run per (model, benchmark), and writes public/scores.json.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path(__file__).parent / "runs"
DEFAULT_OUTPUT = Path(__file__).parent / "public" / "scores.json"

EXPECTED_FIELDS = {
    "benchmark", "agent", "timestamp", "resolved",
    "turns_to_discovery", "turns_to_fix", "signal_to_noise",
    "false_positives", "token_cost", "tokens_per_correct_line",
    "recovery_events", "recovery_rate", "wall_clock", "blind_discovery",
}


def normalize_agent_for_dedup(raw: str) -> str:
    """Normalize an agent name for dedup matching.

    Mirrors the leaderboard's normalizeForMatch():
      1. Strip vendor prefix (everything before the first '/').
      2. Lowercase.
      3. Replace underscores and dots with hyphens.

    This ensures 'anthropic/claude-opus-4.6' and 'claude-opus-4-6'
    collapse to the same key.
    """
    name = raw.split("/", 1)[1] if "/" in raw else raw
    return re.sub(r"[_.]", "-", name.lower())


def parse_timestamp(ts: str) -> datetime:
    """Parse ISO 8601 timestamp, tolerant of missing timezone."""
    ts = ts.rstrip("Z")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.min


def normalize_model_name(file_path: Path) -> str:
    """
    Extract model identifier from the file path.

    Handles both layouts:
      runs/<model>/<benchmark>.score.json         -> model
      runs/<model>/<benchmark>/score.json          -> model
      runs/<vendor>/<model>/<benchmark>.score.json -> vendor/model
      runs/<vendor>/<model>/<benchmark>/score.json -> vendor/model

    We trust the 'agent' field inside the JSON as the canonical model name.
    This function is only used as a fallback.
    """
    rel = file_path.relative_to(RUNS_DIR)
    parts = rel.parts

    # Pattern: <model>/<benchmark>.score.json  (2 parts)
    # Pattern: <model>/<benchmark>/score.json  (3 parts, last is score.json)
    # Pattern: <vendor>/<model>/<benchmark>.score.json  (3 parts, last ends .score.json)
    # Pattern: <vendor>/<model>/<benchmark>/score.json  (4 parts)

    if len(parts) == 2:
        return parts[0]
    elif len(parts) == 3:
        if parts[-1] == "score.json":
            return parts[0]
        else:
            return f"{parts[0]}/{parts[1]}"
    elif len(parts) == 4:
        return f"{parts[0]}/{parts[1]}"
    else:
        return parts[0]


def extract_benchmark(file_path: Path) -> str:
    """
    Extract benchmark name from file path as fallback.

    runs/<model>/<benchmark>.score.json -> benchmark
    runs/<model>/<benchmark>/score.json -> benchmark
    """
    if file_path.name == "score.json":
        return file_path.parent.name
    else:
        return file_path.stem.replace(".score", "")


def find_score_files() -> list[Path]:
    """Find all score JSON files in the runs directory."""
    files = []
    for root, _dirs, filenames in os.walk(RUNS_DIR):
        root_path = Path(root)
        for fname in filenames:
            fpath = root_path / fname
            # Match *.score.json or score.json inside a benchmark subdir
            if fname.endswith(".score.json") or (
                fname == "score.json" and root_path.parent != RUNS_DIR
            ):
                files.append(fpath)
    return files


def load_score(file_path: Path) -> dict | None:
    """Load and validate a score JSON file. Returns None on failure."""
    try:
        with open(file_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: skipping {file_path} — {e}", file=sys.stderr)
        return None

    if not isinstance(data, dict):
        print(f"  WARNING: skipping {file_path} — not a JSON object", file=sys.stderr)
        return None

    # Must have at least benchmark and agent
    if "benchmark" not in data:
        data["benchmark"] = extract_benchmark(file_path)
    if "agent" not in data:
        data["agent"] = normalize_model_name(file_path)

    return data


def score_key(entry: dict) -> tuple[str, str]:
    """Return (normalized_model, benchmark) dedup key.

    Uses normalize_agent_for_dedup so that 'anthropic/claude-opus-4.6'
    and 'claude-opus-4-6' map to the same key.
    """
    return (normalize_agent_for_dedup(entry["agent"]), entry["benchmark"])


def is_better(candidate: dict, current: dict) -> bool:
    """
    Return True if candidate should replace current.

    Preference order:
      1. resolved=True beats resolved=False
      2. Lower token_cost wins among same resolved status
    """
    c_resolved = bool(candidate.get("resolved", False))
    e_resolved = bool(current.get("resolved", False))

    if c_resolved and not e_resolved:
        return True
    if not c_resolved and e_resolved:
        return False

    # Same resolved status — prefer lower token_cost
    c_cost = candidate.get("token_cost", float("inf"))
    e_cost = current.get("token_cost", float("inf"))
    return c_cost < e_cost


def deduplicate_locations(files: list[Path]) -> list[Path]:
    """
    When both <benchmark>.score.json and <benchmark>/score.json exist
    for the same model+benchmark, keep the more recently modified one.

    Also collapses vendor-prefixed and flat paths for the same logical
    model (e.g. runs/anthropic/claude-opus-4.6/ vs runs/claude-opus-4-6/).
    """
    # Group by (normalized_model, benchmark)
    groups: dict[tuple[str, str], list[Path]] = {}
    for fpath in files:
        bench = extract_benchmark(fpath)
        model = normalize_model_name(fpath)
        key = (normalize_agent_for_dedup(model), bench)
        groups.setdefault(key, []).append(fpath)

    result = []
    for key, paths in groups.items():
        if len(paths) == 1:
            result.append(paths[0])
        else:
            # Pick the most recently modified
            best = max(paths, key=lambda p: p.stat().st_mtime)
            result.append(best)
    return result


def consolidate(dry_run: bool = False, output: Path = DEFAULT_OUTPUT) -> None:
    """Main consolidation logic."""
    if not RUNS_DIR.exists():
        print(f"ERROR: runs directory not found at {RUNS_DIR}", file=sys.stderr)
        sys.exit(1)

    # Step 1: find all score files
    all_files = find_score_files()
    print(f"Found {len(all_files)} score files in {RUNS_DIR}")

    # Step 2: deduplicate file locations
    deduped_files = deduplicate_locations(all_files)
    print(f"After location dedup: {len(deduped_files)} files")

    # Step 3: load and validate
    entries = []
    skipped = 0
    for fpath in deduped_files:
        entry = load_score(fpath)
        if entry is None:
            skipped += 1
            continue
        entries.append(entry)

    if skipped:
        print(f"Skipped {skipped} malformed files")

    # Step 4: keep best per (model, benchmark)
    best: dict[tuple[str, str], dict] = {}
    for entry in entries:
        key = score_key(entry)
        if key not in best or is_better(entry, best[key]):
            best[key] = entry

    # Step 5: sort by model name then benchmark name
    consolidated = sorted(best.values(), key=lambda e: (e["agent"], e["benchmark"]))

    # Step 6: summary
    models = sorted(set(normalize_agent_for_dedup(e["agent"]) for e in consolidated))
    benchmarks = sorted(set(e["benchmark"] for e in consolidated))
    resolved_count = sum(1 for e in consolidated if e.get("resolved"))

    print(f"\n--- Summary ---")
    print(f"Total entries:  {len(consolidated)}")
    print(f"Models ({len(models)}):     {', '.join(models)}")
    print(f"Benchmarks ({len(benchmarks)}): {', '.join(benchmarks)}")
    print(f"Resolved:       {resolved_count}/{len(consolidated)}")

    # Step 7: write or dry-run
    if dry_run:
        print(f"\n[dry-run] Would write {len(consolidated)} entries to {output}")
        if output.exists():
            existing = json.load(open(output))
            print(f"[dry-run] Current file has {len(existing)} entries")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)

        before_count = 0
        if output.exists():
            try:
                before_count = len(json.load(open(output)))
            except Exception:
                pass

        with open(output, "w") as f:
            json.dump(consolidated, f, indent=2)
            f.write("\n")

        print(f"\nWrote {len(consolidated)} entries to {output}")
        if before_count:
            print(f"Before: {before_count} entries → After: {len(consolidated)} entries")


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate needle-bench run scores into the public leaderboard."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    consolidate(dry_run=args.dry_run, output=args.output)


if __name__ == "__main__":
    main()
