#!/usr/bin/env python3
"""
cell_validity.py — fail-closed cell classification for the needle-bench score pipeline.

TRUST DOCTRINE: the bench FAILS CLOSED. A cell with missing/zero token
accounting on a completed run, a mismatched requested-vs-executed arm, or a
malformed payload is INVALID — excluded from aggregates, counted visibly.
Never a silent 0, never a silent pass.

Every (model, benchmark, arm) cell is one of THREE states — the distinction
between the last two is the whole point of this module:

  VALID    score file present, well-formed, the arm receipt matches the
           requested arm, and token accounting is real for the work claimed.
           Enters aggregates.
  INVALID  score file present but untrustworthy. Excluded from ALL published
           aggregates and counted VISIBLY per model/arm with a reason code.
  absent   no score file at all — the cell was legitimately never run.
           Absent is NOT invalid: it contributes nothing, is not counted as
           a failure, and shows up only as a missing cell.

Importable by consolidate_scores.py (and any future consolidator); stdlib only.
"""

from __future__ import annotations

from typing import NamedTuple

VALID = "valid"
INVALID = "invalid"

# ---------------------------------------------------------------------------
# Reason codes (stable strings — they key the board's invalid_by_reason
# histogram, so treat them as a public vocabulary).
# ---------------------------------------------------------------------------
REASON_MALFORMED = "malformed_payload"            # unparseable / not a score dict
REASON_NO_ARM_RECEIPT = "no_arm_receipt"          # payload carries no executed-arm evidence
REASON_ARM_MISMATCH = "arm_mismatch"              # requested arm != executed arm
REASON_DEADLINE = "deadline_exceeded"             # infra deadline, not a model verdict
REASON_ZERO_WORK = "zero_work"                    # 0 turns + 0 tokens (bootstrap race etc.)
REASON_ZERO_TOKEN_INPUT = "zero_token_accounting:no_input"    # completed run, no input-side tokens
REASON_ZERO_TOKEN_OUTPUT = "zero_token_accounting:no_output"  # completed run, no output tokens
# Completed run whose writer RECORDS billing (billed_tokens key present) but
# billed it as 0 with empty atomic buckets — the harness-incomplete class
# (deepseek/grok/devstral native): the only surviving number is an
# unverifiable folded input_tokens, so the cell is untrustworthy for cost.
REASON_ZERO_BILLED = "zero_token_accounting:zero_billed"
# Dedup reasons — assigned by the consolidator (needs whole-tree context, see
# consolidate_scores.duplicate_column_reason), defined here so the reason
# vocabulary lives in one place.
REASON_NATIVE_DRIVER_DUP = "native_driver_duplicate_column"
REASON_GENERIC_KERNEL_DUP = "generic_kernel_duplicate_column"

# Arm names as used by runs/ directory suffixes. Order matters: "kernel-cpu"
# must be tested before "kernel" (suffix containment).
ARM_SUFFIXES = ("native", "kernel-cpu", "kernel")


class Classification(NamedTuple):
    status: str          # VALID | INVALID
    reasons: list[str]   # empty when VALID


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


def classify_cell(entry: object, requested_arm: str | None = None) -> Classification:
    """Fail-closed classification of a single loaded score payload.

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
      passive-verify exception — stop_reason == 'pass' with 0 turns and 0
                         tokens is a legitimate zero-cost cell (a handful of
                         benches resolve via passive verification with no
                         agent turns) and stays VALID.
    """
    if not isinstance(entry, dict) or "benchmark" not in entry:
        return Classification(INVALID, [REASON_MALFORMED])

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
    elif turns == 0 and input_side + output == 0 and stop != "pass":
        reasons.append(REASON_ZERO_WORK)
    else:
        completed = bool(entry.get("resolved")) or turns > 0 or tool_uses > 0
        passive_verify = stop == "pass" and turns == 0 and input_side == 0 and output == 0
        if completed and not passive_verify:
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

    if reasons:
        return Classification(INVALID, reasons)
    return Classification(VALID, [])
