#!/usr/bin/env python3
"""
cell_validity.py — fail-closed cell classification for the needle-bench score pipeline.

TRUST DOCTRINE: the bench FAILS CLOSED and is judged PER AXIS. A cell carries
two independently trustworthy claims:

  SOLVE axis — the oracle verdict (test.sh pass/fail) plus the arm receipt:
           did THIS arm actually run, finish inside its deadline, and do real
           work? Poisoned by: malformed payload, missing/mismatched arm
           receipt, deadline stall, zero-work bootstrap glitch, or a
           duplicate-column presentation of one execution.
  COST axis — token/cost accounting: is the billing record real? Poisoned by:
           zero/absent token accounting on a completed run (no input, no
           output, or a billing-aware writer that recorded billed=0 with
           empty buckets).

A SOLVE-axis failure kills BOTH axes (a stalled or receipt-less run has no
trustworthy verdict AND no attributable spend). A COST-axis failure does NOT
destroy a valid solve verdict — the oracle still ran and the arm receipt still
matches; the cell keeps its solve verdict and its cost axis is marked
UNAVAILABLE (excluded from cost/token aggregates, counted visibly, and NEVER
priced — a fabricated rate-card number is worse than a visible gap).

Every (model, benchmark, arm) cell is therefore one of FOUR states:

  VALID         both axes trustworthy. Enters solve AND cost aggregates.
  COST_INVALID  solve axis trustworthy, cost axis unavailable. Enters solve
                aggregates; excluded from cost/token aggregates; counted
                VISIBLY per model/arm with a reason code; never priced.
  INVALID       solve axis untrustworthy (which drags cost down with it).
                Excluded from ALL published aggregates, counted VISIBLY.
  absent        no score file at all — the cell was legitimately never run.
                Absent is NOT invalid: it contributes nothing, is not counted
                as a failure, and shows up only as a missing cell.

Importable by consolidate_scores.py (and any future consolidator); stdlib only.
"""

from __future__ import annotations

from typing import NamedTuple

VALID = "valid"
INVALID = "invalid"
COST_INVALID = "cost_invalid"

# ---------------------------------------------------------------------------
# Reason codes (stable strings — they key the board's invalid_by_reason
# histogram, so treat them as a public vocabulary).
# ---------------------------------------------------------------------------
REASON_MALFORMED = "malformed_payload"            # unparseable / not a score dict
REASON_NO_ARM_RECEIPT = "no_arm_receipt"          # payload carries no executed-arm evidence
REASON_ARM_MISMATCH = "arm_mismatch"              # requested arm != executed arm
REASON_DEADLINE = "deadline_exceeded"             # infra deadline, not a model verdict
REASON_STALL = "stall"                            # stall_watch killed a no-progress run
REASON_ZERO_WORK = "zero_work"                    # 0 turns + 0 tokens (bootstrap race etc.)
REASON_ZERO_TOKEN_INPUT = "zero_token_accounting:no_input"    # completed run, no input-side tokens
REASON_ZERO_TOKEN_OUTPUT = "zero_token_accounting:no_output"  # completed run, no output tokens
# Completed run whose writer RECORDS billing (billed_tokens key present) but
# billed it as 0 with empty atomic buckets — the harness-incomplete class
# (deepseek/grok/devstral native): the only surviving number is an
# unverifiable folded input_tokens, so the cell is untrustworthy for cost.
REASON_ZERO_BILLED = "zero_token_accounting:zero_billed"
# Completed / "pass" cell that recorded ZERO turns AND ZERO tokens on BOTH
# sides — a zero-ACCOUNTING pass. Either the arm did real work whose telemetry
# was lost (the native error-result writer stamps error_max_turns runs as a
# 0-turn/0-token "pass": nginx-upstream-port-mismatch's launcher.log shows
# num_turns:41 and $1.40 of spend, all discarded to 0) or it is a genuine
# passive-verify $0 cell — the two are INDISTINGUISHABLE at the score-payload
# level, so we fail closed on the COST axis. Previously an explicit
# passive_verify exemption blessed these as fully VALID, silently deflating
# cost aggregates with unverifiable $0 rows. COST-axis: the solve verdict is
# preserved (native resolution is exit-code-gated); cost is unavailable and
# never priced.
REASON_ZERO_ACCOUNTING_PASS = "zero_token_accounting:passive_pass"
# Dedup reasons — assigned by the consolidator (needs whole-tree context, see
# consolidate_scores.duplicate_column_reason), defined here so the reason
# vocabulary lives in one place.
REASON_NATIVE_DRIVER_DUP = "native_driver_duplicate_column"
REASON_GENERIC_KERNEL_DUP = "generic_kernel_duplicate_column"

