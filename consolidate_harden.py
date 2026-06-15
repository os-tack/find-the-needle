#!/usr/bin/env python3
"""Hardened 8-model native->B consolidation (2026-06-14).

Reads score JSONs directly (the legacy consolidate_scores.py hangs). Honest
methodology:
  - June-only (mtime >= 2026-06-01); exclude flaky bench retry-storm-duplicate-transfer.
  - Pair native ∩ B per bench. B = kernel-cpu (true B) for Tier-1, kernel (B*) for Tier-2.
  - COST is the trustworthy axis: cache-aware estimated_cost_usd, with opus-4-8
    kernel cost ÷3 (old score files carry the stale $15/$75 rate; real opus-4-8
    is $5/$25). Native cost = CLI cache-aware cost (no correction).
  - SOLVE rate = resolved over the paired set.
  - TOKENS: native input_tokens is cache-FOLDED (fresh+cache_read+cache_create);
    kernel input_tokens is fresh-only on old-binary cells. Use billed_tokens on
    B when present (new split binary) for a like-for-like total; else flag fresh-only.
  - Honest paired cell = both arms produced a score that isn't deadline/0-cost.
"""
import glob, os, json, datetime, sys

JUNE = datetime.datetime(2026, 6, 1).timestamp()
RUNS = "runs"
INVALID = {"retry-storm-duplicate-transfer"}  # flaky (1/8 spurious) — dropped entirely
EXCLUDE_STOP = {"deadline_exceeded"}  # not honest comparable spend
# PUBLISHED_EXCLUDE: RETIRED from the suite entirely — legacy tests predating
# the ostk rename and the kernel's real existence, so their oracles are NOT
# valid for this comparison. Neither run nor published. Score files archived
# (not deleted) under runs-archive/legacy-pre-kernel-<ts>/. No appendix — they
# don't warrant one; a single methodology note in REPORT §2 covers them.
# → published task count = 38, not 40.
PUBLISHED_EXCLUDE = {"haystack-boot", "haystack-mint"}

# →2062 SCHEMA PARITY: both arms store ATOMIC token buckets (fresh / cache_read
# / cache_create +5m/1h / output) with identical meaning. Cost is SUMMED here
# from those buckets at per-bucket rates — never read from a pre-folded field
# and no opus ÷3 hack (that existed only because the old kernel score carried a
# stale $15/$75 rate; we now price from the rate card below).
# Rate card: $/M tokens (input, output). Cache multipliers relative to input.
RATE_CARD = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "devstral-2512": (0.40, 2.00),
    "gpt-5.5": (5.00, 30.00),
    "grok-4.3": (1.25, 2.50),
    "deepseek-v4-pro": (1.74, 3.48),
    "kimi-k2.6": (0.40, 1.99),
}
CACHE_MULT = {"fresh": 1.0, "cache_read": 0.10, "cc_5m": 1.25, "cc_1h": 2.00}


def bucket_cost(rec, model):
    """Cost summed from the atomic token buckets at per-bucket rates.
    fresh 1x, cache_read 0.1x, cache_create 5m 1.25x / 1h 2x, output at out rate."""
    rate = RATE_CARD.get(model)
    if not rate:
        return rec.get("estimated_cost_usd", 0) or 0.0
    in_rate, out_rate = rate
    fresh = rec.get("fresh_input_tokens")
    if fresh is None:
        # legacy score w/o split — input_tokens priced at input rate
        tin = rec.get("input_tokens", 0) or 0
        tout = rec.get("output_tokens", 0) or 0
        return (tin * in_rate + tout * out_rate) / 1_000_000
    fresh = fresh or 0
    cr = rec.get("cache_read_tokens", 0) or 0
    cc5 = rec.get("cache_create_5m_tokens", 0) or 0
    cc1 = rec.get("cache_create_1h_tokens", 0) or 0
    cc_tot = rec.get("cache_create_tokens", 0) or 0
    cc_uns = max(0, cc_tot - cc5 - cc1)  # unsplit cache_create → treat as 5m
    tout = rec.get("output_tokens", 0) or 0
    cin = (fresh * CACHE_MULT["fresh"] + cr * CACHE_MULT["cache_read"]
           + cc5 * CACHE_MULT["cc_5m"] + cc1 * CACHE_MULT["cc_1h"]
           + cc_uns * CACHE_MULT["cc_5m"]) * in_rate
    return (cin + tout * out_rate) / 1_000_000


