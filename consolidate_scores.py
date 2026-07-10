#!/usr/bin/env python3
"""
consolidate_scores.py — Aggregate score files into public JSON for the website.

Walks runs/ to find all *-native/, *-kernel/, *-kernel-cpu/ directories,
loads score files, and produces:

  public/scores.json          — flat list of all scores (best per model+bench+arm)
  public/experiment-scores.json — three-arm comparison (native/kernel/kernel-cpu)

TRUST DOCTRINE (fail-closed, judged PER AXIS; see cell_validity.py):
  - Every cell is judged on two axes. SOLVE axis: oracle verdict + arm receipt
    + not stalled (malformed / no-receipt / arm-mismatch / deadline / zero-work
    / duplicate-column cells are fully INVALID — excluded from everything,
    counted visibly). COST axis: real token/cost accounting (zero token
    accounting on a completed run marks the cost axis UNAVAILABLE — the cell
    KEEPS its solve verdict, enters solve columns, is excluded from cost/token
    columns with a visible count, and is NEVER priced). absent = never run.
  - SNAPSHOTS: every cell carries a run-date snapshot (from its payload
    timestamp). The published 'latest' board is SINGLE-SNAPSHOT PER COLUMN —
    a (model, arm) column whose cells span several run dates publishes only
    its latest snapshot (no June/July keep-best mixing); earlier snapshots
    are published as their own versioned boards under boards/<ostk-version>/.
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

# ---------------------------------------------------------------------------
# Run-date snapshots (versioned publishing). A snapshot is a run-date window;
# cells are assigned by their payload timestamp. v7.6.0 has two: the June
# 15-17 original run ("june16", includes the *.recovered-june16 twins) and the
# July-2 opus/gemini kernel-cpu + opus native re-run ("july02"). 2026-07-09
# hosted THREE frozen-bin pins in one day — v7.7.1 ("july09"; fable-5 + flash
# smoke), v7.7.2 ("july09v2"), and v7.7.3 ("july09v3", the current latest).
# ---------------------------------------------------------------------------
OSTK_VERSION = "v7.7.4"                      # current binary (latest board)
# Chronological order — MUST stay oldest→newest: downstream ranking uses
# SNAPSHOTS.index (see published_snapshot_for / consolidate_all) to pick the
# newest snapshot per column, so a new pin is APPENDED, never inserted.
SNAPSHOTS = ("june16", "july02", "july09", "july09v2", "july09v3", "july10")
# F2 FIX — RECEIPT-DRIVEN SNAPSHOT IDENTITY. The cell's own bench_binary.version
# receipt is the RELIABLE attribution signal; the hand-maintained date ladder
# below can only split the v7.6.0 june16/july02 pair (same binary, different run
# windows) and is used ONLY as the fallback for cells with no known version
# receipt. Any cell whose version maps here is filed by receipt regardless of
# its payload date. v7.6.0 is intentionally ABSENT (date-ambiguous). Newest
# first for readability; matching is exact-or-dotted-prefix (see snapshot_of).
SNAPSHOT_BY_VERSION = (
    ("7.7.4", "july10"),     # 2026-07-10 pin (native OpenAI Responses driver; current latest)
    ("7.7.3", "july09v3"),   # 2026-07-09 third pin
    ("7.7.2", "july09v2"),   # 2026-07-09 second pin
    ("7.7.1", "july09"),     # 2026-07-09 first pin
)
# Run-date boundaries, newest first; a cell belongs to the first snapshot
# whose boundary its run date reaches. Dates below the oldest → SNAPSHOTS[0].
# 2026-07-09 hosted multiple binaries, so the date boundary alone cannot
# attribute that day; the version receipt (SNAPSHOT_BY_VERSION) decides. A
# receiptless 2026-07-09 cell stays in "july09" (the day's OLDEST binary) —
# fail-closed, never promoted into a newer binary it may not have executed on.
SNAPSHOT_BOUNDARIES = (
    ("2026-07-09", "july09"),
    ("2026-07-01", "july02"),
)
SNAPSHOT_RUN_DATE = {"june16": "2026-06-16", "july02": "2026-07-02",
                     "july09": "2026-07-09", "july09v2": "2026-07-09",
                     "july09v3": "2026-07-09", "july10": "2026-07-10"}
# Binary identity per snapshot: versioned boards land under
# boards/<SNAPSHOT_OSTK_VERSION[snap]>/ — a run is never re-attributed to a
# binary it did not execute on (runs/.binary_identity.jsonl is the receipt).
SNAPSHOT_OSTK_VERSION = {"june16": "v7.6.0", "july02": "v7.6.0",
                         "july09": "v7.7.1", "july09v2": "v7.7.2",
                         "july09v3": "v7.7.3", "july10": "v7.7.4"}
# Fault ids (boards/FAULTS.json) attached to each snapshot's published board.
SNAPSHOT_FAULTS = {
    "june16": ["june16-teardown-masked"],
    "july02": ["july02-cache-read-zero"],
    "july09": ["july09-fable-kernel-refusal",
               "july09-opus48-unknown-tool-loop-exit",
               "july09-f7-earlystop-truncated-votes",
               "july09-sonnet5-native-cli-oprate",
               "july09-fable5-native-opus-fallback"],
    "july09v2": ["july09-f7-earlystop-truncated-votes",
                 "july09-runcell-exit1-hardfail"],
    # v7.7.3: F7 early-stop + runcell hard-fail both fixed before this pin;
    # retry-cost accounting summed across attempts (load_score F3).
    "july09v3": [],
    # v7.7.4 (current latest): adds the native OpenAI Responses-API CpuDriver
    # (gpt-* kernel-cpu becomes a true Tier-1 native-driver arm; the old
    # generic-OpenRouter gpt kernel cells are archived, frozen boards keep
    # their history). No known era faults yet.
    "july10": [],
}
# Human-readable one-liners for the ids above; the canonical machine-readable
# era annotations (windows, commits, affected models) live in boards/FAULTS.json.
FAULT_NOTES = {
    "june16-teardown-masked":
        "June-16 era: kernel teardown hang — a subset of cells were scored by "
        "the journal+SIGKILL watchdog and publish with teardown_masked=true "
        "(counted per model/arm).",
    "july02-cache-read-zero":
        "July-2 re-run (opus/gemini-3.1 kernel-cpu + opus native): "
        "cache_read/cache_create report 0 with ALL input folded into fresh — "
        "accounting is real (cost axis valid) but carries no cache split; see "
        "boards/v7.6.0/2026-06-16-experiment-scores.json for real cache buckets.",
    "july09-fable-kernel-refusal":
        "July-9 smoke (v7.7.1): every claude-fable-5 kernel-cpu cell ends in an "
        "empty-content stop_reason=refusal after 2-3 real tool turns (native arm "
        "unaffected). Cells are VALID resolved=false with real accounting, but "
        "the unsolved verdict reflects a provider refusal on the kernel-arm "
        "prompt shape — treat fable-5 kernel-cpu solve rates as a floor.",
    "july09-opus48-unknown-tool-loop-exit":
        "July-9 (v7.7.1): 2 claude-opus-4-8 kernel-cpu cells lost to an agent-"
        "loop hard-exit on an undeclared tool request (harness defect, not a "
        "capability gap) — treat opus-4-8 kernel-cpu solve rate as a floor.",
    "july09-f7-earlystop-truncated-votes":
        "F7 resampling stopped on any mid-stream solve, locking [F,T] cells at "
        "1/2 = miss without their deciding third sample. Affected v7.7.1 cells "
        "publish as-were (solve rates are floors); harness fixed 2026-07-09 "
        "before the v7.7.2 board.",
    "july09-runcell-exit1-hardfail":
        "v7.7.2 capture: run_cell hard-failed clean completed-but-unsolved "
        "exits (unmasked by the teardown fix); one sonnet-5 kernel cell was "
        "remediated by cold re-run before publish — no published board carries "
        "a mis-scored cell. Harness fixed 2026-07-09.",
    "july09-sonnet5-native-cli-oprate":
        "claude-sonnet-5 native COST axis only: the vendor CLI prices "
        "sonnet-5 at the Opus 4.8 rate card — recorded $17.51 vs honest "
        "$7.58 intro / $11.36 standard. Tokens and solve axis unaffected; "
        "reprice from raw telemetry via scripts/reprice_native.py.",
    "july09-fable5-native-opus-fallback":
        "claude-fable-5 native: 4 security cells silently continued on "
        "claude-opus-4-8 after a mid-session classifier stop — solve "
        "attribution for those cells is not pure fable-5 (see FAULTS.json "
        "for the cell list).",
}


def snapshot_for_version(version: str) -> str | None:
    """Map a bench_binary.version receipt to its snapshot, or None when the
    version is empty or not one we recognise. Matching is exact-or-dotted-prefix
    so a build-suffixed receipt (e.g. '7.7.3.1') still resolves while '7.7.30'
    does NOT collide with '7.7.3'."""
    v = (version or "").strip()
    if not v:
        return None
    for prefix, snap in SNAPSHOT_BY_VERSION:
        if v == prefix or v.startswith(prefix + "."):
            return snap
    return None


def snapshot_of(entry: dict | None, fpath: Path | None = None) -> str:
    """Snapshot for a cell — RECEIPT-FIRST (F2 fix).

    The cell's own bench_binary.version receipt is the reliable attribution
    signal, so any cell carrying a version that maps to a known snapshot
    (SNAPSHOT_BY_VERSION) is filed by that receipt REGARDLESS of its payload
    date. This is what keeps a 2026-07-09 v7.7.3 cell out of the v7.7.1
    "july09" bucket (the old date-ladder special-case only knew v7.7.2).

    The date ladder is the FALLBACK for cells with no known version receipt —
    it distinguishes the v7.6.0 june16/july02 pair (one binary, two run windows,
    so a receipt cannot split them) and, fail-closed, keeps a receiptless
    2026-07-09 cell in "july09" (the day's OLDEST binary). A receiptless cell
    is NEVER promoted into a newer binary it may not have executed on.

    Payload timestamp drives the date ladder; file mtime is the last-resort
    fallback for malformed payloads with no timestamp to trust."""
    # 1. Version receipt — the reliable signal. Wins over the date ladder.
    if isinstance(entry, dict):
        snap = snapshot_for_version(
            str((entry.get("bench_binary") or {}).get("version", "") or ""))
        if snap is not None:
            return snap
    # 2. Fail-closed date ladder for receiptless / unknown-version cells.
    ts = (entry.get("timestamp") or "") if isinstance(entry, dict) else ""
    if not ts and fpath is not None:
        import datetime
        ts = datetime.datetime.fromtimestamp(
            fpath.stat().st_mtime, datetime.timezone.utc).isoformat()
    for boundary, snap in SNAPSHOT_BOUNDARIES:
        if ts[:10] >= boundary:
            return snap
    return SNAPSHOTS[0]

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
    # gpt-5.6 family (GA 2026-07-09): Sol $5/$30, Terra $2.50/$15, Luna $1/$6.
    "gpt-5.6": (5.00, 30.00), "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-terra": (2.50, 15.00), "gpt-5.6-luna": (1.00, 6.00),
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
    "claude-sonnet-5",  # frontier-2026 Tier-1: native CpuDriver via AnthropicClient
                        # (same route as opus-4-8 / fable-5). F6 fix — was
                        # mislabeled generic_kernel (no driver) on the board.
    # Google — native Gemini API
    # gemini-3-1-pro-preview is the frontier-2026 Tier-1 entry (also pre-existing).
    "gemini-2-5-flash", "gemini-2-5-pro", "gemini-3-flash-preview", "gemini-3-1-pro-preview",
    "gemini-3-5-flash",  # frontier-2026 flash; native GeminiClient cpu driver
    # OpenAI — native API (OpenRouterClient pointed at api.openai.com)
    "gpt-4-1", "gpt-5-codex", "o3", "o4-mini",
    # gpt-5.5 / gpt-5.6: Tier-1 as of v7.7.4 — haystack ships a native OpenAI
    # Responses-API CpuDriver (src/cpu/openai.rs, POST /v1/responses,
    # reasoning.effort=high, encrypted_content round-trip). The pre-v7.7.4
    # generic-OpenRouter gpt-5.5 kernel cells were archived
    # (runs-archive/gpt-5.5-*-v760-*); frozen boards keep their B* history.
    "gpt-5-5", "gpt-5-6",
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

# F6-broad fix: cache economics are PER-PROVIDER — the Anthropic multipliers
# above must not be applied to every model. OpenAI (Responses API, implicit
# prefix caching): reads 0.10x like Anthropic, but the WRITE premium is 1.25x
# only from the gpt-5.6 family onward (5.5 and earlier bill writes at 1.0x —
# no explicit cache-write fee), and there is no 5m/1h TTL split at all.
# Anthropic/other models keep CACHE_MULT unchanged (existing boards byte-stable).
CACHE_MULT_OPENAI_PRE56 = {"fresh": 1.0, "cache_read": 0.10, "cache_create_5m": 1.00, "cache_create_1h": 1.00}
CACHE_MULT_OPENAI_56 = {"fresh": 1.0, "cache_read": 0.10, "cache_create_5m": 1.25, "cache_create_1h": 1.25}


def cache_mult_for(model: str) -> dict:
    """Per-provider cache multipliers (see F6-broad note above)."""
    m = model.lower()
    if m.startswith("gpt-5.6") or m.startswith("gpt-5-6"):
        return CACHE_MULT_OPENAI_56
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return CACHE_MULT_OPENAI_PRE56
    return CACHE_MULT


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
    # Multipliers are per-provider (cache_mult_for — F6-broad fix).
    mult = cache_mult_for(model)
    cc_unsplit = max(0, cc_total - cc_5m - cc_1h)
    cost_input = (
        fresh * mult["fresh"]
        + cache_read * mult["cache_read"]
        + cc_5m * mult["cache_create_5m"]
        + cc_1h * mult["cache_create_1h"]
        + cc_unsplit * mult["cache_create_5m"]
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

    F3 — RETRY-COST TRUTH: F7 runs up to 3 samples, resampling only after a
    miss, and the canonical `<bench>.score.json` mirrors only the FINAL attempt.
    So an initially-failing cell would publish just its last try's spend while a
    first-pass success publishes its single full spend — non-comparable $/task.
    When N>1 attempts exist, the token & cost fields are OVERWRITTEN with the
    TRUE TOTAL SPEND summed across every attempt (priced downstream on the same
    per-bucket basis as a single-shot cell); the final-attempt values are kept
    under `final_attempt_*` and the count under `retry_attempts`. Resolution and
    turns remain majority-vote / final (unchanged).

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
                if n > 1:
                    # F3 — fold TOTAL retry spend (sum across attempts) into the
                    # token & cost fields. compute_cost_ex / total_input_tokens
                    # then price the full spend from the summed atomic buckets on
                    # the same basis as a single-shot cell. A field is summed
                    # only when at least one attempt reports a non-None value, so
                    # an all-None `fresh_input_tokens` is left untouched (keeps
                    # compute_cost_ex on its legacy path, no present-but-zero
                    # flip). None is treated as 0 within the sum.
                    TOTAL_SPEND_FIELDS = (
                        "input_tokens", "output_tokens", "token_cost",
                        "estimated_cost_usd", "fresh_input_tokens",
                        "cache_read_tokens", "cache_create_tokens",
                        "cache_create_5m_tokens", "cache_create_1h_tokens",
                        "tool_uses",
                    )
                    data["retry_attempts"] = n
                    for field in TOTAL_SPEND_FIELDS:
                        if not any(s.get(field) is not None for s in samples):
                            continue
                        data[f"final_attempt_{field}"] = data.get(field)
                        data[field] = sum((s.get(field) or 0) for s in samples)
        except Exception as e:
            print(f"  WARN: samples sidecar unreadable {samples_fp} — {e}", file=sys.stderr)
    return data, None


def arm_summary(entry: dict, model: str, cost_valid: bool = True,
                cost_reasons: list | None = None) -> dict:
    """Extract per-arm summary fields from a score entry.

    AXIS SPLIT: when the cell's cost axis is unavailable (cost_valid=False)
    the solve fields stand but cost_usd is None with cost_basis='unavailable'
    — never a rate-card synthesis over untrustworthy accounting. Raw token
    fields are passed through as recorded (they are the payload), flagged by
    cost_valid=False so no consumer sums them into a cost aggregate."""
    if cost_valid:
        cost, basis = compute_cost_ex(entry, model)
        cost_usd = round(cost, 6)
    else:
        cost_usd, basis = None, "unavailable"
    billed_input, _split = total_input_tokens(entry)
    out = {
        "resolved": bool(entry.get("resolved", False)),
        "turns": entry.get("turns_to_fix", 0) or 0,
        "input_tokens": entry.get("input_tokens", 0) or 0,
        "output_tokens": entry.get("output_tokens", 0) or 0,
        # Grand-total input incl. cache traffic (present-but-zero aware) — the
        # honest token column; `input_tokens` above is fresh-only post-→2062.
        "billed_input_tokens": billed_input,
        "token_cost": entry.get("token_cost", 0) or 0,
        "cost_usd": cost_usd,
        "cost_basis": basis,
        "cost_valid": cost_valid,
        "teardown_masked": bool(entry.get("teardown_masked", False)),
        "tool_uses": entry.get("tool_uses", 0) or 0,
        "wall_clock": entry.get("wall_clock", 0) or 0,
        "summary": entry.get("summary", ""),
        "stop_reason": entry.get("stop_reason", ""),
    }
    if not cost_valid and cost_reasons:
        out["cost_unavailable_reasons"] = list(cost_reasons)
    return out


# Native CLI actually used per model (the harness that runs --arm native).
NATIVE_CLI_MAP = {
    "claude-haiku-4-5": "claude-code", "claude-sonnet-4-6": "claude-code", "claude-opus-4-6": "claude-code",
    "claude-opus-4-8": "claude-code", "claude-fable-5": "claude-code",
    "claude-sonnet-5": "claude-code",
    "gemini-2-5-flash": "gemini-cli", "gemini-2-5-pro": "gemini-cli",
    "gemini-3-flash-preview": "gemini-cli", "gemini-3-1-pro-preview": "gemini-cli",
    "gemini-3-5-flash": "gemini-cli",
    "gpt-4-1": "codex", "gpt-5-codex": "codex", "o4-mini": "codex", "gpt-5-5": "codex",
    "gpt-5-6": "codex", "gpt-5-6-sol": "codex",
    "devstral-2512": "vibe", "devstral-medium": "vibe", "devstral-small-latest": "vibe",
    "kimi-k2-5": "kimi-cli", "kimi-k2-6": "kimi",
}


def collect_cells() -> tuple[list[dict], dict, dict]:
    """Walk runs/ and classify every score file — THE single judgment pass.

    Returns (cells, policy_excluded, display_name). Each cell dict carries:
      model, norm, arm (dir suffix), arm_key (native|kernel|cpu), benchmark,
      file (repo-relative), entry (payload dict | None), cls
      (cell_validity.Classification with dedup reasons folded in), snapshot,
      teardown_masked.

    The public board, the versioned snapshot boards, and the board-v760 /
    cells-v760 adapters (publish_boards.py) ALL consume this — no sibling
    pipeline may re-judge cells (consolidate_harden.py / emit_cells.py are
    retired and refuse to run).
    """
    if not RUNS_DIR.exists():
        print(f"ERROR: {RUNS_DIR} not found", file=sys.stderr)
        sys.exit(1)

    arm_dirs: list[tuple[str, str, Path]] = []  # (model, arm, path)
    arms_by_model: dict[str, set] = {}  # norm → set of arms present on disk
    for entry in sorted(RUNS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = ARM_PATTERN.match(entry.name)
        if m:
            arm_dirs.append((m.group(1), m.group(2), entry))
            arms_by_model.setdefault(normalize_model(m.group(1)), set()).add(m.group(2))
    print(f"Found {len(arm_dirs)} arm directories in {RUNS_DIR}")

    cells: list[dict] = []
    policy_excluded: dict[str, int] = {}        # PUBLISHED_EXCLUDE benches per snapshot (not invalid)
    display_name: dict[str, str] = {}           # norm → model spelling on disk

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
                # Snapshot-scope the excluded count so a frozen historical
                # board never drifts when unrelated new data lands. The payload
                # is not returned by load_score for excluded cells, so re-read
                # its timestamp directly (mtime fallback via snapshot_of).
                try:
                    _snap = snapshot_of(json.load(open(fpath)), fpath)
                except Exception:
                    _snap = snapshot_of(None, fpath)
                policy_excluded[_snap] = policy_excluded.get(_snap, 0) + 1
                continue

            if entry is None:
                bench_name = fname.removesuffix(".score.json")
                reasons = [skip_reason or cell_validity.REASON_MALFORMED]
                masked = False
            else:
                bench_name = entry["benchmark"]
                # TEARDOWN VISIBILITY: payload field wins; else the sidecar
                # derived from the watchdog line in the run logs.
                entry["teardown_masked"] = bool(
                    entry.get("teardown_masked") or bench_name in masked_set
                )
                masked = entry["teardown_masked"]
                reasons = list(cell_validity.classify_cell(
                    entry, requested_arm=arm, model=model).reasons)
                if dup_reason:
                    reasons.append(dup_reason)

            cells.append({
                "model": model, "norm": norm, "arm": arm, "arm_key": arm_key,
                "benchmark": bench_name,
                "file": str(fpath.relative_to(RUNS_DIR.parent)),
                "entry": entry,
                "cls": cell_validity.classify_reasons(reasons),
                "snapshot": snapshot_of(entry, fpath),
                "teardown_masked": masked,
            })
    return cells, policy_excluded, display_name


def column_snapshots(cells: list[dict]) -> dict:
    """(norm, arm_key) → the column's published snapshot for the LATEST board:
    the NEWEST snapshot with any cell, unconditionally. If a column's newest
    re-run is all-invalid, that newest run still wins and its invalidity is
    published loudly — a broken re-run must never silently revert the board
    to stale data (the older snapshot stays visible in its own versioned
    board under boards/<version>/)."""
    by_col: dict[tuple, list] = {}
    for c in cells:
        by_col.setdefault((c["norm"], c["arm_key"]), []).append(c)
    return {col: max((c["snapshot"] for c in col_cells), key=SNAPSHOTS.index)
            for col, col_cells in by_col.items()}


def build_experiment_output(cells: list[dict], policy_excluded,
                            snapshot: str = "latest",
                            quiet: bool = False) -> tuple[dict, list[dict]]:
    """Aggregate classified cells into (experiment_output, flat_scores).

    snapshot='latest' → single-snapshot-per-column board (each (model, arm)
    column publishes only its newest run-date snapshot — the June/July
    opus+gemini columns never mix). snapshot='june16'/'july02' → the versioned
    board of that run date only.

    `policy_excluded` accepts either the per-snapshot dict from collect_cells
    (snapshot-scoped — a frozen historical board keeps its own excluded count
    and never drifts when unrelated new data lands) or a bare int (legacy /
    test callers). For a specific snapshot only that snapshot's excluded cells
    are counted; 'latest' sums across all snapshots.
    """
    if isinstance(policy_excluded, dict):
        pe_count = (sum(policy_excluded.values()) if snapshot == "latest"
                    else policy_excluded.get(snapshot, 0))
    else:
        pe_count = policy_excluded

    def say(*a, **k):
        if not quiet:
            print(*a, **k)

    col_supersessions: dict[str, dict] = {}
    if snapshot == "latest":
        chosen = column_snapshots(cells)
        use = [c for c in cells if chosen[(c["norm"], c["arm_key"])] == c["snapshot"]]
        col_snap_meta = {f"{n}/{a}": s for (n, a), s in sorted(chosen.items())}
        # Eviction disclosure: newest-wins is the documented column policy,
        # but a column flip must never be SILENT — a single-bench re-run that
        # displaces a fuller older snapshot (gemini-3.5-flash cpu: 1 july09
        # cell evicted 36 june16 cells) is recorded here, in the artifact,
        # with the displaced cell counts. The displaced snapshot stays fully
        # published in its own versioned board under boards/<version>/.
        col_snap_counts: dict[tuple, Counter] = {}
        for c in cells:
            col_snap_counts.setdefault(
                (c["norm"], c["arm_key"]), Counter())[c["snapshot"]] += 1
        for (n, a), snap_counts in sorted(col_snap_counts.items()):
            if len(snap_counts) > 1:
                win = chosen[(n, a)]
                col_supersessions[f"{n}/{a}"] = {
                    "published_snapshot": win,
                    "published_cells": snap_counts[win],
                    "displaced_cells": {s: cnt for s, cnt in
                                        sorted(snap_counts.items()) if s != win},
                }
    elif snapshot in SNAPSHOTS:
        use = [c for c in cells if c["snapshot"] == snapshot]
        col_snap_meta = {f"{c['norm']}/{c['arm_key']}": snapshot for c in use}
    else:
        raise ValueError(f"unknown snapshot {snapshot!r}")

    # scores.json contract (module docstring): ONE entry per (model, bench,
    # arm) — best by the same keep-best rule as the board (resolved > not,
    # then fewer turns, then cost-valid beats cost-unavailable). Multiple
    # score files for one cell collapse here so no consumer summing the flat
    # file ever double-counts a cell.
    best_scores: dict[tuple[str, str, str], dict] = {}
    experiments: dict[tuple[str, str], dict] = {}

    def keeps_best(new: dict, cur: dict | None) -> bool:
        if cur is None:
            return True
        new_res, cur_res = bool(new.get("resolved")), bool(cur.get("resolved"))
        if new_res != cur_res:
            return new_res
        nt, ct = (new.get("turns_to_fix") or 0), (cur.get("turns_to_fix") or 0)
        if nt != ct:
            return nt < ct
        return bool(new.get("cost_valid")) and not bool(cur.get("cost_valid"))

    # ---- fail-closed, per-axis bookkeeping (INVALID ≠ COST_INVALID ≠ absent) ----
    invalid_cells: list[dict] = []              # solve-axis faults: fully invalid
    cost_invalid_cells: list[dict] = []         # solve kept, cost unavailable
    invalid_counts: Counter = Counter()         # (norm, arm_key) → count
    cost_invalid_counts: Counter = Counter()
    invalid_reasons: Counter = Counter()        # reason code → count
    cost_invalid_reasons: Counter = Counter()   # full subtype (zero_token_accounting:*)
    display_name: dict[str, str] = {}
    solve_files = 0                             # files entering solve aggregates
    cost_files = 0                              # ...whose cost axis is also valid

    for c in use:
        cls = c["cls"]
        entry = c["entry"]
        model, norm, arm_key = c["model"], c["norm"], c["arm_key"]
        display_name.setdefault(norm, model)
        record = {
            "model": model, "model_normalized": norm, "arm": arm_key,
            "benchmark": c["benchmark"], "file": c["file"],
            "snapshot": c["snapshot"],
            "teardown_masked": c["teardown_masked"],
        }
        if not cls.solve_valid:
            # SOLVE-axis fault: fully INVALID — excluded from ALL aggregates,
            # counted visibly (cost-axis reasons ride along in `reasons`).
            invalid_cells.append({**record, "axis": "solve", "reasons": cls.reasons})
            invalid_counts[(norm, arm_key)] += 1
            for r in cls.reasons:
                invalid_reasons[r.split(":", 1)[0]] += 1
            continue

        solve_files += 1
        if cls.cost_valid:
            cost_files += 1
            cost, cost_basis = compute_cost_ex(entry, model)
            entry["computed_cost_usd"] = cost
            entry["cost_basis"] = cost_basis
        else:
            # COST-axis fault: the solve verdict STANDS (oracle + receipt are
            # good); cost is UNAVAILABLE — excluded from cost/token columns,
            # counted visibly, NEVER priced (no rate-card synthesis).
            cost_invalid_cells.append({**record, "axis": "cost",
                                       "reasons": cls.cost_reasons,
                                       "resolved": bool(entry.get("resolved"))})
            cost_invalid_counts[(norm, arm_key)] += 1
            for r in cls.cost_reasons:
                cost_invalid_reasons[r] += 1
            entry["computed_cost_usd"] = None
            entry["cost_basis"] = "unavailable"
        entry["cost_valid"] = cls.cost_valid
        entry["snapshot"] = c["snapshot"]

        benchmark = c["benchmark"]
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
        summary = arm_summary(entry, model, cls.cost_valid, cls.cost_reasons)

        # Keep best: resolved > not, then fewer turns, then cost-valid
        if current is None:
            experiments[ek][arm_key] = summary
        elif summary["resolved"] and not current["resolved"]:
            experiments[ek][arm_key] = summary
        elif summary["resolved"] == current["resolved"] and summary["turns"] < current["turns"]:
            experiments[ek][arm_key] = summary
        elif (summary["resolved"] == current["resolved"] and summary["turns"] == current["turns"]
              and summary["cost_valid"] and not current["cost_valid"]):
            experiments[ek][arm_key] = summary

    # Materialize the deduped flat list (insertion order = walk order).
    all_scores = list(best_scores.values())

    # Build experiment output with model aggregates
    exp_list = sorted(experiments.values(), key=lambda e: (e["model"], e["benchmark"]))

    # Compute per-model aggregates for the experiment data.
    # AXIS SPLIT: solved/total (+ turns/tools/wall — work receipts) are the
    # SOLVE axis and include cost-invalid cells; cost/tokens are the COST axis
    # and sum ONLY over cost-valid cells (cost_cells = how many), so a $/task
    # or tok/task mean divides by cost_cells, never by total.
    def _empty_arm_agg() -> dict:
        return {"solved": 0, "total": 0, "cost": 0, "tokens": 0, "tokens_fresh": 0,
                "cost_cells": 0, "tools": 0, "turns": 0, "wall": 0,
                "invalid": 0, "cost_invalid": 0, "masked": 0,
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
                agg["tools"] += arm_data["tool_uses"]
                agg["turns"] += arm_data["turns"]
                agg["wall"] += arm_data["wall_clock"]
                if arm_data["teardown_masked"]:
                    agg["masked"] += 1
                if arm_data["cost_valid"]:
                    agg["cost_cells"] += 1
                    agg["cost"] += arm_data["cost_usd"]
                    # tokens = grand-total input INCL. cache traffic + output
                    # (the old input+output was fresh-only post-→2062 and
                    # inverted the Anthropic token comparison); tokens_fresh
                    # keeps the old fresh-only number for continuity.
                    agg["tokens"] += arm_data["billed_input_tokens"] + arm_data["output_tokens"]
                    agg["tokens_fresh"] += arm_data["input_tokens"] + arm_data["output_tokens"]
                basis = arm_data.get("cost_basis", "unpriced")
                agg["cost_bases"][basis] = agg["cost_bases"].get(basis, 0) + 1

    # Attach visible INVALID + COST-INVALID counts (a model whose cells are
    # ALL invalid still gets a row — invisible exclusion is exactly what
    # fail-closed forbids).
    for (norm, arm_key), count in invalid_counts.items():
        if norm not in model_agg:
            model_agg[norm] = _empty_model_agg(display_name.get(norm, norm), norm)
        model_agg[norm][arm_key]["invalid"] = count
    for (norm, arm_key), count in cost_invalid_counts.items():
        if norm not in model_agg:
            model_agg[norm] = _empty_model_agg(display_name.get(norm, norm), norm)
        model_agg[norm][arm_key]["cost_invalid"] = count

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

    say(f"[{snapshot}] Loaded {solve_files} solve-valid score files "
        f"({cost_files} also cost-valid)")
    say(f"Models: {len(models)}")
    say(f"Benchmarks: {len(benchmarks)}")
    say(f"Experiment entries: {len(exp_list)}")
    say(f"Model aggregates: {len(agg_list)}")

    # Fail-closed, per-axis accounting — INVALID is visible, absent is silent.
    masked_total = sum(agg[a]["masked"] for agg in model_agg.values()
                       for a in ("native", "kernel", "cpu"))
    say(f"\nValidity (per-axis): {solve_files} SOLVE-valid ({cost_files} cost-valid, "
        f"{len(cost_invalid_cells)} cost-INVALID: solve kept, cost unavailable), "
        f"{len(invalid_cells)} fully INVALID (excluded from all aggregates), "
        f"{pe_count} policy-excluded (PUBLISHED_EXCLUDE)")
    if invalid_reasons:
        for reason, n in sorted(invalid_reasons.items(), key=lambda kv: -kv[1]):
            say(f"  INVALID (both axes) {reason}: {n}")
    if cost_invalid_reasons:
        for reason, n in sorted(cost_invalid_reasons.items(), key=lambda kv: -kv[1]):
            say(f"  COST-INVALID (solve kept) {reason}: {n}")
    say(f"Teardown-masked cells (watchdog-scored, published with flag): {masked_total}")

    # Grand totals (solve axis over all published cells; cost/token axis over
    # cost-valid cells only — never a synthesized number in the sum)
    grand_solved = sum(1 for s in all_scores if s.get("resolved"))
    cost_valid_scores = [s for s in all_scores if s.get("cost_valid")]
    grand_cost = sum(s.get("computed_cost_usd") or 0 for s in cost_valid_scores)
    grand_tokens = sum(total_input_tokens(s)[0] + (s.get("output_tokens", 0) or 0)
                       for s in cost_valid_scores)
    say(f"\nGrand totals: {grand_solved}/{len(all_scores)} solved (solve axis); "
        f"{grand_tokens:,} tokens, ${grand_cost:,.2f} cost over "
        f"{len(cost_valid_scores)} cost-valid cells")

    # Fault annotations for this board (machine-readable ids → boards/FAULTS.json)
    snaps_present = sorted({c["snapshot"] for c in use}, key=SNAPSHOTS.index)
    faults = []
    seen_fids: set[str] = set()
    for s in snaps_present:
        for fid in SNAPSHOT_FAULTS.get(s, []):
            # A fault can attach to several snapshots (e.g. a harness bug
            # spanning a re-pin); the board's fault list carries it once.
            if fid in seen_fids:
                continue
            seen_fids.add(fid)
            faults.append({"id": fid, "note": FAULT_NOTES.get(fid, "")})

    import datetime as _dt
    experiment_output = {
        "generated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # A versioned snapshot board carries the binary it actually ran on;
        # the latest (mixed-column) view reports the current binary.
        "ostk_version": SNAPSHOT_OSTK_VERSION.get(snapshot, OSTK_VERSION),
        "snapshot": snapshot,
        "snapshot_policy": ("latest = single snapshot per (model, arm) column — "
                            "a column never mixes run dates; earlier snapshots "
                            "are published under boards/"
                            + SNAPSHOT_OSTK_VERSION.get(snapshot, OSTK_VERSION)
                            + "/"),
        "column_snapshots": col_snap_meta,
        "faults": faults,
        "faults_ref": "boards/FAULTS.json",
        # Fail-closed doctrine block: fully-INVALID cells are excluded from
        # every aggregate; COST-INVALID cells enter solve columns only. Both
        # are counted here and per model/arm — never a silent 0.
        "trust": {
            "doctrine": "fail-closed, judged per axis. SOLVE axis (oracle "
                        "verdict + arm receipt + not stalled): malformed / "
                        "no-arm-receipt / arm-mismatch / deadline / zero-work / "
                        "duplicate-column cells are fully INVALID — excluded "
                        "from everything, counted visibly. COST axis (real "
                        "token/cost accounting): zero token accounting on a "
                        "completed run keeps the solve verdict and marks cost "
                        "UNAVAILABLE — included in solve columns, excluded "
                        "from cost/token columns, counted visibly, never "
                        "priced. absent = never run (not invalid).",
            "solve_valid_cells": solve_files,
            "cost_valid_cells": cost_files,
            "cost_invalid_cells": len(cost_invalid_cells),
            "invalid_cells": len(invalid_cells),
            "policy_excluded_cells": pe_count,
            "invalid_by_reason": dict(sorted(invalid_reasons.items())),
            "cost_invalid_by_reason": dict(sorted(cost_invalid_reasons.items())),
            "teardown_masked_cells": masked_total,
        },
        "invalid_cells": invalid_cells,
        "cost_invalid_cells": cost_invalid_cells,
        "models": agg_list,
        "benchmarks": exp_list,
    }

    # LATEST-only honesty riders (versioned snapshot boards are single-version
    # and append-only — their payloads must stay byte-identical, so nothing
    # below touches a non-latest build).
    if snapshot == "latest":
        # Per-snapshot binary attribution: which execution version each
        # published column ran on (finding: the single ostk_version tag hid
        # that the rolling view mixes v7.6.0 and v7.7.1 executions).
        experiment_output["ostk_versions"] = {
            s: SNAPSHOT_OSTK_VERSION.get(s, OSTK_VERSION) for s in snaps_present}
        experiment_output["ostk_version_note"] = (
            "ostk_version is the CURRENT binary; columns in this rolling view "
            "executed under the version of their snapshot — see ostk_versions "
            "+ column_snapshots for per-column attribution.")
        if col_supersessions:
            experiment_output["column_supersessions"] = col_supersessions
            experiment_output["column_supersessions_note"] = (
                "columns whose newest snapshot displaced older cells "
                "(newest-wins policy). Displaced counts are published here so "
                "a partial re-run can never silently shrink a column; the "
                "displaced snapshot remains fully published under "
                "boards/<version>/.")
        # Per-version trust breakdown: the board-level totals above span
        # execution versions; the split keeps them honest bookkeeping rather
        # than an implied cross-version comparison.
        by_ver: dict[str, Counter] = {}
        for c in use:
            ver = SNAPSHOT_OSTK_VERSION.get(c["snapshot"], OSTK_VERSION)
            cnt = by_ver.setdefault(ver, Counter())
            if not c["cls"].solve_valid:
                cnt["invalid_cells"] += 1
            else:
                cnt["solve_valid_cells"] += 1
                cnt["cost_valid_cells" if c["cls"].cost_valid
                    else "cost_invalid_cells"] += 1
        if len(by_ver) > 1:
            experiment_output["trust"]["by_ostk_version"] = {
                v: {k: cnt.get(k, 0) for k in
                    ("solve_valid_cells", "cost_valid_cells",
                     "cost_invalid_cells", "invalid_cells")}
                for v, cnt in sorted(by_ver.items())}
            experiment_output["trust"]["mixed_versions_note"] = (
                "the totals above aggregate columns executed under more than "
                "one binary version (per-version split in by_ostk_version) — "
                "operational bookkeeping, NOT a cross-version comparison. No "
                "cross-version delta is published anywhere; versioned boards "
                "under boards/<version>/ are single-version.")
    return experiment_output, all_scores


def consolidate_all(dry_run: bool = False) -> None:
    """Main consolidation: walk runs/, produce scores.json + experiment-scores.json
    (the LATEST board — single snapshot per column). Versioned snapshot boards
    + the board-v760/cells-v760 adapters live in publish_boards.py, which
    consumes the same collect_cells/build_experiment_output pass."""
    cells, policy_excluded, _display = collect_cells()
    experiment_output, all_scores = build_experiment_output(
        cells, policy_excluded, snapshot="latest")
    exp_list = experiment_output["benchmarks"]
    agg_list = experiment_output["models"]

    if dry_run:
        print(f"\n[dry-run] Would write {len(all_scores)} scores + "
              f"{len(exp_list)} experiments + {len(agg_list)} aggregates")
        return

    # Write scores.json
    SCORES_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(SCORES_OUTPUT, "w") as f:
        json.dump(all_scores, f, indent=2)
        f.write("\n")
    print(f"\nWrote {len(all_scores)} entries to {SCORES_OUTPUT}")

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