# ---------------------------------------------------------------------------
# Axis assignment. SOLVE-axis reasons poison BOTH axes (no trustworthy verdict
# means no attributable spend either); COST-axis reasons leave the solve
# verdict standing and mark the cost axis unavailable. The dedup reasons are
# solve-axis: one execution surfacing in two columns is a presentation fault
# on the whole cell, not a billing gap.
# ---------------------------------------------------------------------------
SOLVE_AXIS_REASONS = frozenset({
    REASON_MALFORMED, REASON_NO_ARM_RECEIPT, REASON_ARM_MISMATCH,
    REASON_DEADLINE, REASON_STALL, REASON_ZERO_WORK,
    REASON_NATIVE_DRIVER_DUP, REASON_GENERIC_KERNEL_DUP,
})
COST_AXIS_REASONS = frozenset({
    REASON_ZERO_TOKEN_INPUT, REASON_ZERO_TOKEN_OUTPUT, REASON_ZERO_BILLED,
    REASON_ZERO_ACCOUNTING_PASS,
})


def reason_axis(reason: str) -> str:
    """'solve' | 'cost' for a (possibly detail-suffixed) reason code."""
    base = reason.split(":requested", 1)[0]
    if base in COST_AXIS_REASONS:
        return "cost"
    return "solve"  # fail closed: unknown reasons poison the whole cell


# Arm names as used by runs/ directory suffixes. Order matters: "kernel-cpu"
# must be tested before "kernel" (suffix containment).
ARM_SUFFIXES = ("native", "kernel-cpu", "kernel")


class Classification(NamedTuple):
    status: str               # VALID | COST_INVALID | INVALID
    reasons: list[str]        # all reasons, both axes (empty when VALID)
    solve_valid: bool         # oracle verdict + arm receipt + not stalled
    cost_valid: bool          # real token/cost accounting present
    solve_reasons: list[str]  # solve-axis reasons (kill both axes)
    cost_reasons: list[str]   # cost-axis reasons (cost unavailable only)


def normalize_arm(arm: str | None) -> str | None:
    """Canonicalize an arm string. 'cpu' is an alias for 'kernel-cpu' (the
    consolidator's column key); anything else passes through lowercased."""
    if arm is None:
        return None
    arm = str(arm).strip().lower()
    return "kernel-cpu" if arm == "cpu" else arm


def split_agent_arm(agent: str | None) -> tuple[str, str] | None:
    """Parse '<model>-<arm>' from an agent string, e.g.
    'claude-sonnet-4-6-kernel-cpu' -> ('claude-sonnet-4-6', 'kernel-cpu').
    Returns None when no recognized arm suffix is present (e.g. the bare
    '<model>' agents written by the pre-fix arm-drop path)."""
    if not agent:
        return None
    for suffix in ARM_SUFFIXES:
        tail = "-" + suffix
        if agent.endswith(tail) and len(agent) > len(tail):
            return agent[: -len(tail)], suffix
    return None