def total_input(rec):
    """Grand-total input tokens summed from atomic buckets (fresh+read+create),
    falling back to the fresh-only/legacy `input_tokens` when no split exists."""
    fresh = rec.get("fresh_input_tokens")
    if fresh is None:
        return rec.get("input_tokens", 0) or 0, False
    return ((fresh or 0) + (rec.get("cache_read_tokens", 0) or 0)
            + (rec.get("cache_create_tokens", 0) or 0)), True

# model -> (B arm dir, B label, native_harness)
# native_harness: the vendor CLI used for the native arm. Only claude-code
# reports cost + cache split + real turns → only ANTHROPIC models yield a
# trustworthy cross-arm cost/token comparison. gemini-cli/codex/opencode/
# mistral report cost=$0 and/or undercounted tokens and/or fake turn counts
# (codex = always 1 turn), so for those models we present SOLVE-RATE ONLY +
# kernel-arm absolutes, and explicitly mark native metrics harness-incomplete.
MODELS = [
    ("claude-opus-4-8",        "kernel-cpu", "B",  "claude-code"),
    ("claude-sonnet-4-6",      "kernel-cpu", "B",  "claude-code"),
    ("gemini-3.1-pro-preview", "kernel-cpu", "B",  "gemini-cli"),
    ("devstral-2512",          "kernel-cpu", "B",  "opencode/mistral"),
    ("gpt-5.5",                "kernel-cpu", "B",  "codex"),
    ("grok-4.3",               "kernel",     "B*", "opencode"),
    ("deepseek-v4-pro",        "kernel",     "B*", "opencode"),
    ("kimi-k2.6",              "kernel",     "B*", "opencode"),
]
# Only these native harnesses report trustworthy cost + cache + turns.
COST_TRUSTED_HARNESS = {"claude-code"}

def load(model, arm):
    out = {}
    for f in glob.glob(f"{RUNS}/{model}-{arm}/*.score.json"):
        if os.path.getmtime(f) < JUNE:
            continue
        b = os.path.basename(f).replace(".score.json", "")
        if b in INVALID:
            continue
        try:
            out[b] = json.load(open(f))
        except Exception:
            pass
    return out

def has_spend(rec):
    """A cell produced real, comparable spend if it has output tokens and some
    input (fresh or, for legacy, input_tokens). →2062: derived from atomic
    buckets, not the pre-folded estimated_cost_usd."""
    out = rec.get("output_tokens", 0) or 0
    tin, _ = total_input(rec)
    return out > 0 and tin > 0

def native_cost_present(rec):
    """A native cell carries a real cost number (claude-code does; gemini-cli /
    codex / opencode report 0). Cost/token deltas use ONLY such cells."""
    return (rec.get("estimated_cost_usd", 0) or 0) > 0

def is_zero_work(rec):
    """INFRA failure, not a capability failure: the agent never made an API
    call (container/spawn glitch or api.error before turn 1). resolved=false
    AND turns==0 AND output_tokens==0. Excluded from BOTH solve-rate and
    efficiency, symmetric for both arms, and retried. A genuine fail
    (resolved=false with turns>0) is NOT zero-work and DOES count as a loss.
    stop_reason='pass' (passive-verify, 0 turns legitimately) is never
    zero-work."""
    if rec.get("resolved"):
        return False
    if (rec.get("stop_reason") or "").lower() == "pass":
        return False
    return (rec.get("turns_to_fix", 0) or 0) == 0 and (rec.get("output_tokens", 0) or 0) == 0


