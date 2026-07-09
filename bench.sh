#!/usr/bin/env bash
# bench.sh — one-command operator path: preflight -> run -> validate -> board.
#
#   ./bench.sh <model-id> [--arms native,kernel-cpu] [--bench a,b] [--dry-run]
#              [--offline] [--no-board]
#   ./bench.sh --status          # live cell states (stall_watch ledger)
#
# Flow (fails closed at every step):
#   1. PREFLIGHT   scripts/preflight.py — roster resolution, provider env
#                  key(s) for the model's actual routing, LIVE provider
#                  probes (free models-list call: key validity + exact model
#                  id existence, fail-closed with closest matches; skipped by
#                  --offline; --dry-run implies --offline), docker daemon,
#                  frozen-bin content-hash pin + local musl binary (hash
#                  recorded to runs/.binary_identity.jsonl). A failed
#                  preflight exits nonzero BEFORE any container/API activity.
#   2. RUN         run_matrix_v41.py — the existing resumable matrix runner
#                  (per-cell retries, infra quarantine, F7 sampling), with
#                  scripts/stall_watch.py wrapped around EVERY cell: journal
#                  completion detection (kernel arms), no-progress watchdog
#                  (WARN 90s / probe 180s / SIGKILL 300s -> INVALID 'stall'),
#                  per-cell hard deadline from the bench's own budget. Every
#                  transition lands timestamped in runs/.cell_status.jsonl —
#                  './bench.sh --status' renders it live. Re-run the same
#                  bench.sh command to resume after interruption.
#   3. POSTFLIGHT  scripts/validate.py — cell_validity over this model's
#                  runs/ dirs; any INVALID cell is reported loudly with
#                  reasons and aborts the pipeline (nonzero exit; the board
#                  is NOT regenerated over untrusted cells).
#   4. CONSOLIDATE consolidate_scores.py — fail-closed aggregation into
#                  public/scores.json + public/experiment-scores.json.
#   5. BOARD       npm run build — astro copies public/ into dist/.
#
# --bench a,b restricts the run to specific benchmarks (narrow/smoke runs).
# --dry-run walks the whole plan (which benchmarks, which arms, estimated
# cell count) with ZERO network/docker activity, then prints what steps
# 3-5 would do. See RUNBOOK.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    # Print the whole header comment block (line 2 -> first non-# line) so
    # --help can't silently truncate again when the header grows.
    awk 'NR < 2 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

MODEL=""
ARMS=""
BENCHES=""
DRY_RUN=0
NO_BOARD=0
OFFLINE=0

while (( $# )); do
    case "$1" in
        --arms)     ARMS="${2:?--arms needs a value}"; shift 2 ;;
        --arms=*)   ARMS="${1#--arms=}"; shift ;;
        --bench)    BENCHES="${2:?--bench needs a value}"; shift 2 ;;
        --bench=*)  BENCHES="${1#--bench=}"; shift ;;
        --dry-run)  DRY_RUN=1; shift ;;
        --offline)  OFFLINE=1; shift ;;
        --no-board) NO_BOARD=1; shift ;;
        --status)   exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/stall_watch.py" --status ;;
        -h|--help)  usage; exit 0 ;;
        -*)         echo "unknown flag: $1" >&2; usage >&2; exit 2 ;;
        *)          if [ -z "$MODEL" ]; then MODEL="$1"; shift
                    else echo "unexpected argument: $1" >&2; exit 2; fi ;;
    esac
done
if [ -z "$MODEL" ]; then usage >&2; exit 2; fi

PLAN_FILE="$(mktemp "${TMPDIR:-/tmp}/bench-plan.XXXXXX")"
trap 'rm -f "$PLAN_FILE"' EXIT

# ---------------------------------------------------------------- 1. PREFLIGHT
echo "=== [1/5] PREFLIGHT: $MODEL ==="
PRE_ARGS=("$MODEL" --plan-out "$PLAN_FILE")
[ -n "$ARMS" ]     && PRE_ARGS+=(--arms "$ARMS")
[ "$DRY_RUN" = 1 ] && PRE_ARGS+=(--dry-run)
[ "$OFFLINE" = 1 ] && PRE_ARGS+=(--offline)
# set -e: a nonzero preflight aborts here — before any container/API activity.
python3 "$ROOT/scripts/preflight.py" "${PRE_ARGS[@]}"

FRONTIER="$(python3 -c 'import json,sys; print(1 if json.load(open(sys.argv[1]))["frontier"] else 0)' "$PLAN_FILE")"
ARM_LIST="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["arms"]))' "$PLAN_FILE")"

# ---------------------------------------------------------------------- 2. RUN
echo
echo "=== [2/5] RUN: resumable matrix (arms: $ARM_LIST) ==="
RUN_CMD=(python3 "$ROOT/run_matrix_v41.py" --model "$MODEL")
[ "$FRONTIER" = 1 ] && RUN_CMD+=(--frontier)
for a in $ARM_LIST; do RUN_CMD+=(--arm "$a"); done
if [ -n "$BENCHES" ]; then
    for b in ${BENCHES//,/ }; do RUN_CMD+=(--bench "$b"); done
fi
[ "$DRY_RUN" = 1 ] && RUN_CMD+=(--dry-run)

set +e
"${RUN_CMD[@]}"
RUN_RC=$?
set -e
if [ "$RUN_RC" -ne 0 ]; then
    echo "[bench.sh] runner exited $RUN_RC (hard-failed cells) — validating what completed, then failing closed." >&2
fi

# --------------------------------------------------------------- 3. POSTFLIGHT
echo
echo "=== [3/5] POSTFLIGHT VALIDATE (cell_validity, fail-closed) ==="
if [ "$DRY_RUN" = 1 ]; then
    echo "[dry-run] would run: scripts/validate.py runs/ --model $MODEL"
else
    # Exits nonzero if any publishable INVALID cell exists for this model —
    # the board is never regenerated over untrusted cells.
    python3 "$ROOT/scripts/validate.py" "$ROOT/runs" --model "$MODEL"
fi

if [ "$RUN_RC" -ne 0 ]; then
    echo "[bench.sh] FAIL CLOSED: runner had hard failures (exit $RUN_RC); board not regenerated." >&2
    exit "$RUN_RC"
fi

# -------------------------------------------------------------- 4. CONSOLIDATE
echo
echo "=== [4/5] CONSOLIDATE ==="
if [ "$DRY_RUN" = 1 ]; then
    echo "[dry-run] would run: python3 consolidate_scores.py  (writes public/scores.json + public/experiment-scores.json)"
else
    python3 "$ROOT/consolidate_scores.py"
fi

# -------------------------------------------------------------------- 5. BOARD
echo
echo "=== [5/5] BOARD REGEN ==="
if [ "$DRY_RUN" = 1 ]; then
    echo "[dry-run] would run: npm run build  (astro: public/ -> dist/)"
    echo
    echo "dry-run complete — no container, API, or docker activity occurred."
    exit 0
fi
if [ "$NO_BOARD" = 1 ]; then
    echo "[skip] --no-board: public/*.json updated; dist/ NOT rebuilt (run 'npm run build' to publish)."
    exit 0
fi
if ! command -v npm >/dev/null 2>&1; then
    echo "[bench.sh] FAIL CLOSED: npm not found — board NOT regenerated (public/*.json are updated; run 'npm run build' where npm is available, or pass --no-board to accept this)." >&2
    exit 1
fi
( cd "$ROOT" && npm run build )
echo
echo "bench.sh complete: $MODEL — board regenerated in dist/."
