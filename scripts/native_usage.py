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


def parse_claude_result_json(text: str) -> dict | None:
    """Recover num_turns + token/cost usage from a Claude Code CLI *result*
    envelope, INCLUDING error results (is_error:true / subtype:
    error_max_turns and the other error subtypes).

    WHY: the native error-result writer discards an error-terminated run to a
    0-turn / 0-token "pass" (nginx-upstream-port-mismatch: num_turns:41,
    $1.40 spent, all zeroed). But the raw envelope survives — as a JSONL line
    in native.stdout AND as a `harness error: {...}` line in launcher.log —
    so we parse it wherever it lands, on ANY line whose first `{` decodes to a
    {"type":"result", ...} object. is_error is deliberately IGNORED: an
    error-terminated run still billed real tokens and took real turns."""
    for line in text.splitlines():
        brace = line.find("{")
        if brace < 0 or '"type"' not in line or '"result"' not in line:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(line[brace:])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "result":
            continue
        usage = obj.get("usage") or {}
        fresh = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        nt = obj.get("num_turns")
        num_turns = int(nt) if isinstance(nt, (int, float)) else None
        cost = obj.get("total_cost_usd")
        if fresh + cache_read + cache_create + out == 0 and not num_turns:
            continue  # a result envelope with no usable telemetry — keep looking
        return {
            "source": "claude_result_json",
            "api_calls": num_turns or 0,
            "num_turns": num_turns,
            "fresh_input_tokens": fresh,
            "cache_read_tokens": cache_read,
            "cache_create_tokens": cache_create,
            "output_tokens": out,
            "reasoning_tokens": 0,
            "billed_tokens": fresh + cache_read + cache_create,
            "cost_usd": round(float(cost), 6) if cost else None,
        }
    return None


_MODEL_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
_HAIKU_SIDECAR = "claude-haiku-4-5"


def _canon_model_key(key: str) -> str:
    """Strip a trailing -YYYYMMDD date suffix for sidecar-model comparison
    ONLY. The raw (possibly date-suffixed) key is still what ends up in
    observed_models — cell_validity.normalize_model_id does its own
    normalization at compare time."""
    return _MODEL_DATE_SUFFIX_RE.sub("", key)


def observed_models_from_result_json(text: str) -> list[str] | None:
    """Pull the real, billable model id(s) that served THIS session from a
    Claude Code CLI result envelope's `modelUsage` map (the same envelope
    parse_claude_result_json reads, INCLUDING error results).

    WHY: a Claude Code session can silently continue on a different model
    mid-session — e.g. Fable 5's cyber-safety classifier trips on a
    security-flavored bench and the vendor CLI falls back to Opus 4.8
    without surfacing it anywhere else in the score payload (see
    boards/FAULTS.json july09-fable5-native-opus-fallback: 4 cells, verified
    via modelUsage showing both fable AND opus token buckets in one session).
    modelUsage is keyed by every model id that billed tokens in the session,
    so >1 non-sidecar id here means the session was not pure single-model.

    The claude-haiku-4-5 sidecar (which the CLI runs internally for titles /
    quick classification on EVERY session — not a fallback) and any
    zero-usage entries are excluded: they are not part of the cell's model
    identity claim. Returns None when no result envelope with a usable
    modelUsage map is found (mirrors parse_claude_result_json's None
    contract)."""
    for line in text.splitlines():
        brace = line.find("{")
        if brace < 0 or '"type"' not in line or '"result"' not in line:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(line[brace:])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "result":
            continue
        mu = obj.get("modelUsage")
        if not isinstance(mu, dict) or not mu:
            continue
        models = []
        for key, d in mu.items():
            if _canon_model_key(key) == _HAIKU_SIDECAR:
                continue
            d = d or {}
            used = (int(d.get("inputTokens") or 0) + int(d.get("outputTokens") or 0)
                    + int(d.get("cacheReadInputTokens") or 0)
                    + int(d.get("cacheCreationInputTokens") or 0))
            if used == 0:
                continue
            models.append(key)
        return models or None
    return None


def observed_models_from_raw(raw_dir: Path) -> list[str] | None:
    """observed_models_from_result_json, sourced from a cell's
    .raw/native.stdout (or None if the file is absent/unreadable/foreign)."""
    fp = raw_dir / "native.stdout"
    if not fp.is_file():
        return None
    try:
        text = fp.read_text(errors="replace")
    except OSError:
        return None
    return observed_models_from_result_json(text)