# →2062 RESOLVED-GATING (methodology): efficiency (cost/token) deltas are
# computed ONLY over cells BOTH arms solved — "cheaper at failing" is
# meaningless. Solve-rate is the complete /38 picture per arm; split-resolve
# (one arm only) and both-fail cells are reported separately, never folded
# into the efficiency mean.
anthropic_rows = []   # full cost/token + solve
solveonly_rows = []   # solve-rate only (native metrics harness-incomplete)
split_log = {}        # model -> list of (bench, who_solved)
bothfail_log = {}     # model -> list of bench
infra_log = {}        # model -> list of (bench, which_arm) zero-work infra fails
TASK_TOTAL = None     # discovered from the largest paired set (≈38)
for model, barm, label, harness in MODELS:
    nat = load(model, "native")
    B = load(model, barm)
    # PUBLISHED aggregates exclude retired haystack-boot/haystack-mint.
    paired = sorted((set(nat) & set(B)) - PUBLISHED_EXCLUDE)
    # ZERO-WORK (infra) cells: agent never made an API call (turns==0, output==0,
    # not resolved). NOT a capability fail — excluded from solve-rate AND
    # efficiency, symmetric for both arms, and flagged for re-run. A genuine
    # fail (resolved=false WITH turns>0) is NOT zero-work and counts as a loss.
    infra = [(b, "native" if is_zero_work(nat[b]) else "kernel")
             for b in paired if is_zero_work(nat[b]) or is_zero_work(B[b])]
    infra_log[model] = infra
    infra_benches = {b for b, _ in infra}
    # Scored set: both arms non-deadline AND neither arm zero-work (so a glitch
    # on either arm removes the cell from BOTH arms' solve-rate, symmetric).
    scored = [b for b in paired
              if nat[b].get("stop_reason") not in EXCLUDE_STOP
              and B[b].get("stop_reason") not in EXCLUDE_STOP
              and b not in infra_benches]
    cost_trusted = harness in COST_TRUSTED_HARNESS
    n_scored = len(scored)
    nat_solved = [b for b in scored if nat[b].get("resolved")]
    b_solved = [b for b in scored if B[b].get("resolved")]
    nat_solve = (len(nat_solved) / n_scored) if n_scored else None
    b_solve = (len(b_solved) / n_scored) if n_scored else None
    # Both-solved = the ONLY set for efficiency deltas.
    both_solved = [b for b in scored if nat[b].get("resolved") and B[b].get("resolved")]
    # Split-resolve (one arm solved, the other didn't) + both-fail — reported
    # separately, excluded from efficiency.
    split_log[model] = [(b, "native-only" if nat[b].get("resolved") else "kernel-only")
                        for b in scored
                        if bool(nat[b].get("resolved")) != bool(B[b].get("resolved"))]
    bothfail_log[model] = [b for b in scored
                           if not nat[b].get("resolved") and not B[b].get("resolved")]

    if cost_trusted:
        # Efficiency cells: BOTH solved AND both produced real spend AND native
        # carries a real cost (guard against $0-native cells in the mean).
        cost_cells = [b for b in both_solved
                      if has_spend(nat[b]) and has_spend(B[b])
                      and native_cost_present(nat[b])]
        nc = len(cost_cells)
        if nc:
            nat_cost = sum(bucket_cost(nat[b], model) for b in cost_cells)
            b_cost = sum(bucket_cost(B[b], model) for b in cost_cells)
            nat_in = sum(total_input(nat[b])[0] for b in cost_cells)
            b_in = sum(total_input(B[b])[0] for b in cost_cells)
            split_cov = sum(1 for b in cost_cells if total_input(B[b])[1]) / nc
            cost_delta = (b_cost - nat_cost) / nat_cost * 100 if nat_cost else None
            tok_delta = (b_in - nat_in) / nat_in * 100 if nat_in else None
        else:
            nat_cost = b_cost = nat_in = b_in = split_cov = cost_delta = tok_delta = None
        anthropic_rows.append((model, label, n_scored, len(nat_solved), len(b_solved),
                               nat_solve, b_solve, nc, nat_cost, b_cost,
                               cost_delta, tok_delta, split_cov))
    else:
        # Solve-rate-only model: native metrics harness-incomplete. B-arm
        # absolutes over the B-solved cells (context), NO cross-arm delta.
        bsv = [b for b in scored if B[b].get("resolved")]
        b_abs_cost = sum(bucket_cost(B[b], model) for b in bsv) if bsv else None
        b_abs_in = sum(total_input(B[b])[0] for b in bsv) if bsv else None
        solveonly_rows.append((model, label, harness, n_scored,
                               len(nat_solved), len(b_solved),
                               nat_solve, b_solve, b_abs_cost, b_abs_in))

