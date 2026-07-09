#!/usr/bin/env python3
"""reprice_native.py — honest re-pricing of the native-arm cost axis from raw
per-model token counts, instead of trusting the vendor CLI's own cost estimate.

WHY: native-arm cells copy `estimated_cost_usd` verbatim from the Claude Code
CLI's telemetry (its `total_cost_usd`, the sum of per-model `modelUsage[m].costUSD`).
That figure is only as trustworthy as the CLI's internal price table, and for
`claude-sonnet-5` the CLI is demonstrably wrong: it prices Sonnet 5 at the
**Claude Opus 4.8 rate card** ($5 in / $25 out / $0.50 cache-read / $6.25
5m-cache-write per MTok) rather than Sonnet 5's real $3/$15 standard (or $2/$10
introductory through 2026-08-31). A linear fit of the recorded token splits
against the CLI's per-cell cost recovers those Opus rates to floating-point
precision — a flat mis-tiered rate card, NOT a per-request long-context premium
(there is none: Fable 5, Opus 4.8, and Sonnet 5 all bill the full 1M window at
standard rates). The CLI prices Opus 4.8 and Fable 5 correctly; only Sonnet 5 is
mis-tiered, inflating its cost axis by 1.67x vs standard / 2.5x vs intro.

HOW: this script reprices every native cell from its raw *per-model* token split
(`modelUsage` in the cell's .raw/native.stdout) against a pinned, source-verified
rate table — each model's tokens at that model's own correct rate, including the
small Haiku sidecar the CLI runs for titles/quick tasks. Per-model pricing is
load-bearing: on Fable 5 security cells that trip the cyber-safety classifier and
fall back to Opus 4.8, one cell blends Fable + Opus tokens, and the score-file
aggregate (fresh/cache_read/cache_create/output) collapses both into one bucket —
so a single-rate reprice of the aggregate over-charges the Opus-served tokens.
When the raw telemetry is missing, the script falls back to repricing the
score-file aggregate at the cell's main-model rate (correct only for
single-model cells; flagged in the output).

READ-ONLY by default: reads runs/<model>-native/*.score.json and their
.raw/native.stdout, and writes nothing. Pass --emit <path> to dump JSON to a
chosen path. Stdlib only.

Rates verified against https://platform.claude.com/docs/en/about-claude/pricing
(captured 2026-07-09):
  cache multipliers: 5m-write = 1.25x base input, 1h-write = 2x, read = 0.1x.
  Sonnet 5 introductory $2/$10 per MTok through 2026-08-31, standard $3/$15
  from 2026-09-01. Fable 5 $10/$50, Opus 4.8 $5/$25, Haiku 4.5 $1/$5. All three
  frontier models include the full 1M context window at standard pricing — no
  long-context (>200K) premium.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# pinned rate table — per MTok USD, source-verified (see module docstring)
# ---------------------------------------------------------------------------
CACHE_WRITE_5M_MULT = 1.25   # 5-minute cache write = 1.25x base input
CACHE_WRITE_1H_MULT = 2.00   # 1-hour cache write   = 2.00x base input
CACHE_READ_MULT = 0.10       # cache read (hit)      = 0.10x base input

# Long-context premium threshold. Per Anthropic pricing docs, NONE of the models
# here carry a >200K premium (the full 1M window is standard-priced), so every
# model's long_context_mult is 1.0 and this threshold never bites. Kept explicit
# so the table documents the (non-)tiering rather than hiding it.
LONG_CONTEXT_THRESHOLD = 200_000

# Each model: date-windowed (start_incl, end_incl, base_input, base_output) in
# $/MTok on the cell's UTC date (first matching window wins), plus a
# long-context multiplier.
RATE_TABLE: dict[str, dict] = {
    "claude-sonnet-5": {
        "windows": [
            ("2000-01-01", "2026-08-31", 2.0, 10.0),   # introductory
            ("2026-09-01", "2999-12-31", 3.0, 15.0),   # standard
        ],
        "long_context_mult": 1.0,   # 1M window at standard pricing, no premium
    },
    "claude-opus-4-8": {
        "windows": [("2000-01-01", "2999-12-31", 5.0, 25.0)],
        "long_context_mult": 1.0,
    },
    "claude-fable-5": {
        "windows": [("2000-01-01", "2999-12-31", 10.0, 50.0)],
        "long_context_mult": 1.0,
    },
    # Sidecar model the CLI runs for titles / quick classification.
    "claude-haiku-4-5": {
        "windows": [("2000-01-01", "2999-12-31", 1.0, 5.0)],
        "long_context_mult": 1.0,
    },
}

# The rate card the CLI ACTUALLY applied to each frontier model, recovered by
# linear regression of recorded token splits against the CLI's per-cell cost
# (exact to ~1e-15). Sonnet 5's row is the Opus 4.8 card — that is the bug.
# Used only to annotate the report; not part of the reprice math.
CLI_EFFECTIVE_INPUT_RATE = {
    "claude-sonnet-5": 5.0,    # WRONG: Opus 4.8 rate; real Sonnet 5 is 3.0/2.0
    "claude-opus-4-8": 5.0,    # correct
    "claude-fable-5": 10.0,    # correct
}

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
_RESULT_MARKER = '{"type":"result"'


def _canon_model(key: str) -> str:
    """Normalize a modelUsage key to a rate-table handle (strip -YYYYMMDD)."""
    return _DATE_SUFFIX_RE.sub("", key)


def _rates_for(model: str, cell_date: str) -> dict[str, float] | None:
    """Per-MTok rates for a model on the cell's UTC date, or None if unknown."""
    spec = RATE_TABLE.get(model)
    if spec is None:
        return None
    base_in = base_out = None
    for start, end, r_in, r_out in spec["windows"]:
        if start <= cell_date <= end:
            base_in, base_out = r_in, r_out
            break
    if base_in is None:  # date outside every window — use the last window
        _, _, base_in, base_out = spec["windows"][-1]
    return {
        "input": base_in,
        "output": base_out,
        "cache_read": round(base_in * CACHE_READ_MULT, 6),
        "cache_write_5m": round(base_in * CACHE_WRITE_5M_MULT, 6),
        "cache_write_1h": round(base_in * CACHE_WRITE_1H_MULT, 6),
        "long_context_mult": spec["long_context_mult"],
    }


