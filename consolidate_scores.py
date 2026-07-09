#!/usr/bin/env python3
"""
consolidate_scores.py — Aggregate score files into public JSON for the website.

Walks runs/ to find all *-native/, *-kernel/, *-kernel-cpu/ directories,
loads score files, and produces:

  public/scores.json          — flat list of all scores (best per model+bench+arm)
  public/experiment-scores.json — three-arm comparison (native/kernel/kernel-cpu)

TRUST DOCTRINE (fail-closed; see cell_validity.py):
  - Every cell is VALID, INVALID(reason), or absent (never run). INVALID cells
    (malformed payload, arm-receipt mismatch, deadline, zero-work, or zero/absent
    token accounting on a completed run) are EXCLUDED from all aggregates and
    counted VISIBLY per model/arm — never a silent 0, never a silent pass.
  - DEDUP: for kernel_arm_type=native_driver models one execution serves both
    the kernel and cpu treatments; it is presented under the single 'ostk' view
    (source_arm-annotated) and can never appear in two aggregated columns
    (duplicate_column_reason() enforces this at ingest).
  - Cost is summed from atomic token buckets with explicit fallbacks
    (compute_cost_ex); every cell carries its cost_basis so mixed-basis
    comparisons are visible, fixing the cost-inversion bug (commit bb4aa6d5).
  - teardown_masked (watchdog-scored cells after a kernel teardown hang) is
    passed through to the board and counted per model/arm.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import cell_validity

RUNS_DIR = Path(__file__).parent / "runs"
SCORES_OUTPUT = Path(__file__).parent / "public" / "scores.json"
EXPERIMENT_OUTPUT = Path(__file__).parent / "public" / "experiment-scores.json"

# Benchmarks excluded from PUBLISHED aggregates (score files kept on disk; see
# REPORT §2 Appendix). retry-storm-duplicate-transfer: flaky (1/8 spurious).
# haystack-boot / haystack-mint: STALE / UNVALIDATED TEST ORACLE — assertions
# predate the current kernel version and are unverified, so neither arm's
# pass/fail is trustworthy. Pending assertion review or retirement.
PUBLISHED_EXCLUDE = {"retry-storm-duplicate-transfer", "haystack-boot", "haystack-mint"}

# Three experiment arms
ARM_PATTERN = re.compile(r"^(.+)-(native|kernel-cpu|kernel)$")

# Rate card for cost computation ($/M tokens: input, output)
RATE_CARD = {
    "claude-haiku-4.5": (1.00, 5.00), "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00), "claude-opus-4.6": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00), "claude-opus-4.7": (5.00, 25.00),
    # claude-opus-4-8: same list price as 4-7 ($5/$25, confirmed Anthropic 2026-05).
    "claude-opus-4-8": (5.00, 25.00), "claude-opus-4.8": (5.00, 25.00),
    # claude-fable-5: Anthropic frontier list price $10/$50 per M (2026-06 launch).
    "claude-fable-5": (10.00, 50.00),
    "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.00),
    # gemini-3.1-pro-preview: ≤200k tier (Google). Provider slug carries -preview.
    "gemini-3.1-pro-preview": (2.00, 12.00), "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-flash-lite-preview": (0.10, 0.40),
    # gemini-3.5-flash: GA 2026, list price $1.50/$9.00 (implicit cache read $0.15).
    "gemini-3.5-flash": (1.50, 9.00), "gemini-3-5-flash": (1.50, 9.00),
    "gpt-4.1": (2.00, 8.00), "gpt-5-codex": (1.25, 10.00),
    "gpt-5.2": (1.25, 10.00), "gpt-5.4": (1.25, 10.00),
    # gpt-5.5 line: April 23 2026 price hike doubled the GPT-5 line → $5/$30 input/output.
    "gpt-5.5": (5.00, 30.00), "gpt-5.5-pro": (30.00, 180.00),
    "o3": (2.00, 8.00), "o4-mini": (1.10, 4.40),
    "grok-3": (3.00, 15.00), "grok-3-fast": (0.20, 0.50), "grok-4": (3.00, 15.00),
    "grok-3-mini": (0.30, 0.50), "grok-4-fast": (0.20, 0.50),
    "grok-4.1-fast": (0.20, 0.50), "grok-4.20": (2.00, 6.00),
    # grok-4.3: xAI flagship, launched 2026-04-30 ($1.25/$2.50, ≤200k tier).
    "grok-4.3": (1.25, 2.50),
    "grok-code-fast-1": (0.20, 1.50),
    "devstral-small-latest": (0.10, 0.30), "devstral-small": (0.10, 0.30),
    "devstral-medium": (0.40, 2.00), "devstral-2512": (0.40, 2.00),
    "codestral-2508": (0.30, 0.90),
    "mistral-small-4-0-26-03": (0.10, 0.30), "mistral-small-119b-2603": (0.10, 0.30),
    "kimi-k2.5": (0.40, 1.99), "kimi-k2.6": (0.40, 1.99),
    "deepseek-v3.2": (0.26, 0.38), "deepseek-r1": (0.70, 2.50),
    "deepseek-r1-0528": (0.45, 2.15),
    # deepseek-v4-pro: DeepSeek V4 Pro STANDARD list price ($1.74/$3.48 per M).
    # The 75%-off launch promo ($0.435/$0.87) expired 2026-05-31, so the
    # standard rate now applies. Native Control is key-blocked; only B*/kernel runs.
    "deepseek-v4-pro": (1.74, 3.48),
    "qwen3-coder-plus": (0.65, 3.25), "qwen3-coder": (0.22, 1.00),
    "qwen3-coder-flash": (0.20, 0.97),
    "llama-4-maverick": (0.15, 0.60),
}


def normalize_model(name: str) -> str:
    """Normalize model name: strip vendor prefix, lowercase, dots→hyphens."""
    name = name.split("/", 1)[1] if "/" in name else name
    return re.sub(r"[_.]", "-", name.lower())


# Models with working native CPU drivers in haystack create_driver().
# Others route through OpenRouter on kernel arm — CPU arm not yet built.
# NOTE: keys are NORMALIZED model names (normalize_model: lowercase, [_.]→-).
# So "claude-opus-4-8" stays, "gemini-3.1-pro-preview" → "gemini-3-1-pro-preview",
# "gpt-5.5" → "gpt-5-5", "gpt-5.5-pro" → "gpt-5-5-pro".
CPU_DRIVER_MODELS = {
    # Anthropic — native Messages API
    "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6",
    "claude-opus-4-8",  # frontier-2026 Tier-1
    "claude-fable-5",   # frontier-2026 Tier-1 (added 2026-07-09)
    # Google — native Gemini API
    # gemini-3-1-pro-preview is the frontier-2026 Tier-1 entry (also pre-existing).
    "gemini-2-5-flash", "gemini-2-5-pro", "gemini-3-flash-preview", "gemini-3-1-pro-preview",
    "gemini-3-5-flash",  # frontier-2026 flash; native GeminiClient cpu driver
    # OpenAI — native API (OpenRouterClient pointed at api.openai.com)
    "gpt-4-1", "gpt-5-codex", "o3", "o4-mini",
    # NOTE: gpt-5.5 / gpt-5.5-pro are NOT here. ostk v7.6.0 has no native
    # OpenAI Responses-API driver, so their kernel-cpu cells fall back to the
    # generic OpenRouter (openrouter.ai) path — i.e. B* (generic_kernel), not
    # true native-driver B. Keeping them out tags kernel_arm_type correctly.
    # Mistral — native API
    "codestral-2508", "devstral-2512", "devstral-medium", "devstral-small-latest",
}


def kernel_arm_type_for(norm_model: str) -> str:
    """B vs B* labeling: native_driver (Tier-1, true B — kernel-cpu with a
    hand-written CpuDriver) vs generic_kernel (Tier-2, B* — plain kernel via
    OpenRouter, no native driver)."""
    return "native_driver" if norm_model in CPU_DRIVER_MODELS else "generic_kernel"


def duplicate_column_reason(arm: str, arm_type: str, model_arms: set) -> str | None:
    """DEDUP RULE — one execution must NEVER appear in two aggregated columns.

    native_driver models (hand-written CpuDriver): the kernel run IS the cpu
    run — a single execution. Its canonical home is the -kernel-cpu directory
    (→ the 'cpu' column). If a plain -kernel directory ALSO holds cells for
    such a model, those cells are the same single execution surfacing under a
    second column → INVALID(native_driver_duplicate_column), never aggregated.

    generic_kernel models (no native driver; OpenRouter fallback): the
    canonical home is the -kernel directory (→ the 'kernel' column). If a
    -kernel-cpu directory ALSO exists alongside -kernel, the kernel-cpu cells
    are INVALID(generic_kernel_duplicate_column). A -kernel-cpu directory
    ALONE (gpt-5.5: requested `--arm kernel --driver cpu`, executed via the
    generic fallback) is accepted as the model's single ostk execution and is
    surfaced under the 'ostk' view with source_arm='cpu' plus a note.

    Presentation side: each model aggregate carries ONE 'ostk' treatment view
    (a source_arm-annotated alias of exactly one underlying column, never a
    sum), killing the misleading kernel-0/0 presentation for native_driver
    models. See consolidate_all."""
    if arm_type == "native_driver" and arm == "kernel" and "kernel-cpu" in model_arms:
        return cell_validity.REASON_NATIVE_DRIVER_DUP
    if arm_type == "generic_kernel" and arm == "kernel-cpu" and "kernel" in model_arms:
        return cell_validity.REASON_GENERIC_KERNEL_DUP
    return None


def load_teardown_sidecar(dir_path: Path) -> frozenset:
    """TEARDOWN VISIBILITY: benches in this run dir whose cells were scored by
    the journal+SIGKILL watchdog after a kernel teardown hang.

    Source of truth is the score payload's own `teardown_masked` field when a
    writer stamps it; for historical runs (whose payloads predate the field)
    annotate_teardown.py derives the flag from the watchdog line in the run
    logs and writes a `_teardown_masked.json` sidecar next to the scores.
    Returns the set of masked benchmark names (empty when no sidecar)."""
    fp = dir_path / "_teardown_masked.json"
    if not fp.exists():
        return frozenset()
    try:
        data = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN: unreadable teardown sidecar {fp} — {e}", file=sys.stderr)
        return frozenset()
    benches = data.get("benchmarks")
    if isinstance(benches, dict):
        return frozenset(benches.keys())
    if isinstance(benches, list):
        return frozenset(benches)
    return frozenset()


# →2062 SCHEMA PARITY cache multipliers (relative to the model's INPUT rate).
# Both arms now store ATOMIC token buckets (fresh / cache_read / cache_create
# +5m/1h / output) with identical meaning; cost is SUMMED here from those
# buckets, never read from a pre-folded field. Anthropic prompt-caching rates:
#   fresh (uncached) input ...... 1.00x input rate
#   cache_read (cache hit) ...... 0.10x input rate
#   cache_create 5m write ....... 1.25x input rate
#   cache_create 1h write ....... 2.00x input rate
CACHE_MULT = {"fresh": 1.0, "cache_read": 0.10, "cache_create_5m": 1.25, "cache_create_1h": 2.00}


def compute_cost_ex(entry: dict, model: str) -> tuple[float, str]:
    """Compute (cost, basis) by SUMMING the atomic token buckets at their
    per-bucket rate, with EXPLICIT fallbacks for the writer quirks that used
    to invert the cost comparison (commit bb4aa6d5 "aggregator is broken
    (inverts cost)").

    Bases (every published cell carries one, so mixed-basis boards are visible):
      bucket-split     — full fresh/cache_read/cache_create split priced per
                         bucket (fresh 1x, read 0.1x, create 5m 1.25x / 1h 2x).
      self-reported    — token buckets empty but the harness recorded a real
                         provider-billed estimated_cost_usd (opencode).
                         PREFERRED over total-as-fresh in the present-but-zero
                         branch: pricing input_tokens at the full rate card
                         while a provider-billed number sits in the same
                         payload SYNTHESIZES cost (deepseek native published
                         29x its billed truth, flattering the kernel on the
                         published $/task ratio). The legacy branch always
                         preferred est; this branch now matches. NOTE: cells
                         of the zero-billed harness-incomplete class are
                         INVALID upstream (cell_validity zero_billed) and
                         normally never reach pricing at all.
      total-as-fresh   — PRESENT-BUT-ZERO fix (inversion leg 1): records carry
                         fresh=0/cr=0/cc=0 with the real total in
                         `input_tokens` and NO provider-billed cost; pricing
                         the empty buckets collapsed cost to output-only ≈ $0.
                         Price input_tokens as all-fresh instead — an explicit
                         over-stating FLOOR, used only when no provider-billed
                         number exists.
      self-reported-legacy — pre-→2062 record without split buckets: its
                         `input_tokens` is a cache-FOLDED total, so pricing it
                         at the full 1.0x input rate over-states cache-heavy
                         arms ~10x (inversion leg 2). Prefer the harness's own
                         cache-aware number when present.
      folded-as-fresh  — legacy record with tokens but no self-reported cost:
                         priced at full rate, flagged as an over-stating floor.
      output-only      — only output accounting exists; priced and flagged
                         (such cells are normally INVALID upstream anyway).
      unpriced         — nothing priceable. NEVER silently published as a real
                         $0: cells that should have spend are INVALID upstream
                         (cell_validity), and 'unpriced' marks the remainder.
    """
    est = entry.get("estimated_cost_usd", 0) or 0.0
    rate = RATE_CARD.get(model)
    if not rate:
        # No rate card → the harness's own number, visibly flagged; else unpriced.
        return (est, "self-reported") if est > 0 else (0.0, "unpriced")
    in_rate, out_rate = rate
    fresh = entry.get("fresh_input_tokens")
    tout = entry.get("output_tokens", 0) or 0
    if fresh is None:
        # Legacy path: no split fields at all. `input_tokens` may be a
        # pre-→2062 cache-FOLDED total (inversion leg 2 — see docstring).
        tin = entry.get("input_tokens", 0) or 0
        if est > 0:
            return est, "self-reported-legacy"
        if tin == 0 and tout == 0:
            return 0.0, "unpriced"
        return (tin * in_rate + tout * out_rate) / 1_000_000, "folded-as-fresh"
    fresh = fresh or 0
    cache_read = entry.get("cache_read_tokens", 0) or 0
    cc_5m = entry.get("cache_create_5m_tokens", 0) or 0
    cc_1h = entry.get("cache_create_1h_tokens", 0) or 0
    cc_total = entry.get("cache_create_tokens", 0) or 0
    if fresh == 0 and cache_read == 0 and cc_total == 0:
        # PRESENT-BUT-ZERO (inversion leg 1 — see docstring). A provider-billed
        # cost in the same payload beats rate-card synthesis (same ordering as
        # the legacy branch); total-as-fresh is the no-est fallback floor.
        if est > 0:
            return est, "self-reported"
        tin = entry.get("input_tokens", 0) or 0
        if tin > 0:
            return (tin * in_rate + tout * out_rate) / 1_000_000, "total-as-fresh"
        if tout > 0:
            return (tout * out_rate) / 1_000_000, "output-only"
        return 0.0, "unpriced"
    # Full split present → price every bucket explicitly.
    # If 5m/1h aren't split out, treat all cache_create as 5m (the common case).
    cc_unsplit = max(0, cc_total - cc_5m - cc_1h)
    cost_input = (
        fresh * CACHE_MULT["fresh"]
        + cache_read * CACHE_MULT["cache_read"]
        + cc_5m * CACHE_MULT["cache_create_5m"]
        + cc_1h * CACHE_MULT["cache_create_1h"]
        + cc_unsplit * CACHE_MULT["cache_create_5m"]
    ) * in_rate
    return (cost_input + tout * out_rate) / 1_000_000, "bucket-split"


def compute_cost(entry: dict, model: str) -> float:
    """Backward-compatible wrapper: cost only (see compute_cost_ex)."""
    return compute_cost_ex(entry, model)[0]


def total_input_tokens(entry: dict) -> tuple[int, bool]:
    """Grand-total input tokens (fresh + cache_read + cache_create), aware of
    both writer quirks. Post-→2062 `input_tokens` is FRESH-ONLY, so the old
    input+output aggregate omitted all cache traffic and inverted the token
    comparison (native fresh≈1k vs kernel 4k while real billed was 125k vs
    66k). Returns (total, split_present); split_present=False means the count
    came from the unsplit `input_tokens` field (legacy or present-but-zero)."""
    fresh = entry.get("fresh_input_tokens")
    if fresh is None:
        return (entry.get("input_tokens", 0) or 0), False
    total = (fresh or 0) + (entry.get("cache_read_tokens", 0) or 0) + (entry.get("cache_create_tokens", 0) or 0)
    tin = entry.get("input_tokens", 0) or 0
    if total == 0 and tin > 0:
        # PRESENT-BUT-ZERO: real total lives in input_tokens (no true split).
        return tin, False
    return total, True


def load_score(fpath: Path) -> tuple[dict | None, str | None]:
    """Load a score JSON file → (entry, skip_reason).

    Returns (entry, None) on success; (None, reason) otherwise, where reason is
      'policy_excluded'      — PUBLISHED_EXCLUDE bench (kept on disk, never
                               published; counted separately, NOT invalid)
      'malformed_payload:…'  — unparseable JSON / not a score dict.

    F7: if a sibling `<bench>.samples.json` exists, fold majority-vote +
    variance from the samples sidecar into the returned entry.

    NOTE: the old silent quality gate (deadline / zero-work cells returning
    None) moved to cell_validity.classify_cell — those cells are now VISIBLY
    INVALID and counted per model/arm instead of vanishing (fail-closed).
    """
    try:
        with open(fpath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN: malformed score {fpath} — {e}", file=sys.stderr)
        return None, f"{cell_validity.REASON_MALFORMED}:{type(e).__name__}"
    if not isinstance(data, dict) or "benchmark" not in data:
        return None, cell_validity.REASON_MALFORMED
    # Published-exclude: flaky + stale-oracle benches kept on disk but never
    # entered into published aggregates (REPORT §2 Appendix lists them raw).
    if data.get("benchmark") in PUBLISHED_EXCLUDE:
        return None, "policy_excluded"
    samples_fp = fpath.with_suffix("").with_suffix(".samples.json")
    # `with_suffix` on `<bench>.score.json` strips `.json` then `.score`,
    # leaving `<bench>`; re-add `.samples.json` explicitly.
    samples_fp = fpath.parent / (fpath.name.removesuffix(".score.json") + ".samples.json")
    if samples_fp.exists():
        try:
            samples = json.loads(samples_fp.read_text())
            if isinstance(samples, list) and samples:
                resolved_count = sum(1 for s in samples if s.get("resolved"))
                n = len(samples)
                data["samples_count"] = n
                data["samples_resolved_count"] = resolved_count
                data["resolution_rate"] = resolved_count / n
                data["resolved"] = resolved_count * 2 > n  # strict majority
                turns = [s.get("turns_to_fix", 0) or 0 for s in samples]
                walls = [s.get("wall_clock", 0) or 0 for s in samples]
                if turns:
                    data["turns_min"] = min(turns)
                    data["turns_max"] = max(turns)
                    data["turns_mean"] = sum(turns) / n
                if walls:
                    data["wall_clock_min"] = min(walls)
                    data["wall_clock_max"] = max(walls)
                    data["wall_clock_mean"] = sum(walls) / n
        except Exception as e:
            print(f"  WARN: samples sidecar unreadable {samples_fp} — {e}", file=sys.stderr)
    return data, None


def arm_summary(entry: dict, model: str) -> dict:
    """Extract per-arm summary fields from a score entry."""
    cost, basis = compute_cost_ex(entry, model)
    billed_input, _split = total_input_tokens(entry)
    return {
        "resolved": bool(entry.get("resolved", False)),
        "turns": entry.get("turns_to_fix", 0) or 0,
        "input_tokens": entry.get("input_tokens", 0) or 0,
        "output_tokens": entry.get("output_tokens", 0) or 0,
        # Grand-total input incl. cache traffic (present-but-zero aware) — the
        # honest token column; `input_tokens` above is fresh-only post-→2062.
        "billed_input_tokens": billed_input,
        "token_cost": entry.get("token_cost", 0) or 0,
        "cost_usd": round(cost, 6),
        "cost_basis": basis,
        "teardown_masked": bool(entry.get("teardown_masked", False)),
        "tool_uses": entry.get("tool_uses", 0) or 0,
        "wall_clock": entry.get("wall_clock", 0) or 0,
        "summary": entry.get("summary", ""),
        "stop_reason": entry.get("stop_reason", ""),
    }


def consolidate_all(dry_run: bool = False) -> None:
    """Main consolidation: walk runs/, produce scores.json + experiment-scores.json."""
    if not RUNS_DIR.exists():
        print(f"ERROR: {RUNS_DIR} not found", file=sys.stderr)
        sys.exit(1)

    # Discover arm directories
    arm_dirs: list[tuple[str, str, Path]] = []  # (model, arm, path)
    arms_by_model: dict[str, set] = {}  # norm → set of arms present on disk
    for entry in sorted(RUNS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = ARM_PATTERN.match(entry.name)
        if m:
            model = m.group(1)
            arm = m.group(2)
            arm_dirs.append((model, arm, entry))
            arms_by_model.setdefault(normalize_model(model), set()).add(arm)

    print(f"Found {len(arm_dirs)} arm directories in {RUNS_DIR}")

    # Load all scores.
    # scores.json contract (module docstring): ONE entry per (model, bench,
    # arm) — best by the same keep-best rule as the board (resolved > not,
    # then fewer turns). Multiple score files for one cell (e.g. a canonical
    # payload plus a *.recovered-june16.score.json twin) collapse here so no
    # consumer summing the flat file ever double-counts a cell.
    best_scores: dict[tuple[str, str, str], dict] = {}  # (norm, bench, arm_key) → entry
    # Keyed by (normalized_model, benchmark) → {model, benchmark, native, kernel, cpu}
    experiments: dict[tuple[str, str], dict] = {}

    def keeps_best(new: dict, cur: dict | None) -> bool:
        """Keep-best rule shared with the board cells: resolved > not, then
        fewer turns (ties keep the incumbent — deterministic walk order)."""
        if cur is None:
            return True
        new_res, cur_res = bool(new.get("resolved")), bool(cur.get("resolved"))
        if new_res != cur_res:
            return new_res
        return (new.get("turns_to_fix") or 0) < (cur.get("turns_to_fix") or 0)

    # ---- fail-closed bookkeeping (INVALID ≠ absent) ----
    invalid_cells: list[dict] = []              # visible per-cell records
    invalid_counts: Counter = Counter()         # (norm, arm_key) → count
    invalid_reasons: Counter = Counter()        # reason code → count
    policy_excluded = 0                         # PUBLISHED_EXCLUDE benches (not invalid)
    display_name: dict[str, str] = {}           # norm → model spelling on disk

    total_files = 0
    for model, arm, dir_path in arm_dirs:
        norm = normalize_model(model)
        display_name.setdefault(norm, model)
        arm_key = "cpu" if arm == "kernel-cpu" else arm
        arm_type = kernel_arm_type_for(norm)
        # DEDUP RULE: see duplicate_column_reason — applies to every cell in
        # a non-canonical ostk column when the canonical one also exists.
        dup_reason = duplicate_column_reason(arm, arm_type, arms_by_model[norm])
        masked_set = load_teardown_sidecar(dir_path)

        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(".score.json"):
                continue
            fpath = dir_path / fname
            entry, skip_reason = load_score(fpath)
            if skip_reason == "policy_excluded":
                policy_excluded += 1
                continue

            bench_name = fname.removesuffix(".score.json")
            if entry is None:
                reasons = [skip_reason or cell_validity.REASON_MALFORMED]
            else:
                bench_name = entry["benchmark"]
                # TEARDOWN VISIBILITY: payload field wins; else the sidecar
                # derived from the watchdog line in the run logs.
                entry["teardown_masked"] = bool(
                    entry.get("teardown_masked") or bench_name in masked_set
                )
                cls = cell_validity.classify_cell(entry, requested_arm=arm)
                reasons = list(cls.reasons)
                if dup_reason:
                    reasons.append(dup_reason)

            if reasons:
                # INVALID: excluded from ALL aggregates, counted visibly.
                invalid_cells.append({
                    "model": model,
                    "model_normalized": norm,
                    "arm": arm_key,
                    "benchmark": bench_name,
                    "file": str(fpath.relative_to(RUNS_DIR.parent)),
                    "reasons": reasons,
                    "teardown_masked": bool(entry.get("teardown_masked")) if entry else False,
                })
                invalid_counts[(norm, arm_key)] += 1
                for r in reasons:
                    invalid_reasons[r.split(":", 1)[0]] += 1
                continue

            total_files += 1

            benchmark = entry["benchmark"]
            cost, cost_basis = compute_cost_ex(entry, model)
            entry["computed_cost_usd"] = cost
            entry["cost_basis"] = cost_basis

            # Flat scores list — best-per-cell (see contract note above).
            sk = (norm, benchmark, arm_key)
            if keeps_best(entry, best_scores.get(sk)):
                best_scores[sk] = entry

            # Experiment grouping
            ek = (norm, benchmark)
            if ek not in experiments:
                experiments[ek] = {
                    "model": model,
                    "model_normalized": norm,
                    "benchmark": benchmark,
                    "native": None,
                    "kernel": None,
                    "cpu": None,
                }

            current = experiments[ek][arm_key]
            summary = arm_summary(entry, model)

            # Keep best: resolved > not, then fewer turns
            if current is None:
                experiments[ek][arm_key] = summary
            elif summary["resolved"] and not current["resolved"]:
                experiments[ek][arm_key] = summary
            elif summary["resolved"] == current["resolved"] and summary["turns"] < current["turns"]:
                experiments[ek][arm_key] = summary

    # Materialize the deduped flat list (insertion order = walk order).
    all_scores = list(best_scores.values())

    # Build experiment output with model aggregates
    exp_list = sorted(experiments.values(), key=lambda e: (e["model"], e["benchmark"]))

    # Compute per-model aggregates for the experiment data
    def _empty_arm_agg() -> dict:
        return {"solved": 0, "total": 0, "cost": 0, "tokens": 0, "tokens_fresh": 0,
                "tools": 0, "turns": 0, "wall": 0, "invalid": 0, "masked": 0,
                "cost_bases": {}}

    def _empty_model_agg(model: str, norm: str) -> dict:
        return {
            "model": model,
            "model_normalized": norm,
            "benchmarks": 0,
            "native": _empty_arm_agg(),
            "kernel": _empty_arm_agg(),
            "cpu": _empty_arm_agg(),
        }

    model_agg: dict[str, dict] = {}
    for exp in exp_list:
        norm = exp["model_normalized"]
        if norm not in model_agg:
            model_agg[norm] = _empty_model_agg(exp["model"], norm)
        model_agg[norm]["benchmarks"] += 1
        for arm_key in ["native", "kernel", "cpu"]:
            arm_data = exp[arm_key]
            if arm_data is not None:
                agg = model_agg[norm][arm_key]
                agg["total"] += 1
                if arm_data["resolved"]:
                    agg["solved"] += 1
                agg["cost"] += arm_data["cost_usd"]
                # tokens = grand-total input INCL. cache traffic + output (the
                # old input+output was fresh-only post-→2062 and inverted the
                # Anthropic token comparison); tokens_fresh keeps the old
                # fresh-only number for continuity/debugging.
                agg["tokens"] += arm_data["billed_input_tokens"] + arm_data["output_tokens"]
                agg["tokens_fresh"] += arm_data["input_tokens"] + arm_data["output_tokens"]
                agg["tools"] += arm_data["tool_uses"]
                agg["turns"] += arm_data["turns"]
                agg["wall"] += arm_data["wall_clock"]
                if arm_data["teardown_masked"]:
                    agg["masked"] += 1
                basis = arm_data.get("cost_basis", "unpriced")
                agg["cost_bases"][basis] = agg["cost_bases"].get(basis, 0) + 1

    # Attach visible INVALID counts (a model whose cells are ALL invalid still
    # gets a row — invisible exclusion is exactly what fail-closed forbids).
    for (norm, arm_key), count in invalid_counts.items():
        if norm not in model_agg:
            model_agg[norm] = _empty_model_agg(display_name.get(norm, norm), norm)
        model_agg[norm][arm_key]["invalid"] = count

    # Add rate card info + CPU driver status
    for norm, agg in model_agg.items():
        rate = RATE_CARD.get(agg["model"])
        if rate:
            agg["price_per_m_input"] = rate[0]
            agg["price_per_m_output"] = rate[1]

        # CPU driver status: "tested" if driver exists and has real results,
        # "no_driver" if no native driver exists for this provider
        if norm in CPU_DRIVER_MODELS:
            agg["cpu_driver_status"] = "tested"
        else:
            agg["cpu_driver_status"] = "no_driver"

        # B vs B* labeling: a model's consolidated "B" column sources kernel-cpu
        # (true native-driver B) when the model is in CPU_DRIVER_MODELS, else
        # plain kernel (generic OpenRouter B*). Drives the leaderboard B/B*
        # distinction (index.astro).
        #   native_driver  → Tier-1, true B   (kernel-cpu, hand-written CpuDriver)
        #   generic_kernel → Tier-2, B*       (kernel, OpenRouter, no native driver)
        agg["kernel_arm_type"] = kernel_arm_type_for(norm)

        # ---- THE 'ostk' VIEW: one ostk-treatment column per model ----
        # For native_driver models one execution serves BOTH kernel and cpu
        # (kernel ≡ kernel-cpu); presenting a structurally-empty twin as 0/0
        # read as "ran 40, solved 0". The 'ostk' key is an ALIAS of exactly one
        # underlying arm column (source_arm), never a sum — one execution can
        # never appear in two aggregated columns (duplicate_column_reason
        # invalidates non-canonical-column cells at ingest, so at most one of
        # kernel/cpu is ever populated per model).
        ktot, ctot = agg["kernel"]["total"], agg["cpu"]["total"]
        if ktot and ctot:
            # Structurally unreachable (ingest dedup); guard anyway, fail closed.
            print(f"  WARN: {norm} has cells in BOTH kernel and cpu columns after "
                  f"dedup — ostk view sources the canonical column only", file=sys.stderr)
        if agg["kernel_arm_type"] == "native_driver":
            src = "cpu" if ctot else ("kernel" if ktot else "cpu")
        else:
            src = "kernel" if ktot else ("cpu" if ctot else "kernel")
        ostk = dict(agg[src])
        ostk["source_arm"] = src
        ostk["arm_type"] = agg["kernel_arm_type"]
        if agg["kernel_arm_type"] == "generic_kernel" and src == "cpu":
            # gpt-5.5 case: requested `--arm kernel --driver cpu` but v7.6.0 has
            # no native driver → executed via the generic fallback. One
            # execution, sourced from the kernel-cpu directory, flagged.
            ostk["note"] = ("requested-vs-executed arm mismatch: no native driver; "
                            "executed via generic fallback — single execution "
                            "sourced from the kernel-cpu directory")
        agg["ostk"] = ostk
        # Kill the misleading 0/0: annotate the structurally-empty twin column.
        for twin in ("kernel", "cpu"):
            if twin != src and agg[twin]["total"] == 0:
                agg[twin]["status"] = (
                    f"not_applicable — ostk treatment sourced from '{src}' "
                    f"(kernel_arm_type={agg['kernel_arm_type']}; see 'ostk')")

        # Native CLI label — what actually runs for --arm native
        NATIVE_CLI_MAP = {
            "claude-haiku-4-5": "claude-code", "claude-sonnet-4-6": "claude-code", "claude-opus-4-6": "claude-code",
            "claude-opus-4-8": "claude-code", "claude-fable-5": "claude-code",
            "gemini-2-5-flash": "gemini-cli", "gemini-2-5-pro": "gemini-cli",
            "gemini-3-flash-preview": "gemini-cli", "gemini-3-1-pro-preview": "gemini-cli",
            "gpt-4-1": "codex", "gpt-5-codex": "codex", "o4-mini": "codex",
            "devstral-2512": "vibe", "devstral-medium": "vibe", "devstral-small-latest": "vibe",
            "kimi-k2-5": "kimi-cli",
        }
        agg["native_cli"] = NATIVE_CLI_MAP.get(norm, "opencode")

    agg_list = sorted(model_agg.values(), key=lambda a: (
        -max(a["native"]["solved"]/max(a["native"]["total"],1),
             a["kernel"]["solved"]/max(a["kernel"]["total"],1),
             a["cpu"]["solved"]/max(a["cpu"]["total"],1)),
        a["model"]
    ))

    # Summary
    models = sorted(set(e["model_normalized"] for e in exp_list))
    benchmarks = sorted(set(e["benchmark"] for e in exp_list))

    print(f"Loaded {total_files} score files")
    print(f"Models: {len(models)}")
    print(f"Benchmarks: {len(benchmarks)}")
    print(f"Experiment entries: {len(exp_list)}")
    print(f"Model aggregates: {len(agg_list)}")

    # Fail-closed accounting — INVALID is visible, absent is silent.
    masked_total = sum(agg[a]["masked"] for agg in model_agg.values()
                       for a in ("native", "kernel", "cpu"))
    print(f"\nValidity: {total_files} VALID, {len(invalid_cells)} INVALID "
          f"(excluded from aggregates, counted per model/arm), "
          f"{policy_excluded} policy-excluded (PUBLISHED_EXCLUDE)")
    if invalid_reasons:
        for reason, n in sorted(invalid_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  INVALID {reason}: {n}")
    print(f"Teardown-masked cells (watchdog-scored, published with flag): {masked_total}")

    # Grand totals (VALID cells, best-per-cell deduped; tokens incl. cache traffic)
    grand_solved = sum(1 for s in all_scores if s.get("resolved"))
    grand_cost = sum(s.get("computed_cost_usd", 0) for s in all_scores)
    grand_tokens = sum(total_input_tokens(s)[0] + (s.get("output_tokens", 0) or 0) for s in all_scores)
    print(f"\nGrand totals: {grand_solved}/{len(all_scores)} solved, "
          f"{grand_tokens:,} tokens, ${grand_cost:,.2f} estimated cost")

    if dry_run:
        print(f"\n[dry-run] Would write {len(all_scores)} scores + {len(exp_list)} experiments + {len(agg_list)} aggregates")
        return

    # Write scores.json
    SCORES_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(SCORES_OUTPUT, "w") as f:
        json.dump(all_scores, f, indent=2)
        f.write("\n")
    print(f"\nWrote {len(all_scores)} entries to {SCORES_OUTPUT}")

    # Write experiment-scores.json with both per-benchmark and aggregates
    experiment_output = {
        "generated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        # Fail-closed doctrine block: INVALID cells are excluded from every
        # aggregate above but counted here (and per model/arm as
        # models[].{native,kernel,cpu}.invalid) — never a silent 0.
        "trust": {
            "doctrine": "fail-closed: malformed / arm-mismatch / deadline / "
                        "zero-work / zero-token-accounting cells are INVALID — "
                        "excluded from aggregates, counted visibly. "
                        "absent = never run (not invalid).",
            "valid_cells": total_files,
            "invalid_cells": len(invalid_cells),
            "policy_excluded_cells": policy_excluded,
            "invalid_by_reason": dict(sorted(invalid_reasons.items())),
            "teardown_masked_cells": masked_total,
        },
        "invalid_cells": invalid_cells,
        "models": agg_list,
        "benchmarks": exp_list,
    }
    with open(EXPERIMENT_OUTPUT, "w") as f:
        json.dump(experiment_output, f, indent=2)
        f.write("\n")
    print(f"Wrote {len(exp_list)} experiments + {len(agg_list)} model aggregates to {EXPERIMENT_OUTPUT}")


def main():
    parser = argparse.ArgumentParser(description="Consolidate needle-bench scores for the website.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing")
    args = parser.parse_args()
    consolidate_all(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