def pct(x):
    return f"{x*100:>5.0f}%" if x is not None else "    -"

print("=" * 84)
print("SECTION 1 — ANTHROPIC: apples-to-apples cost / token (claude-code native)")
print("  The only native harness reporting cost + cache split + real turns.")
print("  SOLVE = resolved count / scored. EFFICIENCY (cost%/tok%) over the")
print("  both-solved subset ONLY (n=both); 'cheaper at failing' excluded.")
print("=" * 84)
print(f"{'model':<20}{'B':>3}{'scd':>4}{'natSlv':>10}{'BSlv':>9}"
      f"{'both':>5}{'nat$':>9}{'B$':>9}{'cost%':>7}{'tok%':>7}{'splt':>6}")
print("-" * 84)
for (m, lab, scd, nsv, bsv, ns, bs, cn, nc_, bc_, cd, td, sc) in anthropic_rows:
    cds = f"{cd:>+6.0f}%" if cd is not None else "     -"
    tds = f"{td:>+6.0f}%" if td is not None else "     -"
    ncs = f"{nc_:>9.3f}" if nc_ is not None else "        -"
    bcs = f"{bc_:>9.3f}" if bc_ is not None else "        -"
    scs = f"{sc*100:>5.0f}%" if sc is not None else "    -"
    print(f"{m:<20}{lab:>3}{scd:>4}{nsv:>4}/{scd:<2}{pct(ns)}{bsv:>4}/{scd:<2}{pct(bs)}"
          f"{cn:>5}{ncs}{bcs}{cds}{tds}{scs}")

print()
print("=" * 84)
print("SECTION 2 — SOLVE-RATE ONLY (native metrics harness-incomplete)")
print("  native cost=$0 and/or tokens undercounted and/or turns fake (codex=1).")
print("  Cross-arm Δ$/Δtok OMITTED (measurement artifact). B$/B_tok = kernel")
print("  ABSOLUTES over B-solved cells (not a delta).")
print("=" * 84)
print(f"{'model':<18}{'B':>3}{'native_harness':>17}{'scd':>4}{'natSlv':>10}{'BSlv':>9}"
      f"{'B$(abs)':>9}{'B_tok(abs)':>12}")
print("-" * 84)
for (m, lab, harness, scd, nsv, bsv, ns, bs, bac, bai) in solveonly_rows:
    bacs = f"{bac:>9.3f}" if bac is not None else "        -"
    bais = f"{bai:>12,}" if bai is not None else "           -"
    print(f"{m:<18}{lab:>3}{harness:>17}{scd:>4}{nsv:>4}/{scd:<2}{pct(ns)}{bsv:>4}/{scd:<2}{pct(bs)}{bacs}{bais}")

