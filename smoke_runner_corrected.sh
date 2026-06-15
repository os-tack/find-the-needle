#!/usr/bin/env bash
# runner-1 smoke pre-flight (task #3) — corrected model IDs.
# Mirrors smoke_test_new_models.sh but:
#   - applies the 2 ID fixes found in pre-flight (gemini-3.1-pro-preview, deepseek-v4-pro)
#   - runs cells with bounded concurrency (CONC) since the base image already exists
#   - captures a per-cell log under smoke-logs/ for triage
#   - does NOT retry KEY-BLOCKED Tier-2 native cells
# Invoked under: PATH="$HOME/projects/haystack/target/release:$PATH"
set -uo pipefail
cd "$(dirname "$0")"

BENCH="null-pointer-config"
CONC="${CONC:-3}"
LOGDIR="smoke-logs"
mkdir -p "$LOGDIR"

# Tier-1: native (Control) + kernel-cpu (true B). gemini ID corrected.
declare -a TIER1=(
  "claude-opus-4-8"
  "claude-sonnet-4-6"
  "gpt-5.5"
  "gemini-3.1-pro-preview"
  "devstral-2512"
)
# gpt-5.5-pro dropped 2026-06-14 (owner: too expensive at $30/$180 API list; B* only).
# Tier-2: native (Control, KEY-BLOCKED for grok/deepseek/kimi/qwen) + kernel (B* OpenRouter).
# deepseek ID corrected.
declare -a TIER2=(
  "grok-4.3"
  "deepseek-v4-pro"
  "kimi-k2.6"
  "qwen3-coder"
)

# Build the cell list: "model|arm_label|arm_flags"
CELLS=()
for m in "${TIER1[@]}"; do
  CELLS+=("$m|native|--arm native --local")
  CELLS+=("$m|kernel-cpu|--arm kernel --driver cpu --local")
done
for m in "${TIER2[@]}"; do
  CELLS+=("$m|native|--arm native --local")
  CELLS+=("$m|kernel|--arm kernel --local")
done

run_cell() {
  local spec="$1"
  local model="${spec%%|*}"; local rest="${spec#*|}"
  local arm_label="${rest%%|*}"; local arm_flags="${rest#*|}"
  local log="$LOGDIR/${model}-${arm_label}.log"
  local score_file="runs/${model}-${arm_label}/${BENCH}.score.json"
  if [ -f "$score_file" ]; then
    echo "SKIP|$model|$arm_label|score exists"
    return 0
  fi
  # shellcheck disable=SC2086
  ostk bench "$BENCH" --model "$model" $arm_flags --docker >"$log" 2>&1
  local rc=$?
  if [ -f "$score_file" ]; then
    local it
    it=$(jq -r '.input_tokens // 0' "$score_file" 2>/dev/null)
    echo "PASS|$model|$arm_label|input_tokens=$it rc=$rc"
  else
    echo "FAIL|$model|$arm_label|no-score rc=$rc"
  fi
}
export -f run_cell
export BENCH LOGDIR

echo "=== smoke (corrected IDs) started $(date -u +%Y-%m-%dT%H:%M:%SZ) conc=$CONC ==="
printf '%s\n' "${CELLS[@]}" | xargs -P "$CONC" -I {} bash -c 'run_cell "$@"' _ {} | tee "$LOGDIR/_results.txt"
echo "=== smoke finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