def parse_gemini_stats(text: str) -> dict | None:
    """Recover usage from the Gemini CLI end-of-run stats JSON (native.stdout).

    The block is a pretty-printed object keyed on `session_id` with a
    `stats.models.<model>.tokens` bag — prompt (total input incl. cache),
    input (fresh / non-cached), cached (cache read), candidates (output),
    thoughts (reasoning) — and `api.totalRequests` (the turn proxy). Gemini
    reports no per-run USD, so cost_usd stays None and the consolidator prices
    the buckets. Gemini also has no cache-CREATE split, so that bucket is 0."""
    idx = text.rfind('"session_id"')
    if idx < 0:
        return None
    start = text.rfind("{", 0, idx)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    models = ((obj.get("stats") or {}).get("models")) or {}
    if not models:
        return None
    fresh = cache_read = out = reasoning = calls = 0
    for m in models.values():
        tok = (m or {}).get("tokens") or {}
        api = (m or {}).get("api") or {}
        prompt = int(tok.get("prompt") or 0)
        cached = int(tok.get("cached") or 0)
        inp = int(tok.get("input") or 0)
        # fresh = non-cached input. prompt-minus-cached is authoritative
        # (prompt is the whole input side incl. cache read); fall back to the
        # reported 'input' field when prompt is absent/inconsistent.
        fresh += (prompt - cached) if prompt >= cached and prompt > 0 else inp
        cache_read += cached
        out += int(tok.get("candidates") or 0) + int(tok.get("thoughts") or 0)
        reasoning += int(tok.get("thoughts") or 0)
        calls += int(api.get("totalRequests") or 0)
    if fresh + cache_read + out == 0:
        return None
    return {
        "source": "gemini_stats",
        "api_calls": calls,
        "num_turns": calls or None,
        "fresh_input_tokens": fresh,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": 0,  # Gemini stats carry no cache-create split
        "output_tokens": out,
        "reasoning_tokens": reasoning,
        "billed_tokens": fresh + cache_read,
        "cost_usd": None,
    }


def recover_usage(raw_dir: Path) -> dict | None:
    """Recover usage from a cell's .raw/ artifacts, trying each CLI shape.
    Precedence: opencode JSONL (strict), kimi event stream, Claude Code result
    envelope (incl. error results), Gemini stats, vibe meta."""
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
        usage = parse_claude_result_json(text)
        if usage:
            return usage
        usage = parse_gemini_stats(text)
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
    """Patch one native-arm score payload with recovered CLI usage AND (independently)
    an observed-model-identity receipt.

    Returns (changed, note). Two independent enrichments happen here:

      1. MODEL-IDENTITY RECEIPT (→GPT-review F5c): extract observed_models
         from the raw Claude CLI result envelope's modelUsage map whenever
         one is recoverable. This runs regardless of whether the payload
         already carries real cost accounting — the fable-5/opus-4-8
         mid-session fallback happens on cells the writer already billed
         correctly, so gating this behind the cost check would mean it never
         fires for exactly the cells it exists to catch. Idempotent: never
         overwrites an existing observed_models receipt (unless force).

      2. COST-TELEMETRY RECOVERY (the original purpose of this module): never
         touches a payload that already carries real billed accounting
         (unless force). When nothing is recoverable, stamps
         cost_basis='unavailable' explicitly (solve axis untouched).
    """
    try:
        payload = json.loads(score_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return False, f"unreadable payload ({e})"
    if not isinstance(payload, dict):
        return False, "malformed payload"

    bench = payload.get("benchmark") or score_path.name.removesuffix(".score.json")
    if raw_dir is None:
        raw_dir = score_path.parent / f"{bench}.raw"

    model_note: str | None = None
    if "observed_models" not in payload or force:
        observed = observed_models_from_raw(raw_dir) if raw_dir.is_dir() else None
        if observed:
            if apply:
                payload["observed_models"] = observed
            model_note = f"observed_models={observed}"

    def _finish(changed: bool, note: str) -> tuple[bool, str]:
        """Fold the (independent) model-identity note into the return value,
        writing the payload if the model receipt alone is what changed."""
        if not changed and model_note:
            if apply:
                score_path.write_text(json.dumps(payload, indent=2))
            return True, model_note
        if changed and model_note:
            return True, f"{note}; {model_note}"
        return changed, note

    if _accounted(payload) and not force:
        return _finish(False, "already accounted")

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
            return _finish(False, "no telemetry, but not a completed-work cell — untouched")
        note = "no telemetry recoverable -> cost_basis=unavailable"
        if payload.get("cost_basis") == "unavailable":
            return _finish(False, "already marked unavailable")
        if apply:
            payload["cost_basis"] = "unavailable"
            payload["native_usage_source"] = "none"
            score_path.write_text(json.dumps(payload, indent=2))
        return _finish(True, note)

    prev = {k: payload.get(k) for k in
            ("input_tokens", "output_tokens", "billed_tokens",
             "estimated_cost_usd", "turns_to_fix")}
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
        # Recover the real turn count too: the native error-result writer zeros
        # turns_to_fix alongside tokens, so a recovered num_turns replaces a
        # 0 (never overwrites a genuine, already-nonzero count).
        if usage.get("num_turns") and not (payload.get("turns_to_fix") or 0):
            payload["turns_to_fix"] = usage["num_turns"]
        payload.pop("cost_basis", None)  # recovered — no longer unavailable
        payload["native_usage_source"] = usage["source"]
        payload["native_usage"] = usage
        payload["native_usage_prev"] = prev
        score_path.write_text(json.dumps(payload, indent=2))
    note = (f"{usage['source']}: billed_in={usage['billed_tokens']} "
            f"(fresh={usage['fresh_input_tokens']} cr={usage['cache_read_tokens']} "
            f"cc={usage['cache_create_tokens']}) out={usage['output_tokens']} "
            f"calls={usage['api_calls']}"
            + (f" turns={usage['num_turns']}" if usage.get("num_turns") else "")
            + (f" cost=${usage['cost_usd']}" if usage["cost_usd"] else ""))
    return _finish(True, note)


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
