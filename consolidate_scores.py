#!/usr/bin/env python3
"""
consolidate_scores.py — Aggregate score files into public JSON for the website.

Walks runs/ to find all *-native/, *-kernel/, *-kernel-cpu/ directories,
loads score files, and produces:

  public/scores.json          — flat list of all scores (best per model+bench+arm)
  public/experiment-scores.json — three-arm comparison (native/kernel/kernel-cpu)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

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


# →2062 SCHEMA PARITY cache multipliers (relative to the model's INPUT rate).
# Both arms now store ATOMIC token buckets (fresh / cache_read / cache_create
# +5m/1h / output) with identical meaning; cost is SUMMED here from those
# buckets, never read from a pre-folded field. Anthropic prompt-caching rates:
#   fresh (uncached) input ...... 1.00x input rate
#   cache_read (cache hit) ...... 0.10x input rate
#   cache_create 5m write ....... 1.25x input rate
#   cache_create 1h write ....... 2.00x input rate
CACHE_MULT = {"fresh": 1.0, "cache_read": 0.10, "cache_create_5m": 1.25, "cache_create_1h": 2.00}


def compute_cost(entry: dict, model: str) -> float:
    """Compute cost by SUMMING the atomic token buckets at their per-bucket rate.

    →2062: never reads the pre-folded `estimated_cost_usd`; prices each separate
    bucket (fresh 1x, cache_read 0.1x, cache_create 5m 1.25x / 1h 2x, output at
    output rate). Falls back to fresh+output only when the split fields are
    absent (legacy/non-Anthropic scores that don't journal a cache split)."""
    rate = RATE_CARD.get(model)
    if not rate:
        # No rate card → trust the harness's own number if present, else 0.
        return entry.get("estimated_cost_usd", 0) or 0.0
    in_rate, out_rate = rate
    fresh = entry.get("fresh_input_tokens")
    # Anthropic-style split present → price every bucket explicitly.
    if fresh is not None:
        fresh = fresh or 0
        cache_read = entry.get("cache_read_tokens", 0) or 0
        cc_5m = entry.get("cache_create_5m_tokens", 0) or 0
        cc_1h = entry.get("cache_create_1h_tokens", 0) or 0
        cc_total = entry.get("cache_create_tokens", 0) or 0
        # If 5m/1h aren't split out, treat all cache_create as 5m (the common case).
        cc_unsplit = max(0, cc_total - cc_5m - cc_1h)
        tout = entry.get("output_tokens", 0) or 0
        cost_input = (
            fresh * CACHE_MULT["fresh"]
            + cache_read * CACHE_MULT["cache_read"]
            + cc_5m * CACHE_MULT["cache_create_5m"]
            + cc_1h * CACHE_MULT["cache_create_1h"]
            + cc_unsplit * CACHE_MULT["cache_create_5m"]
        ) * in_rate
        return (cost_input + tout * out_rate) / 1_000_000
    # No split fields: legacy path. `input_tokens` is fresh-only post-→2062 (or
    # a fold pre-→2062); price it at the input rate + output.
    tin = entry.get("input_tokens", 0) or 0
    tout = entry.get("output_tokens", 0) or 0
    if tin == 0 and tout == 0:
        return entry.get("estimated_cost_usd", 0) or 0.0
    return (tin * in_rate + tout * out_rate) / 1_000_000


def load_score(fpath: Path) -> dict | None:
    """Load and validate a score JSON file. F7: if a sibling
    `<bench>.samples.json` exists, fold majority-vote + variance from the
    samples sidecar into the returned entry.

    Quality gate: cells that hit the wall_clock deadline OR show 0-turn /
    0-token output are infra failures (F5 snapshot regression, killed
    processes, etc.), NOT real model verdicts. They get filtered out so
    the leaderboard never shows a model as 'failed' on a cell where the
    bench infra was the actual problem.
    """
    try:
        with open(fpath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN: skipping {fpath} — {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or "benchmark" not in data:
        return None
    # Published-exclude: flaky + stale-oracle benches kept on disk but never
    # entered into published aggregates (REPORT §2 Appendix lists them raw).
    if data.get("benchmark") in PUBLISHED_EXCLUDE:
        return None
    # Quality gate: drop infra-failure cells.
    # The kernel-arm `attach: ostk attach bench` bootstrap race produces
    # cells with `resolved=true` AND zero turns AND zero tokens AND
    # stop_reason="exit_code: 1" — these silently inflate kernel-arm
    # pass rates if we only check the `not resolved` half. Drop ALL
    # zero-work cells regardless of resolved, EXCEPT when stop_reason
    # is "pass" (a small handful of benches that resolve via passive
    # verification with no agent turns — e.g. qwen3-coder-plus on
    # retry-storm-duplicate-transfer).
    stop = (data.get("stop_reason") or "").lower()
    turns = data.get("turns_to_fix") or 0
    tokens = (data.get("input_tokens") or 0) + (data.get("output_tokens") or 0)
    if "deadline" in stop:
        return None
    if turns == 0 and tokens == 0 and stop != "pass":
        return None
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
    return data


def arm_summary(entry: dict, model: str) -> dict:
    """Extract per-arm summary fields from a score entry."""
    cost = compute_cost(entry, model)
    return {
        "resolved": bool(entry.get("resolved", False)),
        "turns": entry.get("turns_to_fix", 0) or 0,
        "input_tokens": entry.get("input_tokens", 0) or 0,
        "output_tokens": entry.get("output_tokens", 0) or 0,
        "token_cost": entry.get("token_cost", 0) or 0,
        "cost_usd": round(cost, 6),
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
    for entry in sorted(RUNS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = ARM_PATTERN.match(entry.name)
        if m:
            model = m.group(1)
            arm = m.group(2)
            arm_dirs.append((model, arm, entry))

    print(f"Found {len(arm_dirs)} arm directories in {RUNS_DIR}")

    # Load all scores
    all_scores = []  # flat list for scores.json
    # Keyed by (normalized_model, benchmark) → {model, benchmark, native, kernel, cpu}
    experiments: dict[tuple[str, str], dict] = {}

    total_files = 0
    for model, arm, dir_path in arm_dirs:
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(".score.json"):
                continue
            fpath = dir_path / fname
            entry = load_score(fpath)
            if entry is None:
                continue
            total_files += 1

            benchmark = entry["benchmark"]
            entry["computed_cost_usd"] = compute_cost(entry, model)

            # Flat scores list
            all_scores.append(entry)

            # Experiment grouping
            norm = normalize_model(model)
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

            arm_key = "cpu" if arm == "kernel-cpu" else arm
            current = experiments[ek][arm_key]
            summary = arm_summary(entry, model)

            # Keep best: resolved > not, then fewer turns
            if current is None:
                experiments[ek][arm_key] = summary
            elif summary["resolved"] and not current["resolved"]:
                experiments[ek][arm_key] = summary
            elif summary["resolved"] == current["resolved"] and summary["turns"] < current["turns"]:
                experiments[ek][arm_key] = summary

    # Build experiment output with model aggregates
    exp_list = sorted(experiments.values(), key=lambda e: (e["model"], e["benchmark"]))

    # Compute per-model aggregates for the experiment data
    model_agg: dict[str, dict] = {}
    for exp in exp_list:
        norm = exp["model_normalized"]
        if norm not in model_agg:
            model_agg[norm] = {
                "model": exp["model"],
                "model_normalized": norm,
                "benchmarks": 0,
                "native": {"solved": 0, "total": 0, "cost": 0, "tokens": 0, "tools": 0, "turns": 0, "wall": 0},
                "kernel": {"solved": 0, "total": 0, "cost": 0, "tokens": 0, "tools": 0, "turns": 0, "wall": 0},
                "cpu": {"solved": 0, "total": 0, "cost": 0, "tokens": 0, "tools": 0, "turns": 0, "wall": 0},
            }
        model_agg[norm]["benchmarks"] += 1
        for arm_key in ["native", "kernel", "cpu"]:
            arm_data = exp[arm_key]
            if arm_data is not None:
                agg = model_agg[norm][arm_key]
                agg["total"] += 1
                if arm_data["resolved"]:
                    agg["solved"] += 1
                agg["cost"] += arm_data["cost_usd"]
                agg["tokens"] += arm_data["input_tokens"] + arm_data["output_tokens"]
                agg["tools"] += arm_data["tool_uses"]
                agg["turns"] += arm_data["turns"]
                agg["wall"] += arm_data["wall_clock"]

    # Models with working native CPU drivers in haystack create_driver()
    # Others route through OpenRouter on kernel arm — CPU arm not yet built
    # NOTE: keys are NORMALIZED model names (normalize_model: lowercase, [_.]→-).
    # So "claude-opus-4-8" stays, "gemini-3.1-pro-preview" → "gemini-3-1-pro-preview",
    # "gpt-5.5" → "gpt-5-5", "gpt-5.5-pro" → "gpt-5-5-pro".
    CPU_DRIVER_MODELS = {
        # Anthropic — native Messages API
        "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6",
        "claude-opus-4-8",  # frontier-2026 Tier-1
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
        agg["kernel_arm_type"] = "native_driver" if norm in CPU_DRIVER_MODELS else "generic_kernel"

        # Native CLI label — what actually runs for --arm native
        NATIVE_CLI_MAP = {
            "claude-haiku-4-5": "claude-code", "claude-sonnet-4-6": "claude-code", "claude-opus-4-6": "claude-code",
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

    # Grand totals
    grand_solved = sum(1 for s in all_scores if s.get("resolved"))
    grand_cost = sum(s.get("computed_cost_usd", 0) for s in all_scores)
    grand_tokens = sum((s.get("input_tokens", 0) or 0) + (s.get("output_tokens", 0) or 0) for s in all_scores)
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
