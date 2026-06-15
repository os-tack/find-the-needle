#!/usr/bin/env python3
"""Per-cell matrix emitter for the reinvented heatmap (v7.6.0).

Emits public/cells-v760.json: models x benchmarks grid, each cell carrying the
native AND kernel arm result (resolved/status, turns, full token bucket split,
cost). Benchmarks are tier-classified (ceiling/discriminating/floor) from THIS
run's actual cross-arm solve rate and ordered hardest->easiest so the difficulty
gradient is visible left-to-right.

Standalone (own small copies of the cost/token helpers) so it never triggers
consolidate_harden.py's import-time board emit. Same rate card + bucket logic.
"""
import glob, os, json

RUNS = "runs"
INVALID = {"retry-storm-duplicate-transfer"}          # flaky, dropped
PUBLISHED_EXCLUDE = {"haystack-boot", "haystack-mint"}  # retired
EXCLUDE_STOP = {"deadline_exceeded"}

RATE_CARD = {
    "claude-opus-4-8": (5.00, 25.00), "claude-sonnet-4-6": (3.00, 15.00),
    "gemini-3.1-pro-preview": (2.00, 12.00), "devstral-2512": (0.40, 2.00),
    "gpt-5.5": (5.00, 30.00), "grok-4.3": (1.25, 2.50),
    "deepseek-v4-pro": (1.74, 3.48), "kimi-k2.6": (0.40, 1.99),
}
CM = {"fresh": 1.0, "cache_read": 0.10, "cc_5m": 1.25, "cc_1h": 2.00}

# model -> (B arm dir, label, native harness)
MODELS = [
    ("claude-opus-4-8", "kernel-cpu", "B", "claude-code"),
    ("claude-sonnet-4-6", "kernel-cpu", "B", "claude-code"),
    ("gemini-3.1-pro-preview", "kernel-cpu", "B", "gemini-cli"),
    ("gpt-5.5", "kernel-cpu", "B", "codex"),
    ("devstral-2512", "kernel-cpu", "B", "vibe"),
    ("grok-4.3", "kernel", "B*", "opencode"),
    ("deepseek-v4-pro", "kernel", "B*", "opencode"),
    ("kimi-k2.6", "kernel", "B*", "kimi"),
]


def g(rec, k):
    return rec.get(k, 0) or 0


def bucket_cost(rec, model):
    rate = RATE_CARD.get(model)
    if not rate:
        return rec.get("estimated_cost_usd", 0) or 0.0
    in_rate, out_rate = rate
    fresh = g(rec, "fresh_input_tokens")
    cr = g(rec, "cache_read_tokens")
    cc_tot = g(rec, "cache_create_tokens")
    in_tok = g(rec, "input_tokens")
    tout = g(rec, "output_tokens")
    # present-but-zero native (gemini/codex): real total in input_tokens
    if fresh == 0 and cr == 0 and cc_tot == 0 and in_tok > 0:
        return (in_tok * in_rate + tout * out_rate) / 1e6
    # opencode self-reported (grok/deepseek native): provider billing cost
    sc = rec.get("estimated_cost_usd", 0) or 0
    if fresh == 0 and cr == 0 and cc_tot == 0 and in_tok == 0 and sc > 0:
        return sc
    cc5 = g(rec, "cache_create_5m_tokens")
    cc1 = g(rec, "cache_create_1h_tokens")
    cc_uns = max(0, cc_tot - cc5 - cc1)
    cin = (fresh * CM["fresh"] + cr * CM["cache_read"] + cc5 * CM["cc_5m"]
           + cc1 * CM["cc_1h"] + cc_uns * CM["cc_5m"]) * in_rate
    return (cin + tout * out_rate) / 1e6


def total_input(rec):
    fresh = rec.get("fresh_input_tokens")
    if fresh is None:
        return g(rec, "input_tokens")
    f = fresh or 0
    cr = g(rec, "cache_read_tokens")
    cc = g(rec, "cache_create_tokens")
    in_tok = g(rec, "input_tokens")
    if f == 0 and cr == 0 and cc == 0 and in_tok > 0:
        return in_tok
    return f + cr + cc


def is_zero_work(rec):
    if rec.get("resolved"):
        return False
    if (rec.get("stop_reason") or "").lower() == "pass":
        return False
    return g(rec, "turns_to_fix") == 0 and g(rec, "output_tokens") == 0


def load(model, arm):
    out = {}
    for f in glob.glob(f"{RUNS}/{model}-{arm}/*.score.json"):
        b = os.path.basename(f).replace(".score.json", "")
        if b in INVALID or b in PUBLISHED_EXCLUDE:
            continue
        try:
            out[b] = json.load(open(f))
        except Exception:
            pass
    return out


def cell(rec):
    """One arm's per-cell payload. status: pass|fail|deadline|infra|missing."""
    if rec is None:
        return {"status": "missing"}
    stop = (rec.get("stop_reason") or "")
    if stop in EXCLUDE_STOP:
        status = "deadline"
    elif is_zero_work(rec):
        status = "infra"
    elif rec.get("resolved"):
        status = "pass"
    else:
        status = "fail"
    return {
        "status": status,
        "resolved": bool(rec.get("resolved")),
        "turns": g(rec, "turns_to_fix"),
        "wall": round(rec.get("wall_clock", 0) or 0, 1),
        "billed": g(rec, "billed_tokens") or total_input(rec) + g(rec, "output_tokens"),
        "fresh": g(rec, "fresh_input_tokens"),
        "cache_read": g(rec, "cache_read_tokens"),
        "cache_create": g(rec, "cache_create_tokens"),
        "output": g(rec, "output_tokens"),
        "input_total": total_input(rec),
    }