def executed_arm(entry: dict) -> str | None:
    """The arm the payload says was EXECUTED, strongest receipt first.

    1. `agent` suffix — written by both score writers at execution time and
       always names the actual run directory (the rust writer records
       arm:'kernel' for kernel-cpu runs, but its agent field says
       '<model>-kernel-cpu'; →ARM-IDENTITY GAP, bench.rs write_score).
    2. `arm` field — accepted only when no agent receipt is parseable.
    Returns None when neither yields an arm (fail closed upstream)."""
    parsed = split_agent_arm(entry.get("agent"))
    if parsed:
        return parsed[1]
    arm = entry.get("arm")
    if arm:
        arm = normalize_arm(arm)
        return arm if arm in ARM_SUFFIXES else None
    return None


def token_accounting(entry: dict) -> tuple[int, int]:
    """(input_side, output) token accounting for a cell.

    input_side is the max of the three input-side representations that the
    two writers emit — billed_tokens, the atomic bucket sum
    (fresh + cache_read + cache_create), and the (possibly folded /
    present-but-zero) input_tokens total — so accounting counts as present
    if ANY representation carries it."""
    fresh = entry.get("fresh_input_tokens") or 0
    cache_read = entry.get("cache_read_tokens") or 0
    cache_create = entry.get("cache_create_tokens") or 0
    billed = entry.get("billed_tokens") or 0
    input_tokens = entry.get("input_tokens") or 0
    output = entry.get("output_tokens") or 0
    input_side = max(billed, fresh + cache_read + cache_create, input_tokens)
    return input_side, output


def classify_reasons(reasons: list[str]) -> Classification:
    """Split collected reasons into the two axes and derive the cell status.

    SOLVE-axis reasons → INVALID (both axes dead). COST-axis-only reasons →
    COST_INVALID (solve verdict stands; cost unavailable, never priced).
    Used by classify_cell and by the consolidator when it appends
    whole-tree reasons (dedup) after per-cell classification."""
    solve_reasons = [r for r in reasons if reason_axis(r) == "solve"]
    cost_reasons = [r for r in reasons if reason_axis(r) == "cost"]
    solve_valid = not solve_reasons
    cost_valid = not reasons  # any solve-axis fault kills cost too
    if not solve_valid:
        status = INVALID
    elif not cost_valid:
        status = COST_INVALID
    else:
        status = VALID
    return Classification(status, list(reasons), solve_valid, cost_valid,
                          solve_reasons, cost_reasons)


