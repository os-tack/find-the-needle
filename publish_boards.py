#!/usr/bin/env python3
"""publish_boards.py — versioned, fail-closed board publishing (one command).

SINGLE SOURCE OF TRUTH: every number here comes from ONE classification pass —
consolidate_scores.collect_cells() + build_experiment_output() (axis-split
validity, cell_validity.py). This script only selects, shapes, and writes:

  public/scores.json                — flat per-cell list, LATEST board
  public/experiment-scores.json     — LATEST board (single snapshot per column)
  public/board-v760.json            — leaderboard adapter (index.astro:351)
  public/cells-v760.json            — per-cell heatmap adapter (index.astro:447)
  boards/<ostk-version>/<run-date>-experiment-scores.json — APPEND-ONLY
                                      versioned snapshot boards
  boards/index.json                 — version → runs → per-model solve/cost
                                      aggregates + invalid/masked counts +
                                      faults; comparable:false across versions

The old sibling emitters (consolidate_harden.py, emit_cells.py) diverged
doctrinally from the fail-closed pipeline (own load rules, own rate cards, no
validity judgment) and are retired: they refuse to run and point here.

Append-only: a versioned board file that already exists with different content
is NEVER silently overwritten — the script fails loudly (--force to replace
deliberately, e.g. after a doctrine change; document why in the commit).

No cross-version aggregate exists anywhere: boards/index.json carries
cross_version.comparable=false and no code path sums across versions.

Usage:  python3 publish_boards.py [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import sys
from pathlib import Path

import cell_validity
import consolidate_scores as cs

ROOT = Path(__file__).parent
BOARDS_DIR = ROOT / "boards"
BOARD_OUTPUT = ROOT / "public" / "board-v760.json"
CELLS_OUTPUT = ROOT / "public" / "cells-v760.json"

TODAY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Append-only versioned writes
# ---------------------------------------------------------------------------

def _stable(payload: dict) -> str:
    """Board content minus the volatile generation timestamp."""
    p = copy.deepcopy(payload)
    p.pop("generated", None)
    return json.dumps(p, sort_keys=True)


def write_versioned(path: Path, payload: dict, force: bool = False,
                    refusals: list[str] | None = None) -> bool:
    """Append-only write: identical content → keep the existing file
    (timestamp preserved); different content → refuse loudly unless --force.

    When `refusals` is passed, a different-content refusal is RECORDED there
    instead of exiting immediately, so one drifted historical board cannot
    block a brand-new snapshot from landing — the caller still fails closed
    (nonzero exit) after attempting every file. The historical file on disk
    is never touched either way."""
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"REFUSED: {path} exists but is unreadable ({e}) — "
                     f"versioned boards are append-only; resolve manually.")
        if _stable(old) == _stable(payload):
            print(f"[boards] {path.relative_to(ROOT)} unchanged — kept (append-only)")
            return False
        if not force:
            msg = (f"REFUSED: {path} exists with DIFFERENT content — versioned "
                   f"boards are append-only. Re-run with --force only for a "
                   f"deliberate, documented replacement.")
            if refusals is None:
                sys.exit(msg)
            print(f"[boards] {msg}", file=sys.stderr)
            refusals.append(str(path.relative_to(ROOT)))
            return False
        print(f"[boards] FORCED overwrite of {path.relative_to(ROOT)}", file=sys.stderr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")
    print(f"[boards] wrote {path.relative_to(ROOT)}")
    return True


# ---------------------------------------------------------------------------
# Per-cell shaping shared by the board + cells adapters
# ---------------------------------------------------------------------------

def latest_cells(cells: list[dict]) -> list[dict]:
    chosen = cs.column_snapshots(cells)
    return [c for c in cells if chosen[(c["norm"], c["arm_key"])] == c["snapshot"]]


def keeps_best_cell(new: dict, cur: dict | None) -> bool:
    """Same keep-best rule as consolidate_scores: resolved > not, fewer turns,
    then cost-valid beats cost-unavailable."""
    if cur is None:
        return True
    ne, ce = new["entry"], cur["entry"]
    nr, cr = bool(ne.get("resolved")), bool(ce.get("resolved"))
    if nr != cr:
        return nr
    nt, ct = (ne.get("turns_to_fix") or 0), (ce.get("turns_to_fix") or 0)
    if nt != ct:
        return nt < ct
    return new["cls"].cost_valid and not cur["cls"].cost_valid


def best_by_column(use: list[dict]) -> dict:
    """(norm, arm_key) → {bench: cell}. Solve-usable cells collapse via
    keep-best; a fully-invalid cell only represents a (col, bench) that has
    NO solve-usable cell (so its fault stays visible in the heatmap)."""
    best: dict[tuple, dict] = {}
    invalid_only: dict[tuple, dict] = {}
    for c in use:
        key = (c["norm"], c["arm_key"], c["benchmark"])
        if c["cls"].solve_valid:
            if key not in best or keeps_best_cell(c, best[key]):
                best[key] = c
        else:
            invalid_only.setdefault(key, c)
    out: dict[tuple, dict] = {}
    for key, c in {**invalid_only, **best}.items():  # best wins on overlap
        norm, arm_key, bench = key
        out.setdefault((norm, arm_key), {})[bench] = c
    return out


def cell_status(c: dict) -> str:
    """Heatmap status vocabulary: pass|fail|deadline|infra|invalid (+ the
    adapter emits 'missing' for absent cells)."""
    cls = c["cls"]
    if cls.solve_valid:
        return "pass" if c["entry"].get("resolved") else "fail"
    bases = {r.split(":", 1)[0] for r in cls.solve_reasons}
    if cell_validity.REASON_DEADLINE in bases:
        return "deadline"
    if bases & {cell_validity.REASON_ZERO_WORK, cell_validity.REASON_STALL}:
        return "infra"
    return "invalid"


def cell_payload(c: dict | None, model: str) -> dict:
    if c is None:
        return {"status": "missing"}
    entry = c["entry"] or {}
    g = lambda k: entry.get(k, 0) or 0
    tin, _ = cs.total_input_tokens(entry) if entry else (0, False)
    cost_valid = c["cls"].cost_valid and c["cls"].solve_valid
    cost = None
    if cost_valid:
        cost = entry.get("computed_cost_usd")
        if cost is None:
            cost = cs.compute_cost_ex(entry, model)[0]
        cost = round(cost, 4)
    return {
        "status": cell_status(c),
        "resolved": bool(entry.get("resolved")),
        "turns": g("turns_to_fix"),
        "wall": round(entry.get("wall_clock", 0) or 0, 1),
        "billed": g("billed_tokens") or tin + g("output_tokens"),
        "fresh": g("fresh_input_tokens"),
        "cache_read": g("cache_read_tokens"),
        "cache_create": g("cache_create_tokens"),
        "output": g("output_tokens"),
        "input_total": tin,
        "cost": cost,
        "cost_valid": cost_valid,
        "snapshot": c["snapshot"],
    }


def _buckets(cells_list: list[dict], model: str) -> dict:
    """Token-bucket + cost sums over COST-VALID cells only (callers pre-filter;
    asserted here — a cost-unavailable cell in a bucket sum is a doctrine bug)."""
    f = r = cc = b = o = tin_total = 0
    cost = 0.0
    for c in cells_list:
        assert c["cls"].cost_valid, "cost-invalid cell reached a bucket sum"
        e = c["entry"]
        f += e.get("fresh_input_tokens", 0) or 0
        r += e.get("cache_read_tokens", 0) or 0
        cc += e.get("cache_create_tokens", 0) or 0
        b += e.get("billed_tokens", 0) or 0
        o += e.get("output_tokens", 0) or 0
        tin_total += cs.total_input_tokens(e)[0]
        cv_cost = e.get("computed_cost_usd")
        cost += cv_cost if cv_cost is not None else cs.compute_cost_ex(e, model)[0]
    if b == 0:
        b = f + r + cc
    if b == 0:
        b = tin_total
    return {"cost": round(cost, 3), "fresh": f, "cache_read": r,
            "cache_create": cc, "billed": b, "output": o,
            "input_total": tin_total}


# ---------------------------------------------------------------------------
# board-v760.json adapter (leaderboard; schema consumed by index.astro:351)
# ---------------------------------------------------------------------------

def emit_board(latest_out: dict, by_col: dict, published: int) -> dict:
    models = []
    for agg in latest_out["models"]:
        norm = agg["model_normalized"]
        model = agg["model"]
        src = agg["ostk"]["source_arm"]
        b_label = "B" if agg["kernel_arm_type"] == "native_driver" else "B*"
        nat_cells = by_col.get((norm, "native"), {})
        b_cells = by_col.get((norm, src), {})
        nat_usable = {b: c for b, c in nat_cells.items() if c["cls"].solve_valid}
        b_usable = {b: c for b, c in b_cells.items() if c["cls"].solve_valid}
        native_present = bool(nat_usable)
        both_solved = [b for b in (set(nat_usable) & set(b_usable))
                       if nat_usable[b]["entry"].get("resolved")
                       and b_usable[b]["entry"].get("resolved")]
        # COST axis: bucket/cost sums ONLY over cost-valid cells — a native
        # column whose accounting is unavailable publishes native=null (the
        # page renders 'n/r') while its SOLVE count stands. That is the axis
        # split: solve columns include, cost columns exclude, counts visible.
        both_cost = [b for b in both_solved
                     if nat_usable[b]["cls"].cost_valid and b_usable[b]["cls"].cost_valid]
        b_solved_cost = [b for b, c in b_usable.items()
                         if c["entry"].get("resolved") and c["cls"].cost_valid]
        native = _buckets([nat_usable[b] for b in both_cost], model) if both_cost else None
        kernel = _buckets([b_usable[b] for b in both_cost], model) if both_cost else None
        cost_delta = tok_delta = None
        if native and kernel:
            nb = native.get("input_total") or native["billed"]
            kb = kernel.get("input_total") or kernel["billed"]
            if native["cost"]:
                cost_delta = round((kernel["cost"] - native["cost"]) / native["cost"] * 100, 1)
            if nb:
                tok_delta = round((kb - nb) / nb * 100, 1)
        nat_agg, ostk_agg = agg["native"], agg["ostk"]
        bases = nat_agg.get("cost_bases", {})
        native_cost_basis = (max(bases, key=bases.get) if bases else None)
        # Column snapshots: each column is single-snapshot, but a pair delta
        # may CROSS snapshots when only one arm was re-run (gemini-3.1: native
        # june16 vs cpu july02). Same-version cross-snapshot deltas are
        # published flagged — the july02 cache_read=0 fault means the cost
        # bases differ across the pair (see faults). A pair whose snapshots
        # executed under DIFFERENT binary versions (flash: native june16 =
        # v7.6.0 vs cpu july09 = v7.7.1) gets its deltas SUPPRESSED outright:
        # boards/index.json doctrine forbids publishing any cross-version
        # delta, and a both_n=1 pair across binaries is noise anyway.
        col_snaps = latest_out.get("column_snapshots", {})
        nat_snap = col_snaps.get(f"{norm}/native")
        b_snap = col_snaps.get(f"{norm}/{src}")
        cross = (nat_snap is not None and b_snap is not None and nat_snap != b_snap)
        nat_ver = cs.SNAPSHOT_OSTK_VERSION.get(nat_snap) if nat_snap else None
        b_ver = cs.SNAPSHOT_OSTK_VERSION.get(b_snap) if b_snap else None
        cross_version = cross and (nat_ver != b_ver)
        if cross_version:
            cost_delta = tok_delta = None
        if cross_version:
            delta_note = (f"SUPPRESSED: native ({nat_snap}, {nat_ver}) and ostk "
                          f"({b_snap}, {b_ver}) columns executed under different "
                          f"binary versions — no cross-version delta is "
                          f"published anywhere (boards/index.json doctrine); "
                          f"compare within one version's board instead")
        elif cross:
            delta_note = ("native and ostk columns come from different "
                          "run-date snapshots (see faults: july02 reports "
                          "cache_read=0, so pair cost bases differ)")
        else:
            delta_note = None
        models.append({
            "model": model, "type": b_label,
            "native_cli": agg.get("native_cli", "opencode"),
            "native_present": native_present,
            "published": published,
            "native_solved": nat_agg["solved"] if native_present else None,
            "native_total": nat_agg["total"],
            "kernel_solved": ostk_agg["solved"],
            "kernel_total": ostk_agg["total"],
            "both_n": len(both_solved),
            "both_cost_n": len(both_cost),
            "invalid": {"native": nat_agg["invalid"], "ostk": ostk_agg["invalid"]},
            "cost_invalid": {"native": nat_agg["cost_invalid"], "ostk": ostk_agg["cost_invalid"]},
            "masked": {"native": nat_agg["masked"], "ostk": ostk_agg["masked"]},
            "native_cost_basis": native_cost_basis,
            "native_cost_bases": bases,
            "source_arm": src,
            "snapshots": {"native": nat_snap, "ostk": b_snap},
            "cross_snapshot_delta": cross,
            "cross_version_pair": cross_version,
            "delta_note": delta_note,
            "native": native, "kernel": kernel,
            "cost_delta": cost_delta, "tok_delta": tok_delta,
            "kernel_abs": _buckets([b_usable[b] for b in b_solved_cost], model)
                          if b_solved_cost else None,
        })
    return {
        "generated": TODAY,
        "run": cs.OSTK_VERSION,
        "snapshot": latest_out["snapshot"],
        "pipeline": "publish_boards.py ← consolidate_scores.py (axis-split validity)",
        "published_tasks": published,
        "faults": latest_out["faults"],
        "faults_ref": "boards/FAULTS.json",
        "trust": latest_out["trust"],
        "models": models,
    }


# ---------------------------------------------------------------------------
# cells-v760.json adapter (heatmap; schema consumed by index.astro:447)
# ---------------------------------------------------------------------------

def tier(rate: float) -> str:
    if rate <= 0.40:
        return "ceiling"
    if rate < 0.90:
        return "discriminating"
    return "floor"


def emit_cells(latest_out: dict, by_col: dict, published: int) -> dict:
    # bench universe + solve rate across every solve-usable native/B cell
    rows = []  # (norm, model, src, label, native_cli)
    for agg in latest_out["models"]:
        rows.append((agg["model_normalized"], agg["model"],
                     agg["ostk"]["source_arm"],
                     "B" if agg["kernel_arm_type"] == "native_driver" else "B*",
                     agg.get("native_cli", "opencode")))
    all_benches: set[str] = set()
    for (norm, model, src, label, _cli) in rows:
        all_benches |= set(by_col.get((norm, "native"), {}))
        all_benches |= set(by_col.get((norm, src), {}))

    bench_solve = {}
    for b in sorted(all_benches):
        res = tot = 0
        for (norm, model, src, label, _cli) in rows:
            for col in (by_col.get((norm, "native"), {}), by_col.get((norm, src), {})):
                c = col.get(b)
                if c is None or not c["cls"].solve_valid:
                    continue
                tot += 1
                res += 1 if c["entry"].get("resolved") else 0
        bench_solve[b] = (res, tot, (res / tot if tot else 0.0))

    order = {"ceiling": 0, "discriminating": 1, "floor": 2}
    bench_order = sorted(all_benches,
                         key=lambda b: (order[tier(bench_solve[b][2])], bench_solve[b][2], b))
    benchmarks = [{"name": b, "tier": tier(bench_solve[b][2]),
                   "solve_pct": round(bench_solve[b][2] * 100),
                   "solved": bench_solve[b][0], "total": bench_solve[b][1]}
                  for b in bench_order]

    models_out = []
    for (norm, model, src, label, cli) in rows:
        nat_col = by_col.get((norm, "native"), {})
        b_col = by_col.get((norm, src), {})
        cells_map = {}
        for b in bench_order:
            cn = cell_payload(nat_col.get(b), model)
            cb = cell_payload(b_col.get(b), model)
            cells_map[b] = {"native": cn, "kernel": cb}
        models_out.append({
            "model": model, "type": label, "native_cli": cli,
            "native_solved": sum(1 for b in bench_order
                                 if cells_map[b]["native"]["status"] == "pass"),
            "kernel_solved": sum(1 for b in bench_order
                                 if cells_map[b]["kernel"]["status"] == "pass"),
            "cells": cells_map,
        })

    return {
        "generated": TODAY,
        "run": cs.OSTK_VERSION,
        "snapshot": latest_out["snapshot"],
        "pipeline": "publish_boards.py ← consolidate_scores.py (axis-split validity)",
        "published_tasks": published,
        "faults": latest_out["faults"],
        "benchmarks": benchmarks,
        "models": models_out,
    }


# ---------------------------------------------------------------------------
# boards/index.json — version → runs → aggregates; comparable:false across
# versions (NO cross-version aggregate anywhere).
# ---------------------------------------------------------------------------

def _axis_model_summary(board: dict) -> dict:
    out = {}
    for m in board.get("models", []):
        row = {}
        for col in ("native", "ostk"):
            a = m.get(col)
            if not a or not (a.get("total") or a.get("invalid") or a.get("cost_invalid")):
                continue
            row[col] = {
                "solved": a["solved"], "total": a["total"],
                "cost_usd": round(a["cost"], 4) if a.get("cost_cells") else None,
                "cost_cells": a.get("cost_cells", 0),
                "invalid": a.get("invalid", 0),
                "cost_invalid": a.get("cost_invalid", 0),
                "masked": a.get("masked", 0),
            }
            if col == "ostk":
                row[col]["source_arm"] = a.get("source_arm")
        if row:
            out[m["model"]] = row
    return out


def build_index() -> dict:
    versions: dict[str, dict] = {}
    for fp in sorted(BOARDS_DIR.glob("*/*.json")):
        version = fp.parent.name
        run_date = fp.name[:10]
        try:
            data = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"REFUSED: unreadable board file {fp} — {e}")
        v = versions.setdefault(version, {"version": version, "runs": []})
        if isinstance(data, dict) and "schema_era" in data and "data" in data:
            # backfilled as-was era artifact (backfill_boards.py wrapper)
            v.setdefault("schema_era", data["schema_era"])
            v.setdefault("judged", data.get("validity", "pre-validity-era"))
            v["runs"].append({
                "date": run_date,
                "file": str(fp.relative_to(BOARDS_DIR)),
                "judged": data.get("validity", "pre-validity-era"),
                "source_commit": data.get("source_commit"),
                "faults": data.get("faults", []),
                "models": data.get("summary", {}).get("models", {}),
                "note": data.get("era_note", ""),
            })
        elif isinstance(data, dict) and "trust" in data:
            # axis-judged snapshot board (this pipeline)
            v.setdefault("schema_era", "axis-v2")
            v.setdefault("judged", "axis-v2 (solve/cost split, fail-closed)")
            v["runs"].append({
                "date": run_date,
                "file": str(fp.relative_to(BOARDS_DIR)),
                "judged": "axis-v2 (solve/cost split, fail-closed)",
                "snapshot": data.get("snapshot"),
                "faults": [f["id"] for f in data.get("faults", [])],
                "models": _axis_model_summary(data),
                "trust": {k: data["trust"][k] for k in
                          ("solve_valid_cells", "cost_valid_cells",
                           "cost_invalid_cells", "invalid_cells",
                           "teardown_masked_cells") if k in data["trust"]},
            })
        else:
            sys.exit(f"REFUSED: {fp} is neither an as-was wrapper nor an "
                     f"axis-judged board — unknown schema, fix before indexing.")
    for v in versions.values():
        v["runs"].sort(key=lambda r: r["date"])
        v["comparable_with_other_versions"] = False
    ordered = sorted(versions.values(), key=lambda v: v["runs"][0]["date"] if v["runs"] else "")
    return {
        "generated": TODAY,
        "cross_version": {
            "comparable": False,
            "note": "ostk versions are different experimental conditions "
                    "(different kernel, drivers, benchmark oracles, schema "
                    "eras). NO cross-version delta is published anywhere, and "
                    "versioned boards never aggregate across versions. The "
                    "rolling latest view (public/experiment-scores.json) may "
                    "span versions across its single-snapshot columns for "
                    "operational continuity; it carries per-column version "
                    "attribution (ostk_versions + column_snapshots) and a "
                    "per-version trust breakdown, and its cross-version "
                    "totals are bookkeeping, not comparison. Compare runs "
                    "only within one version, and mind each run's faults "
                    "list even then.",
        },
        "faults_ref": "FAULTS.json",
        "latest": {
            "file": "../public/experiment-scores.json",
            "note": "public/experiment-scores.json is the LATEST board: "
                    "single snapshot per (model, arm) column; columns may "
                    "span binary versions (per-version attribution + trust "
                    "breakdown inside; cross-version deltas suppressed).",
        },
        "versions": ordered,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Versioned board publishing (append-only).")
    ap.add_argument("--dry-run", action="store_true", help="Build everything, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="Deliberately replace an existing versioned board (document why)")
    args = ap.parse_args()

    if not (BOARDS_DIR / "FAULTS.json").exists() and not args.dry_run:
        sys.exit("REFUSED: boards/FAULTS.json missing — fault annotations are "
                 "part of the publishing contract (create it first).")

    cells, policy_excluded, _display = cs.collect_cells()

    # One build per snapshot + the latest view; all from the same single pass.
    latest_out, latest_scores = cs.build_experiment_output(
        cells, policy_excluded, snapshot="latest")
    snapshot_outs = {}
    for snap in cs.SNAPSHOTS:
        if any(c["snapshot"] == snap for c in cells):
            snapshot_outs[snap], _ = cs.build_experiment_output(
                cells, policy_excluded, snapshot=snap, quiet=True)

    use = latest_cells(cells)
    by_col = best_by_column(use)
    published = len({c["benchmark"] for c in use})
    board = emit_board(latest_out, by_col, published)
    cells_json = emit_cells(latest_out, by_col, published)

    if args.dry_run:
        print(f"\n[dry-run] latest: {len(latest_scores)} cells, "
              f"{len(board['models'])} board rows, {published} benches; "
              f"snapshots: {sorted(snapshot_outs)}")
        return

    # public/ (latest)
    cs.SCORES_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(cs.SCORES_OUTPUT, "w") as f:
        json.dump(latest_scores, f, indent=2)
        f.write("\n")
    print(f"[public] wrote {cs.SCORES_OUTPUT.relative_to(ROOT)} ({len(latest_scores)} cells)")
    with open(cs.EXPERIMENT_OUTPUT, "w") as f:
        json.dump(latest_out, f, indent=2)
        f.write("\n")
    print(f"[public] wrote {cs.EXPERIMENT_OUTPUT.relative_to(ROOT)} (latest board)")
    with open(BOARD_OUTPUT, "w") as f:
        json.dump(board, f, indent=1)
        f.write("\n")
    print(f"[public] wrote {BOARD_OUTPUT.relative_to(ROOT)} ({len(board['models'])} models)")
    with open(CELLS_OUTPUT, "w") as f:
        json.dump(cells_json, f, indent=1)
        f.write("\n")
    print(f"[public] wrote {CELLS_OUTPUT.relative_to(ROOT)} "
          f"({len(cells_json['models'])} models x {len(cells_json['benchmarks'])} benches)")

    # boards/<version>/<run-date>-experiment-scores.json (append-only). Each
    # snapshot lands under the binary version it actually ran on — never the
    # current binary's dir (a v7.6.0 run must not move under v7.7.1). Refusals
    # are collected so a drifted historical board (e.g. an INVALID phantom
    # that the matrix legitimately deleted and re-ran) cannot block a new
    # snapshot from landing — but they still fail the run at the end.
    refusals: list[str] = []
    for snap, out in snapshot_outs.items():
        run_date = cs.SNAPSHOT_RUN_DATE[snap]
        version = cs.SNAPSHOT_OSTK_VERSION.get(snap, cs.OSTK_VERSION)
        path = BOARDS_DIR / version / f"{run_date}-experiment-scores.json"
        write_versioned(path, out, force=args.force, refusals=refusals)

    # boards/index.json (rebuilt from what is on disk — includes backfills)
    index = build_index()
    with open(BOARDS_DIR / "index.json", "w") as f:
        json.dump(index, f, indent=1)
        f.write("\n")
    print(f"[boards] wrote boards/index.json "
          f"({sum(len(v['runs']) for v in index['versions'])} runs across "
          f"{len(index['versions'])} versions)")

    if refusals:
        sys.exit(f"FAIL CLOSED: {len(refusals)} versioned board(s) drifted from "
                 f"disk and were NOT overwritten: {refusals} — the on-disk "
                 f"historical record stands; investigate the drift (or --force "
                 f"a deliberate, documented replacement).")


if __name__ == "__main__":
    main()