def _price(model: str, date: str, fresh: int, cache_read: int,
           cc_5m: int, cc_1h: int, output: int) -> float | None:
    r = _rates_for(model, date)
    if r is None:
        return None
    return r["long_context_mult"] * (
        fresh * r["input"]
        + cache_read * r["cache_read"]
        + cc_5m * r["cache_write_5m"]
        + cc_1h * r["cache_write_1h"]
        + output * r["output"]
    ) / 1e6


def _cell_date(payload: dict) -> str:
    ts = payload.get("timestamp") or ""
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    return "2026-07-09"


def _read_model_usage(raw_dir: Path) -> dict | None:
    """Pull the per-model `modelUsage` map from a cell's .raw/native.stdout.
    Returns {model_key: {inputTokens, outputTokens, cacheReadInputTokens,
    cacheCreationInputTokens, costUSD}} or None."""
    fp = raw_dir / "native.stdout"
    if not fp.is_file():
        return None
    try:
        text = fp.read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        idx = line.find(_RESULT_MARKER)
        if idx < 0:
            continue
        try:
            obj = json.loads(line[idx:])
        except json.JSONDecodeError:
            continue
        mu = obj.get("modelUsage")
        if isinstance(mu, dict) and mu:
            return mu
    return None


def reprice_cell(payload: dict, model: str, raw_dir: Path) -> dict | None:
    """Reprice one cell. Prefers the raw per-model `modelUsage` split (correct
    across refusal→fallback cells); falls back to the score-file aggregate at the
    main-model rate. Returns a comparison row, or None if the cell carries no
    cost accounting."""
    if payload.get("cost_basis") == "unavailable":
        return None
    date = _cell_date(payload)
    cli = payload.get("estimated_cost_usd")
    cli = float(cli) if cli not in (None, "") else None

    mu = _read_model_usage(raw_dir)
    per_model: list[dict] = []
    unpriceable: list[str] = []
    if mu:
        repriced = 0.0
        for key, d in mu.items():
            handle = _canon_model(key)
            fresh = int(d.get("inputTokens") or 0)
            cr = int(d.get("cacheReadInputTokens") or 0)
            cc = int(d.get("cacheCreationInputTokens") or 0)  # all 5m per telemetry
            out = int(d.get("outputTokens") or 0)
            if fresh == cr == cc == out == 0:
                continue
            cost = _price(handle, date, fresh, cr, cc, 0, out)
            if cost is None:
                unpriceable.append(handle)
                # keep the CLI's own number for a model we can't price
                cost = float(d.get("costUSD") or 0.0)
            repriced += cost
            per_model.append({
                "model": handle, "input": fresh, "cache_read": cr,
                "cache_create": cc, "output": out,
                "repriced_usd": round(cost, 6),
                "cli_usd": round(float(d.get("costUSD") or 0.0), 6),
            })
        source = "modelUsage"
    else:
        # Fallback: reprice the score-file aggregate at the main-model rate.
        fresh = int(payload.get("fresh_input_tokens") or payload.get("input_tokens") or 0)
        cr = int(payload.get("cache_read_tokens") or 0)
        cc = int(payload.get("cache_create_tokens") or 0)
        cc_5m = int(payload.get("cache_create_5m_tokens") or 0)
        cc_1h = int(payload.get("cache_create_1h_tokens") or 0)
        if cc_5m + cc_1h < cc:
            cc_5m += cc - (cc_5m + cc_1h)
        out = int(payload.get("output_tokens") or 0)
        if fresh == cr == cc == out == 0:
            return None
        repriced = _price(model, date, fresh, cr, cc_5m, cc_1h, out)
        if repriced is None:
            return None
        source = "score_aggregate"

    if not per_model and source == "modelUsage":
        return None

    fallback = sum(
        1 for p in per_model
        if p["model"] != "claude-haiku-4-5" and (p["input"] or p["output"] or p["cache_read"])
    ) > 1
    return {
        "benchmark": payload.get("benchmark"),
        "date": date,
        "source": source,
        "fallback_blended": fallback,
        "unpriceable_models": unpriceable,
        "per_model": per_model,
        "cli_estimated_cost_usd": cli,
        "repriced_cost_usd": round(repriced, 6),
        "delta_usd": round(repriced - cli, 6) if cli is not None else None,
    }


