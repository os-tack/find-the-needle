#!/usr/bin/env python3
"""check_boards.py — JSON sanity over boards/ + the published public artifacts.

Fail-closed: any structural violation exits non-zero with a loud, specific
message. Stdlib only.

  python3 scripts/check_boards.py

Checks:
  - every JSON file under boards/ (and the 4 public artifacts) parses
  - boards/FAULTS.json: unique ids, required fields
  - every fault id referenced by any board/run exists in FAULTS.json
  - boards/index.json: cross_version.comparable == false (NO cross-version
    aggregate anywhere), every referenced run file exists, every board file
    on disk is indexed, runs date-sorted
  - axis-judged boards: trust-block invariants (cost_valid <= solve_valid,
    per-arm solved <= total, cost_cells <= total, zero cost when no
    cost-valid cells, unavailable-cost cells never priced)
  - as-was era wrappers: schema_era + source_commit + pre-validity tag +
    non-empty data/summary (published as-were, never re-judged)
  - public/ latest board: snapshot == 'latest', column snapshots valid
  - board-v760/cells-v760 adapters: schema + internal consistency with the
    latest experiment board (same solve counts — no two-truths)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARDS = ROOT / "boards"
PUBLIC = ROOT / "public"

FAILS: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)


def load(path: Path):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        fail(f"{path.relative_to(ROOT)}: unreadable JSON — {e}")
        return None


def check_arm_agg(where: str, a: dict) -> None:
    if a.get("solved", 0) > a.get("total", 0):
        fail(f"{where}: solved {a['solved']} > total {a['total']}")
    if a.get("cost_cells", 0) > a.get("total", 0):
        fail(f"{where}: cost_cells {a['cost_cells']} > total {a['total']}")
    if a.get("masked", 0) > a.get("total", 0):
        fail(f"{where}: masked {a['masked']} > total {a['total']}")
    if a.get("cost_cells", 0) == 0 and round(a.get("cost", 0), 9) != 0:
        fail(f"{where}: cost ${a['cost']} with zero cost-valid cells "
             f"(a synthesized number leaked past the cost axis)")
    for k in ("solved", "total", "invalid", "cost_invalid", "cost_cells"):
        if a.get(k, 0) < 0:
            fail(f"{where}: negative {k}")


def check_axis_board(path: Path, data: dict) -> None:
    where = str(path.relative_to(ROOT))
    trust = data.get("trust")
    if not isinstance(trust, dict):
        fail(f"{where}: axis board without trust block")
        return
    for k in ("solve_valid_cells", "cost_valid_cells", "cost_invalid_cells",
              "invalid_cells", "invalid_by_reason", "cost_invalid_by_reason"):
        if k not in trust:
            fail(f"{where}: trust block missing {k}")
    if trust.get("cost_valid_cells", 0) > trust.get("solve_valid_cells", 0):
        fail(f"{where}: cost_valid_cells > solve_valid_cells (axis inversion)")
    if trust.get("solve_valid_cells", 0) - trust.get("cost_valid_cells", 0) \
            != trust.get("cost_invalid_cells", -1):
        fail(f"{where}: solve_valid - cost_valid != cost_invalid_cells")
    # Deliberately hardcoded (second opinion on consolidate_scores.SNAPSHOTS):
    # extend this tuple in the SAME change that adds a snapshot to the pipeline.
    if data.get("snapshot") not in ("latest", "june16", "july02", "july09",
                                    "july09v2", "july09v3", "july10",
                                    "july10v2", "july10v3", "july11"):
        fail(f"{where}: unknown snapshot {data.get('snapshot')!r}")
    for m in data.get("models", []):
        for arm in ("native", "kernel", "cpu", "ostk"):
            if isinstance(m.get(arm), dict) and "total" in m[arm]:
                check_arm_agg(f"{where}:{m['model']}/{arm}", m[arm])
    # cost axis honesty on per-bench arms: unavailable ⇒ never priced
    for b in data.get("benchmarks", []):
        for arm in ("native", "kernel", "cpu"):
            a = b.get(arm)
            if isinstance(a, dict) and not a.get("cost_valid", True):
                if a.get("cost_usd") is not None:
                    fail(f"{where}: {b['model']}/{b['benchmark']}/{arm} "
                         f"cost-invalid cell carries a priced cost_usd")
                if a.get("cost_basis") != "unavailable":
                    fail(f"{where}: {b['model']}/{b['benchmark']}/{arm} "
                         f"cost-invalid cell basis != 'unavailable'")
    return


def check_aswas(path: Path, data: dict, fault_ids: set) -> None:
    where = str(path.relative_to(ROOT))
    for k in ("schema_era", "source_commit", "validity", "data", "summary", "faults"):
        if k not in data:
            fail(f"{where}: as-was wrapper missing {k}")
    if "pre-validity" not in str(data.get("validity", "")):
        fail(f"{where}: as-was wrapper not tagged pre-validity-era")
    if not data.get("data"):
        fail(f"{where}: empty as-was payload")
    if not data.get("summary", {}).get("models"):
        fail(f"{where}: as-was summary has no models")
    for fid in data.get("faults", []):
        if fid not in fault_ids:
            fail(f"{where}: unknown fault id {fid!r}")


# Binary-identity: a published cell must live in the snapshot of the binary
# it executed on (receipt = cell.bench_binary.version, stamped by ostk bench
# on clean exits and by stall_watch on synthesized scores). Deliberately an
# INDEPENDENT duplicate of consolidate_scores' snapshot->version mapping —
# two-truths by design: drift between the maps surfaces as a failure here.
SNAPSHOT_BINARY = {"june16": "v7.6.0", "july02": "v7.6.0",
                   "july09": "v7.7.1", "july09v2": "v7.7.2",
                   "july09v3": "v7.7.3", "july10": "v7.7.4",
                   "july10v2": "v7.7.5", "july10v3": "v7.7.6",
                   "july11": "v7.7.7"}
# Snapshots sharing one run date (2026-07-09 hosted three pins, 2026-07-10
# hosts three): a receipt CONTRADICTING its snapshot fails anywhere, but a
# receipt-less cell in a multi-binary window can only be attributed by
# default — count it visibly.
RECEIPT_SPLIT_SNAPSHOTS = {"july09", "july09v2", "july09v3",
                           "july10", "july10v2", "july10v3", "july11"}


def check_binary_identity(path: Path, cells: list) -> None:
    where = path.relative_to(ROOT)
    receiptless_split = 0
    for c in cells:
        snap = c.get("snapshot")
        want = SNAPSHOT_BINARY.get(snap)
        ver = str((c.get("bench_binary") or {}).get("version") or "")
        if ver:
            if want and "v" + ver != want:
                fail(f"{where}: {c.get('agent')}/{c.get('benchmark')}: "
                     f"binary receipt {ver} filed under snapshot {snap} "
                     f"({want}) — mis-attribution")
        elif snap in RECEIPT_SPLIT_SNAPSHOTS:
            receiptless_split += 1
    if receiptless_split:
        print(f"  note: {receiptless_split} receipt-less cells in the "
              f"multi-binary 2026-07-09 window (pre-receipt scores, "
              f"attributed to the window's default binary)")


def main() -> int:
    if not BOARDS.exists():
        fail("boards/ does not exist")
        return 1

    # FAULTS.json
    faults = load(BOARDS / "FAULTS.json") or {}
    fault_list = faults.get("faults", [])
    fault_ids = {f.get("id") for f in fault_list}
    if len(fault_ids) != len(fault_list):
        fail("FAULTS.json: duplicate fault ids")
    for f in fault_list:
        for k in ("id", "title", "effect", "window", "affects"):
            if k not in f:
                fail(f"FAULTS.json: fault {f.get('id')!r} missing {k}")

    # every board file parses; classify + check
    board_files = [p for p in sorted(BOARDS.glob("*/*.json"))]
    for fp in board_files:
        data = load(fp)
        if data is None:
            continue
        if "schema_era" in data and "data" in data:
            check_aswas(fp, data, fault_ids)
        elif "trust" in data:
            check_axis_board(fp, data)
            for f in data.get("faults", []):
                if f.get("id") not in fault_ids:
                    fail(f"{fp.relative_to(ROOT)}: unknown fault id {f.get('id')!r}")
        else:
            fail(f"{fp.relative_to(ROOT)}: neither as-was wrapper nor axis board")

    # index.json
    index = load(BOARDS / "index.json")
    if index:
        if index.get("cross_version", {}).get("comparable") is not False:
            fail("index.json: cross_version.comparable MUST be false")
        indexed = set()
        for v in index.get("versions", []):
            if v.get("comparable_with_other_versions") is not False:
                fail(f"index.json: version {v.get('version')} missing "
                     f"comparable_with_other_versions=false")
            dates = [r.get("date", "") for r in v.get("runs", [])]
            if dates != sorted(dates):
                fail(f"index.json: version {v.get('version')} runs not date-sorted")
            for r in v.get("runs", []):
                rf = BOARDS / r["file"]
                indexed.add(rf.resolve())
                if not rf.exists():
                    fail(f"index.json: referenced file missing: {r['file']}")
                for fid in r.get("faults", []):
                    if fid not in fault_ids:
                        fail(f"index.json: run {r['file']} unknown fault id {fid!r}")
                if not r.get("models"):
                    fail(f"index.json: run {r['file']} has no per-model aggregates")
        for fp in board_files:
            if fp.resolve() not in indexed:
                fail(f"index.json: board file on disk not indexed: {fp.relative_to(BOARDS)}")

    # public artifacts
    exp = load(PUBLIC / "experiment-scores.json")
    if exp:
        check_axis_board(PUBLIC / "experiment-scores.json", exp)
        if exp.get("snapshot") != "latest":
            fail("public/experiment-scores.json: snapshot != 'latest'")
        if not exp.get("column_snapshots"):
            fail("public/experiment-scores.json: missing column_snapshots")
        # Mixed-version honesty: when the rolling view spans binary versions,
        # the per-version trust breakdown must exist and sum to the totals.
        versions = set((exp.get("ostk_versions") or {}).values())
        if len(versions) > 1:
            by_ver = exp.get("trust", {}).get("by_ostk_version")
            if not by_ver:
                fail("public/experiment-scores.json: columns span ostk "
                     "versions but trust.by_ostk_version is missing")
            else:
                for k in ("solve_valid_cells", "cost_valid_cells",
                          "cost_invalid_cells", "invalid_cells"):
                    total = sum(v.get(k, 0) for v in by_ver.values())
                    if total != exp["trust"].get(k, -1):
                        fail(f"public/experiment-scores.json: by_ostk_version "
                             f"{k} sums to {total} != trust total "
                             f"{exp['trust'].get(k)}")
        # Eviction disclosure: every column whose published snapshot displaced
        # older cells must be declared in column_supersessions consistently.
        for col, sup in (exp.get("column_supersessions") or {}).items():
            if sup.get("published_snapshot") != exp["column_snapshots"].get(col):
                fail(f"public/experiment-scores.json: column_supersessions "
                     f"{col} snapshot {sup.get('published_snapshot')!r} != "
                     f"column_snapshots {exp['column_snapshots'].get(col)!r}")

    board = load(PUBLIC / "board-v760.json")
    cells = load(PUBLIC / "cells-v760.json")
    scores = load(PUBLIC / "scores.json")
    if isinstance(scores, list):
        check_binary_identity(PUBLIC / "scores.json", scores)
    if board and exp:
        ostk_solved = {m["model"]: m["ostk"]["solved"] for m in exp["models"]}
        nat_solved = {m["model"]: m["native"]["solved"] for m in exp["models"]}
        for m in board.get("models", []):
            if m.get("type") not in ("B", "B*"):
                fail(f"board-v760: {m['model']} bad type {m.get('type')!r}")
            if m.get("cross_version_pair"):
                # doctrine: no cross-version delta is published anywhere
                if m.get("cost_delta") is not None or m.get("tok_delta") is not None:
                    fail(f"board-v760: {m['model']} publishes a delta across "
                         f"binary versions (cross_version_pair=true must "
                         f"suppress cost_delta/tok_delta)")
            if m["kernel_solved"] != ostk_solved.get(m["model"]):
                fail(f"board-v760: {m['model']} kernel_solved "
                     f"{m['kernel_solved']} != experiment ostk {ostk_solved.get(m['model'])} "
                     f"(two-truths drift)")
            if m.get("native_present") and m["native_solved"] != nat_solved.get(m["model"]):
                fail(f"board-v760: {m['model']} native_solved "
                     f"{m['native_solved']} != experiment native {nat_solved.get(m['model'])}")
            if m.get("native") is None and m.get("native_cost_basis") not in (
                    "unavailable", None) and m.get("both_cost_n", 0) == 0 \
                    and m.get("native_present"):
                pass  # native buckets legitimately null when no both-solved cost pair
    if cells:
        vocab = {"pass", "fail", "deadline", "infra", "invalid", "missing"}
        bench_names = [b["name"] for b in cells.get("benchmarks", [])]
        for m in cells.get("models", []):
            if sorted(m["cells"].keys()) != sorted(bench_names):
                fail(f"cells-v760: {m['model']} cells keys != benchmarks list")
            npass = 0
            for b, pair in m["cells"].items():
                for armname in ("native", "kernel"):
                    st = pair[armname].get("status")
                    if st not in vocab:
                        fail(f"cells-v760: {m['model']}/{b}/{armname} bad status {st!r}")
                    if pair[armname].get("cost") is not None and not pair[armname].get("cost_valid"):
                        fail(f"cells-v760: {m['model']}/{b}/{armname} priced cost on cost-invalid cell")
                if pair["native"].get("status") == "pass":
                    npass += 1
            if npass != m.get("native_solved"):
                fail(f"cells-v760: {m['model']} native_solved {m['native_solved']} != {npass} pass cells")
    if scores is not None:
        if not isinstance(scores, list) or not scores:
            fail("public/scores.json: empty or not a list")
        else:
            seen = set()
            for s in scores:
                key = (s.get("agent"), s.get("benchmark"))
                if key in seen:
                    fail(f"public/scores.json: duplicate cell {key} (double-count risk)")
                seen.add(key)
                if not s.get("cost_valid", False) and s.get("computed_cost_usd") is not None:
                    fail(f"public/scores.json: {key} cost-invalid cell carries a price")

    n_files = len(board_files) + 4
    if FAILS:
        print(f"\ncheck_boards: {len(FAILS)} FAILURE(S) across {n_files} files",
              file=sys.stderr)
        return 1
    print(f"check_boards: OK — {len(board_files)} board files + 4 public artifacts, "
          f"{len(fault_ids)} fault annotations, all invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