# Split-resolve + both-fail + infra breakdown (excluded from efficiency).
print()
print("=" * 84)
print("SECTION 3 — SPLIT-RESOLVE / BOTH-FAIL (solve signal) + INFRA-FAILED (excluded)")
print("  SPLIT/BOTH-FAIL = genuine capability outcomes (turns>0). INFRA = zero-work")
print("  (turns==0, no API call) — excluded from BOTH solve-rate & efficiency, retried.")
print("=" * 84)
any_row = False
for model, barm, label, harness in MODELS:
    sp = split_log.get(model, []); bf = bothfail_log.get(model, []); inf = infra_log.get(model, [])
    if sp or bf or inf:
        any_row = True
        for b, who in sp:
            print(f"  SPLIT     {model:<22} {b:<34} solved by: {who}")
        if bf:
            print(f"  BOTH-FAIL {model:<22} ({len(bf)}): {', '.join(bf)}")
        if inf:
            print(f"  INFRA     {model:<22} ({len(inf)}): " +
                  ', '.join(f"{b}[{arm}]" for b, arm in inf))
if not any_row:
    print("  (none — no split-resolve, both-fail, or infra cells in current data)")

print("\nLEGEND (→2062 schema-parity + report-structure guard):")
print("Both arms store ATOMIC token buckets (fresh / cache_read / cache_create")
print("+5m/1h / output), identical meaning. Cost SUMMED per-bucket: fresh 1x,")
print("cache_read 0.1x, cache_create 5m 1.25x / 1h 2x, output at out rate.")
print("RESOLVED-GATED: efficiency (cost%/tok%) computed ONLY over cells BOTH arms")
print("solved (the 'both' column = N) — never folds a both-fail or split-resolve")
print("cell into the mean. Solve-rate = resolved/scored, complete per arm. S1 also")
print("requires native cost>0. S2 native cost/tokens/turns harness-incomplete")
print("(gemini-cli/codex/opencode) → solve-rate + B-absolutes only. cost%/tok%")
print("neg = kernel cheaper/lighter. Excludes retry-storm (flaky) + 2 retired.")

# --per-cell: anthropic per-cell breakdown (shows easy-cell cost reversals vs
# hard-cell wins). Only the cost-trusted (claude-code) models.
if "--per-cell" in sys.argv:
    for model, barm, label, harness in MODELS:
        if harness not in COST_TRUSTED_HARNESS:
            continue
        nat = load(model, "native"); B = load(model, barm)
        cells = sorted(b for b in (set(nat) & set(B))
                       if nat[b].get("stop_reason") not in EXCLUDE_STOP
                       and B[b].get("stop_reason") not in EXCLUDE_STOP
                       and has_spend(nat[b]) and has_spend(B[b])
                       and native_cost_present(nat[b]))
        print(f"\n=== PER-CELL — {model} ({label}, native={harness}) ===")
        print(f"{'bench':<34}{'natT':>5}{'BT':>4}{'nat$':>9}{'B$':>9}{'cost%':>7}{'tok%':>7}")
        print("-" * 75)
        for b in cells:
            ncst = bucket_cost(nat[b], model); bcst = bucket_cost(B[b], model)
            nin = total_input(nat[b])[0]; bin_ = total_input(B[b])[0]
            cd = (bcst - ncst) / ncst * 100 if ncst else 0
            td = (bin_ - nin) / nin * 100 if nin else 0
            print(f"{b[:33]:<34}{nat[b].get('turns_to_fix',0):>5}{B[b].get('turns_to_fix',0):>4}"
                  f"{ncst:>9.4f}{bcst:>9.4f}{cd:>+6.0f}%{td:>+6.0f}%")

# RETIRED benchmarks (haystack-boot / haystack-mint): legacy pre-rename/pre-
# kernel relics, removed from the suite — neither run nor published, no
# appendix. A one-line methodology note in REPORT §2 documents the removal.
print(f"\n[note] {len(PUBLISHED_EXCLUDE)} retired benchmarks "
      f"({', '.join(sorted(PUBLISHED_EXCLUDE))}) removed from suite — "
      f"legacy pre-rename/pre-kernel; not run, not published. 38 tasks published.")