def reprice_model(runs_root: Path, model: str) -> dict:
    arm_dir = runs_root / f"{model}-native"
    rows: list[dict] = []
    skipped: list[str] = []
    for score_fp in sorted(arm_dir.glob("*.score.json")):
        try:
            payload = json.loads(score_fp.read_text())
        except (OSError, json.JSONDecodeError) as e:
            skipped.append(f"{score_fp.name}: unreadable ({e})")
            continue
        bench = payload.get("benchmark") or score_fp.name.removesuffix(".score.json")
        row = reprice_cell(payload, model, arm_dir / f"{bench}.raw")
        if row is None:
            skipped.append(f"{score_fp.name}: no cost accounting")
            continue
        rows.append(row)
    cli_total = sum(r["cli_estimated_cost_usd"] or 0 for r in rows)
    repriced_total = sum(r["repriced_cost_usd"] for r in rows)
    return {
        "model": model,
        "arm_dir": str(arm_dir),
        "cells": len(rows),
        "fallback_cells": sum(1 for r in rows if r["fallback_blended"]),
        "skipped": skipped,
        "cli_effective_input_rate": CLI_EFFECTIVE_INPUT_RATE.get(model),
        "cli_total_usd": round(cli_total, 6),
        "repriced_total_usd": round(repriced_total, 6),
        "delta_usd": round(repriced_total - cli_total, 6),
        "rows": rows,
    }