def classify_cell(entry: object, requested_arm: str | None = None) -> Classification:
    """Fail-closed, per-axis classification of a single loaded score payload.

    `requested_arm` is the arm implied by the runs/ directory the file was
    found in ('native' | 'kernel' | 'kernel-cpu'); pass None to skip the
    receipt check (unit pricing tests etc.).

    Rules (all reasons that apply are collected, not just the first):
      malformed        — not a dict, or missing the `benchmark` key.
      no_arm_receipt   — requested_arm given but the payload carries neither a
                         parseable agent suffix nor a recognized arm field.
      arm_mismatch     — the executed-arm receipt names a different arm than
                         requested (e.g. a '-native' agent inside a -kernel dir).
      deadline_exceeded— stop_reason contains 'deadline': an infra timeout is
                         not a model verdict. Previously silently dropped;
                         now visibly INVALID.
      zero_work        — 0 turns AND 0 tokens AND stop_reason != 'pass': the
                         agent never made an API call (container/bootstrap
                         glitch). Previously silently dropped; now INVALID.
      zero_token_accounting:no_input / :no_output —
                         a COMPLETED run (resolved, or turns/tool_uses > 0)
                         with no input-side or no output token accounting.
                         Harness-incomplete metrics (e.g. non-Anthropic native
                         arms recording 0 billed tokens) must never publish as
                         real 0-cost cells.
      zero_token_accounting:zero_billed —
                         a COMPLETED run whose payload carries a billed_tokens
                         key that is 0 with all atomic buckets 0 (deepseek/
                         grok/devstral native): the harness never recorded
                         billing, and a folded input_tokens alone is not a
                         billing receipt. Such cells were previously accepted
                         (input_tokens > 0 rescued them) and priced at the
                         full rate card — a synthesized cost that over-stated
                         native spend vs the provider-billed truth. INVALID,
                         per the trust doctrine — never a fabricated number.
                         Payloads WITHOUT a billed_tokens key (pre-→2062
                         legacy writers) are exempt: their input_tokens IS the
                         billing-era record.
      zero_token_accounting:passive_pass —
                         stop_reason == 'pass' with 0 turns AND 0 tokens on
                         both sides is a zero-ACCOUNTING pass. A genuine
                         passive-verify $0 cell is INDISTINGUISHABLE from a run
                         whose real spend was lost (the native error-result
                         writer stamps error_max_turns runs as a 0-turn/
                         0-token 'pass' — see REASON_ZERO_ACCOUNTING_PASS), so
                         it fails closed on COST: COST_INVALID, solve verdict
                         preserved, cost axis unavailable and never priced.
                         (Formerly a passive_verify exemption kept these fully
                         VALID — the P0 cost-accounting defect this replaces.)

    AXIS SPLIT: malformed / no_arm_receipt / arm_mismatch / deadline /
    zero_work are SOLVE-axis reasons → the cell is fully INVALID. The four
    zero_token_accounting reasons are COST-axis reasons → the cell is
    COST_INVALID: its oracle verdict and arm receipt stand (it enters solve
    aggregates) while its cost axis is UNAVAILABLE (excluded from cost/token
    aggregates, never priced). See module docstring.
    """
    if not isinstance(entry, dict) or "benchmark" not in entry:
        return classify_reasons([REASON_MALFORMED])

    reasons: list[str] = []

    if requested_arm is not None:
        req = normalize_arm(requested_arm)
        executed = executed_arm(entry)
        if executed is None:
            reasons.append(REASON_NO_ARM_RECEIPT)
        elif executed != req:
            reasons.append(f"{REASON_ARM_MISMATCH}:requested={req},executed={executed}")

    stop = (entry.get("stop_reason") or "").lower()
    turns = entry.get("turns_to_fix") or 0
    tool_uses = entry.get("tool_uses") or 0
    input_side, output = token_accounting(entry)

    if "deadline" in stop:
        reasons.append(REASON_DEADLINE)
    elif "stall" in stop:
        # stall_watch killed a no-progress run and wrote the cell as INVALID
        # reason 'stall' — an infra verdict, never a model verdict.
        reasons.append(REASON_STALL)
    elif turns == 0 and input_side + output == 0 and stop != "pass":
        reasons.append(REASON_ZERO_WORK)
    elif turns == 0 and input_side + output == 0:
        # stop == "pass" here (the zero_work branch above consumed the
        # stop != "pass" case). A "pass" that recorded ZERO turns and ZERO
        # tokens is a zero-ACCOUNTING pass — COST-axis UNAVAILABLE, not a free
        # VALID cell. The solve verdict stands (native resolution is
        # exit-code-gated), but the cost axis is excluded from aggregates and
        # never priced. See REASON_ZERO_ACCOUNTING_PASS. This REPLACES the
        # former passive_verify exemption, which blessed lost-telemetry $0
        # rows (e.g. native error_max_turns results) as fully VALID.
        reasons.append(REASON_ZERO_ACCOUNTING_PASS)
    else:
        completed = bool(entry.get("resolved")) or turns > 0 or tool_uses > 0
        if completed:
            bucket_sum = ((entry.get("fresh_input_tokens") or 0)
                          + (entry.get("cache_read_tokens") or 0)
                          + (entry.get("cache_create_tokens") or 0))
            if input_side == 0:
                reasons.append(REASON_ZERO_TOKEN_INPUT)
            elif output == 0:
                reasons.append(REASON_ZERO_TOKEN_OUTPUT)
            elif ("billed_tokens" in entry
                  and (entry.get("billed_tokens") or 0) == 0
                  and bucket_sum == 0):
                # Billing-aware writer recorded NO billing and NO buckets on a
                # completed run — harness-incomplete (see REASON_ZERO_BILLED).
                reasons.append(REASON_ZERO_BILLED)

    return classify_reasons(reasons)
