#!/usr/bin/env python3
"""
native_usage.py — recover billed token/cost accounting for native-arm cells
from the vendor CLI's own telemetry.

WHY: 130 native cells (deepseek / grok / kimi / devstral) are cost-blind —
their score payloads carry billed_tokens: 0 with empty buckets, so
cell_validity classifies them INVALID (zero_token_accounting:zero_billed) and
the trust doctrine forbids pricing them at the rate card (that synthesized
3-29x over the provider-billed truth). But the raw artifacts persisted to
runs/<model>-native/<bench>.raw/ DO carry real per-call usage:

  opencode (grok-*, deepseek-* fallback) — native.stdout is JSONL; every
      `step_finish` event has part.tokens {input, output, reasoning,
      cache:{read, write}} and part.cost (provider-billed USD). Sum them.
  kimi CLI (kimi-*, moonshot-*) — native.stdout is a pretty-printed event
      stream; each API call emits `StatusUpdate(... token_usage=TokenUsage(
      input_other=N, output=N, input_cache_read=N, input_cache_creation=N)
      ... message_id='...')`. Per-call usage; sum per unique message_id.
  vibe CLI (devstral-*, mistral-*, codestral-*) — vibe-meta.json carries
      stats.session_prompt_tokens / session_completion_tokens (summed prompt
      and completion across all calls; Mistral has no cache-split billing, so
      the prompt sum IS the billed input side). session_cost is 0.0 unless
      the CLI was configured with prices — never trusted when 0.

Where telemetry is genuinely absent the payload is stamped
cost_basis='unavailable' EXPLICITLY (lane A3 renders the cost axis as
unavailable) — the SOLVE verdict is untouched either way: missing cost
accounting must not destroy a valid solve.

Wired into run_matrix_v41.run_cell for every native-arm cell (enrich before
the validity gate); also runnable standalone over historical runs:

    python3 scripts/native_usage.py runs/ [--model X ...] [--apply]

Default is a dry-run report; --apply patches score payloads in place with
full provenance (native_usage_source, native_usage, native_usage_prev).
Stdlib only. Idempotent: already-accounted cells are never touched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ARM_DIR_RE = re.compile(r"-native$")

# ---------------------------------------------------------------------------
# parsers — one per CLI telemetry shape
# ---------------------------------------------------------------------------


def parse_opencode_stdout(text: str) -> dict | None:
    """Sum step_finish token/cost accounting from opencode --format json."""
    fresh = out = reasoning = cache_read = cache_write = 0
    cost = 0.0
    calls = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or '"step_finish"' not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "step_finish":
            continue
        part = row.get("part") or {}
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        calls += 1
        fresh += int(tokens.get("input") or 0)
        out += int(tokens.get("output") or 0)
        reasoning += int(tokens.get("reasoning") or 0)
        cache_read += int(cache.get("read") or 0)
        cache_write += int(cache.get("write") or 0)
        cost += float(part.get("cost") or 0.0)
    if calls == 0:
        return None
    return {
        "source": "opencode_stdout",
        "api_calls": calls,
        "fresh_input_tokens": fresh,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": cache_write,
        "output_tokens": out + reasoning,
        "reasoning_tokens": reasoning,
        "billed_tokens": fresh + cache_read + cache_write,
        "cost_usd": round(cost, 6) if cost > 0 else None,
    }


_KIMI_STATUS_RE = re.compile(
    r"TokenUsage\((?P<body>[^)]*)\)(?P<rest>.{0,400}?)message_id='(?P<mid>[^']+)'",
    re.S,
)
_KV_RE = re.compile(r"(\w+)=(\d+)")


def parse_kimi_stdout(text: str) -> dict | None:
    """Sum per-call TokenUsage(...) blocks from the kimi CLI event stream.
    Each StatusUpdate is one API call (keyed by message_id — the last
    StatusUpdate wins if a message_id repeats)."""
    per_call: dict[str, dict] = {}
    anon = 0
    for m in _KIMI_STATUS_RE.finditer(text):
        kv = {k: int(v) for k, v in _KV_RE.findall(m.group("body"))}
        if not kv:
            continue
        mid = m.group("mid") or f"_anon{anon}"
        anon += 1
        per_call[mid] = kv
    if not per_call:
        return None
    fresh = sum(kv.get("input_other", 0) for kv in per_call.values())
    out = sum(kv.get("output", 0) for kv in per_call.values())
    cache_read = sum(kv.get("input_cache_read", 0) for kv in per_call.values())
    cache_create = sum(kv.get("input_cache_creation", 0) for kv in per_call.values())
    return {
        "source": "kimi_stdout",
        "api_calls": len(per_call),
        "fresh_input_tokens": fresh,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": cache_create,
        "output_tokens": out,
        "reasoning_tokens": 0,
        "billed_tokens": fresh + cache_read + cache_create,
        "cost_usd": None,  # kimi CLI reports no cost — consolidator prices buckets
    }


def parse_vibe_meta(text: str) -> dict | None:
    """Session totals from vibe-meta.json (Mistral vibe CLI)."""
    try:
        meta = json.loads(text)
    except json.JSONDecodeError:
        return None
    stats = (meta or {}).get("stats") or {}
    prompt = int(stats.get("session_prompt_tokens") or 0)
    completion = int(stats.get("session_completion_tokens") or 0)
    if prompt + completion == 0:
        return None
    cost = float(stats.get("session_cost") or 0.0)
    return {
        "source": "vibe_meta",
        "api_calls": int(stats.get("steps") or 0),
        # no cache split on this API — the prompt sum is the billed input side
        "fresh_input_tokens": prompt,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "output_tokens": completion,
        "reasoning_tokens": 0,
        "billed_tokens": prompt,
        "cost_usd": round(cost, 6) if cost > 0 else None,
    }


def recover_usage(raw_dir: Path) -> dict | None:
    """Recover usage from a cell's .raw/ artifacts, trying each CLI shape.
    Precedence: opencode JSONL (strict), kimi event stream, vibe meta."""
    stdout_fp = raw_dir / "native.stdout"
    text = None
    if stdout_fp.is_file():
        try:
            text = stdout_fp.read_text(errors="replace")
        except OSError:
            text = None
    if text:
        usage = parse_opencode_stdout(text)
        if usage:
            return usage
        usage = parse_kimi_stdout(text)
        if usage:
            return usage
    meta_fp = raw_dir / "vibe-meta.json"
    if meta_fp.is_file():
        try:
            usage = parse_vibe_meta(meta_fp.read_text(errors="replace"))
        except OSError:
            usage = None
        if usage:
            return usage
    return None


# ---------------------------------------------------------------------------
# score enrichment
# ---------------------------------------------------------------------------
def _accounted(payload: dict) -> bool:
    """Does the payload already carry real billed accounting? (billed_tokens
    or any atomic bucket nonzero — the exact complement of cell_validity's
    zero_billed class.)"""
    if (payload.get("billed_tokens") or 0) > 0:
        return True
    bucket_sum = ((payload.get("fresh_input_tokens") or 0)
                  + (payload.get("cache_read_tokens") or 0)
                  + (payload.get("cache_create_tokens") or 0))
    return bucket_sum > 0


def enrich_score_file(score_path: Path, raw_dir: Path | None = None,
                      apply: bool = True, force: bool = False) -> tuple[bool, str]:
    """Patch one native-arm score payload with recovered CLI usage.

    Returns (changed, note). Never touches a payload that already carries
    real accounting (unless force). When nothing is recoverable, stamps
    cost_basis='unavailable' explicitly (solve axis untouched)."""
    try:
        payload = json.loads(score_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, f"unreadable payload ({e})"
    if not isinstance(payload, dict):
        return False, "malformed payload"
    if _accounted(payload) and not force:
        return False, "already accounted"

    bench = payload.get("benchmark") or score_path.name.removesuffix(".score.json")
    if raw_dir is None:
        raw_dir = score_path.parent / f"{bench}.raw"
    usage = recover_usage(raw_dir) if raw_dir.is_dir() else None

    if usage is None:
        # cost_basis=unavailable is ONLY for cells that claim COMPLETED WORK
        # with no recoverable billing. A passive-verify cell (stop=pass, 0
        # turns, 0 tokens) has a LEGITIMATE $0 cost, and a zero-work/stall/
        # deadline cell is solve-axis INVALID upstream — stamping either
        # would misdescribe them (mirrors cell_validity's completed/passive
        # split).
        stop = (payload.get("stop_reason") or "").lower()
        turns = payload.get("turns_to_fix") or 0
        tool_uses = payload.get("tool_uses") or 0
        completed = bool(payload.get("resolved")) or turns > 0 or tool_uses > 0
        passive_verify = stop == "pass" and turns == 0
        if not completed or passive_verify:
            return False, "no telemetry, but not a completed-work cell — untouched"
        note = "no telemetry recoverable -> cost_basis=unavailable"
        if payload.get("cost_basis") == "unavailable":
            return False, "already marked unavailable"
        if apply:
            payload["cost_basis"] = "unavailable"
            payload["native_usage_source"] = "none"
            score_path.write_text(json.dumps(payload, indent=2))
        return True, note

    prev = {k: payload.get(k) for k in
            ("input_tokens", "output_tokens", "billed_tokens",
             "estimated_cost_usd")}
    if apply:
        payload["fresh_input_tokens"] = usage["fresh_input_tokens"]
        payload["cache_read_tokens"] = usage["cache_read_tokens"]
        payload["cache_create_tokens"] = usage["cache_create_tokens"]
        payload.setdefault("cache_create_5m_tokens", 0)
        payload.setdefault("cache_create_1h_tokens", 0)
        payload["input_tokens"] = usage["fresh_input_tokens"]
        payload["output_tokens"] = usage["output_tokens"]
        if usage["reasoning_tokens"]:
            payload["reasoning_tokens"] = usage["reasoning_tokens"]
        payload["billed_tokens"] = usage["billed_tokens"]
        payload["token_cost"] = usage["fresh_input_tokens"] + usage["output_tokens"]
        if usage["cost_usd"] and not (payload.get("estimated_cost_usd") or 0) > 0:
            payload["estimated_cost_usd"] = usage["cost_usd"]
        payload.pop("cost_basis", None)  # recovered — no longer unavailable
        payload["native_usage_source"] = usage["source"]
        payload["native_usage"] = usage
        payload["native_usage_prev"] = prev
        score_path.write_text(json.dumps(payload, indent=2))
    note = (f"{usage['source']}: billed_in={usage['billed_tokens']} "
            f"(fresh={usage['fresh_input_tokens']} cr={usage['cache_read_tokens']} "
            f"cc={usage['cache_create_tokens']}) out={usage['output_tokens']} "
            f"calls={usage['api_calls']}"
            + (f" cost=${usage['cost_usd']}" if usage["cost_usd"] else ""))
    return True, note


# ---------------------------------------------------------------------------
# CLI — sweep a runs/ tree
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="recover native-arm billed usage from vendor CLI telemetry")
    ap.add_argument("root", nargs="?", default=None,
                    help="runs/ tree (default: <repo>/runs)")
    ap.add_argument("--model", action="append", default=[],
                    help="restrict to model(s), on-disk spelling; repeatable")
    ap.add_argument("--apply", action="store_true",
                    help="patch score payloads (default: dry-run report)")
    ap.add_argument("--force", action="store_true",
                    help="re-derive even for already-accounted payloads")
    args = ap.parse_args()

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1] / "runs"
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 2

    changed = skipped = unavailable = 0
    for arm_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not arm_dir.name.endswith("-native"):
            continue
        model = ARM_DIR_RE.sub("", arm_dir.name)
        if args.model and model not in args.model:
            continue
        for score_fp in sorted(arm_dir.glob("*.score.json")):
            did, note = enrich_score_file(score_fp, apply=args.apply,
                                          force=args.force)
            tag = "PATCH" if (did and args.apply) else ("WOULD " if did else "skip ")
            if did:
                changed += 1
                if "unavailable" in note:
                    unavailable += 1
            else:
                skipped += 1
            if did or "--" in note:
                print(f"  [{tag}] {arm_dir.name}/{score_fp.name}: {note}")
    mode = "applied" if args.apply else "dry-run (pass --apply to patch)"
    print(f"\n{changed} cell(s) {'patched' if args.apply else 'patchable'} "
          f"({unavailable} marked cost-unavailable), {skipped} skipped — {mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