def _print_report(result: dict) -> None:
    model = result["model"]
    print(f"\n=== {model}-native  ({result['cells']} cells priced,"
          f" {result['fallback_cells']} refusal→fallback) ===")
    eff = result["cli_effective_input_rate"]
    correct = RATE_TABLE[model]["windows"][0][2]
    tag = "   (correct)" if eff == correct else f"   <-- MISPRICED (real base is ${correct}/MTok)"
    print(f"    CLI effective input rate: ${eff}/MTok{tag}")
    print(f"    {'benchmark':<34} {'CLI $':>9} {'repriced $':>11} {'delta $':>9} {'delta%':>7}  note")
    print(f"    {'-'*34} {'-'*9} {'-'*11} {'-'*9} {'-'*7}  ----")
    for r in result["rows"]:
        cli = r["cli_estimated_cost_usd"]
        rep = r["repriced_cost_usd"]
        d = r["delta_usd"]
        cli_s = f"{cli:9.4f}" if cli is not None else f"{'n/a':>9}"
        d_s = f"{d:9.4f}" if d is not None else f"{'n/a':>9}"
        pct = f"{(d/cli*100):7.1f}" if (cli not in (None, 0) and d is not None) else f"{'n/a':>7}"
        note = "fallback→opus" if r["fallback_blended"] else ""
        print(f"    {(r['benchmark'] or '?'):<34} {cli_s} {rep:11.4f} {d_s} {pct}  {note}")
    ct, rt, dl = result["cli_total_usd"], result["repriced_total_usd"], result["delta_usd"]
    pct = f"{(dl/ct*100):+.1f}%" if ct else "n/a"
    print(f"    {'-'*34} {'-'*9} {'-'*11} {'-'*9} {'-'*7}")
    print(f"    {'TOTAL':<34} {ct:9.4f} {rt:11.4f} {dl:9.4f} {pct:>7}")
    if result["skipped"]:
        print(f"    ({len(result['skipped'])} cell(s) skipped: no cost accounting)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="reprice native-arm cost from raw per-model token counts")
    ap.add_argument("--runs", default=None, help="runs/ tree (default: <repo>/runs)")
    ap.add_argument("--model", action="append", default=[],
                    help="frontier model (on-disk spelling); repeatable. "
                         "Default: sonnet-5, opus-4-8, fable-5.")
    ap.add_argument("--emit", default=None,
                    help="write the full comparison as JSON to this path "
                         "(default: write nothing)")
    args = ap.parse_args()

    runs_root = Path(args.runs) if args.runs else Path(__file__).resolve().parents[1] / "runs"
    if not runs_root.is_dir():
        print(f"ERROR: {runs_root} is not a directory", file=sys.stderr)
        return 2

    default_models = ["claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"]
    models = args.model or default_models
    unknown = [m for m in models if m not in RATE_TABLE]
    if unknown:
        print(f"ERROR: no pinned rates for: {', '.join(unknown)}", file=sys.stderr)
        return 2

    report = {"runs_root": str(runs_root), "models": []}
    for model in models:
        if not (runs_root / f"{model}-native").is_dir():
            print(f"  (skip {model}: no {model}-native/ dir)", file=sys.stderr)
            continue
        result = reprice_model(runs_root, model)
        report["models"].append(result)
        _print_report(result)

    if report["models"]:
        gct = sum(m["cli_total_usd"] for m in report["models"])
        grt = sum(m["repriced_total_usd"] for m in report["models"])
        print(f"\nGRAND TOTAL across {len(report['models'])} model(s): "
              f"CLI ${gct:.2f}  ->  repriced ${grt:.2f}  (delta ${grt - gct:+.2f})")

    if args.emit:
        emit_path = Path(args.emit)
        emit_path.write_text(json.dumps(report, indent=2))
        print(f"\nwrote comparison JSON -> {emit_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