def cost_of(rec, model):
    return round(bucket_cost(rec, model), 4) if rec is not None else None


# ---- discover benchmarks + tier them from cross-arm solve rate ----
all_benches = set()
arms_data = {}
for model, barm, label, harness in MODELS:
    nat = load(model, "native")
    B = load(model, barm)
    arms_data[model] = (nat, B, barm, label, harness)
    all_benches |= (set(nat) | set(B))
all_benches = sorted(all_benches)

# solve rate per bench across every valid (non-deadline) model-arm cell
bench_solve = {}
for b in all_benches:
    res = tot = 0
    for model, (nat, B, barm, label, harness) in arms_data.items():
        for d in (nat.get(b), B.get(b)):
            if d is None:
                continue
            if (d.get("stop_reason") or "") in EXCLUDE_STOP:
                continue
            tot += 1
            res += 1 if d.get("resolved") else 0
    bench_solve[b] = (res, tot, (res / tot if tot else 0.0))


def tier(rate):
    if rate <= 0.40:
        return "ceiling"
    if rate < 0.90:
        return "discriminating"
    return "floor"


# order: ceiling first, then discriminating, then floor — each by ascending solve
order = {"ceiling": 0, "discriminating": 1, "floor": 2}
bench_order = sorted(
    all_benches,
    key=lambda b: (order[tier(bench_solve[b][2])], bench_solve[b][2], b),
)

benchmarks = []
for b in bench_order:
    res, tot, rate = bench_solve[b]
    benchmarks.append({
        "name": b, "tier": tier(rate),
        "solve_pct": round(rate * 100), "solved": res, "total": tot,
    })

NATIVE_CLI = {
    "claude-opus-4-8": "claude-code", "claude-sonnet-4-6": "claude-code",
    "gemini-3.1-pro-preview": "gemini-cli", "gpt-5.5": "codex",
    "devstral-2512": "vibe", "grok-4.3": "opencode",
    "deepseek-v4-pro": "opencode", "kimi-k2.6": "kimi",
}

models_out = []
for model, (nat, B, barm, label, harness) in arms_data.items():
    cells = {}
    for b in bench_order:
        nr, br = nat.get(b), B.get(b)
        cn, cb = cell(nr), cell(br)
        cn["cost"] = cost_of(nr, model)
        cb["cost"] = cost_of(br, model)
        cells[b] = {"native": cn, "kernel": cb}
    nat_solved = sum(1 for b in bench_order
                     if cells[b]["native"]["status"] == "pass")
    ker_solved = sum(1 for b in bench_order
                     if cells[b]["kernel"]["status"] == "pass")
    models_out.append({
        "model": model, "type": label, "native_cli": NATIVE_CLI.get(model, harness),
        "native_solved": nat_solved, "kernel_solved": ker_solved,
        "cells": cells,
    })

out = {
    "generated": "2026-06-16", "run": "v7.6.0", "published_tasks": 38,
    "benchmarks": benchmarks, "models": models_out,
}
os.makedirs("public", exist_ok=True)
with open("public/cells-v760.json", "w") as f:
    json.dump(out, f, indent=1)
print(f"[cells] wrote public/cells-v760.json "
      f"({len(models_out)} models x {len(benchmarks)} benches)")

# ---- also refresh the canonical flat per-cell scores.json (about.astro's
# "every run appends a record" dataset). Fresh v7.6.0 data, all 8 models x
# both arms, with difficulty tier + recomputed per-bucket cost. ----
tier_of = {b["name"]: b["tier"] for b in benchmarks}
flat = []
COPY = ["resolved", "turns_to_fix", "wall_clock", "estimated_cost_usd",
        "token_cost", "fresh_input_tokens", "cache_read_tokens",
        "cache_create_tokens", "cache_create_5m_tokens",
        "cache_create_1h_tokens", "billed_tokens", "input_tokens",
        "output_tokens", "stop_reason", "tool_uses", "timestamp"]
for model, (nat, B, barm, label, harness) in arms_data.items():
    for arm_name, recs in (("native", nat), (barm.replace("-cpu", ""), B)):
        for b in bench_order:
            r = recs.get(b)
            if r is None:
                continue
            rec = {"agent": f"{model}-{arm_name}", "arm": arm_name,
                   "benchmark": b, "model": model, "kernel_type": label,
                   "difficulty_tier": tier_of.get(b, "unknown"),
                   "recomputed_cost_usd": round(bucket_cost(r, model), 6)}
            for k in COPY:
                if k in r:
                    rec[k] = r[k]
            flat.append(rec)
with open("public/scores.json", "w") as f:
    json.dump(flat, f, indent=1)
print(f"[scores] wrote public/scores.json ({len(flat)} per-cell records)")
nb = sum(1 for x in benchmarks if x["tier"] == "ceiling")
nd = sum(1 for x in benchmarks if x["tier"] == "discriminating")
nf = sum(1 for x in benchmarks if x["tier"] == "floor")
print(f"[tiers] ceiling={nb} discriminating={nd} floor={nf}")
