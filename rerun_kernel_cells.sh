#!/usr/bin/env bash
# runner-1: re-run ONLY the kernel-cpu + kernel (B/B*) cells against the PATCHED musl
# (after harness-dev's I4 normalize-before-gate fix). Native cells are NOT touched —
# their smoke results are already valid (vendor CLI, not affected by the musl/I4 bug).
#
# Invoke under: PATH="$HOME/projects/haystack/target/release:$PATH"
# (the kernel arm reads the patched musl via local_linux_binary path under --local)
#
# SCOPE selector (per team-lead — OpenRouter now FUNDED, $147.85 remaining):
#   SCOPE=confirm6         -> 6 confirmation cells proving BOTH the I4 patch AND that credit flows:
#                             - 4 native-provider kernel-cpu (I4-patch proof on native keys):
#                               claude-opus-4-8, claude-sonnet-4-6, gemini-3.1-pro-preview, devstral-2512
#                             - gpt-5.5 kernel-cpu (Tier-1 via OpenRouter — patch + credit together)
#                             - kimi-k2.6 kernel (Tier-2 B* via OpenRouter — route+resolve)
#                             Remaining 4 cells are covered by the full sweep (#4). (default)
#   SCOPE=native-provider  -> only the 4 native-provider kernel-cpu cells.
#   SCOPE=all              -> all 10 kernel cells (6 Tier-1 kernel-cpu + 4 Tier-2 kernel).
set -uo pipefail
cd "$(dirname "$0")"

BENCH="null-pointer-config"
CONC="${CONC:-3}"
SCOPE="${SCOPE:-confirm6}"
LOGDIR="rerun-kernel-logs"
mkdir -p "$LOGDIR"

# model|arm_label|arm_flags
declare -a NATIVE_PROVIDER=(
  "claude-opus-4-8|kernel-cpu|--arm kernel --driver cpu --local"
  "claude-sonnet-4-6|kernel-cpu|--arm kernel --driver cpu --local"
  "gemini-3.1-pro-preview|kernel-cpu|--arm kernel --driver cpu --local"
  "devstral-2512|kernel-cpu|--arm kernel --driver cpu --local"
)
declare -a OPENROUTER_ROUTED=(
  "gpt-5.5|kernel-cpu|--arm kernel --driver cpu --local"
  # gpt-5.5-pro dropped 2026-06-14 (owner: too expensive at $30/$180 API list; B* only).
  "grok-4.3|kernel|--arm kernel --local"
  "deepseek-v4-pro|kernel|--arm kernel --local"
  "kimi-k2.6|kernel|--arm kernel --local"
  "qwen3-coder|kernel|--arm kernel --local"
)

CELLS=("${NATIVE_PROVIDER[@]}")
case "$SCOPE" in
  confirm6) CELLS+=("gpt-5.5|kernel-cpu|--arm kernel --driver cpu --local" \
                    "kimi-k2.6|kernel|--arm kernel --local" \
                    "kimi-k2.6|native|--arm native --local") ;;  # +gpt-5.5(Tier-1 OR), kimi kernel(B*), kimi NATIVE(FIX#5 context.jsonl path)
  all)      CELLS+=("${OPENROUTER_ROUTED[@]}") ;;
  native-provider) : ;;  # 4 only
esac

run_cell() {
  local spec="$1"
  local model="${spec%%|*}"; local rest="${spec#*|}"
  local arm_label="${rest%%|*}"; local arm_flags="${rest#*|}"
  local log="$LOGDIR/${model}-${arm_label}.log"
  local score_file="runs/${model}-${arm_label}/${BENCH}.score.json"
  # Always re-run: delete any stale score so we measure the patched binary, not a cached pass/fail.
  rm -f "$score_file"
  # shellcheck disable=SC2086
  ostk bench "$BENCH" --model "$model" $arm_flags --docker >"$log" 2>&1
  local rc=$?
  if [ -f "$score_file" ]; then
    local res it tu sr
    res=$(jq -r '.resolved // false' "$score_file" 2>/dev/null)
    it=$(jq -r '.input_tokens // 0' "$score_file" 2>/dev/null)
    tu=$(jq -r '(.summary // "") | (capture("(?<n>[0-9]+) tools").n // "?")' "$score_file" 2>/dev/null)
    sr=$(jq -r '.stop_reason // "?"' "$score_file" 2>/dev/null)
    echo "DONE|$model|$arm_label|resolved=$res in=$it tools=$tu stop=$sr rc=$rc"
  else
    echo "NOSCORE|$model|$arm_label|rc=$rc (see $log)"
  fi
}
export -f run_cell
export BENCH LOGDIR

echo "=== kernel re-run (patched musl) scope=$SCOPE conc=$CONC started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "=== ostk version: $(ostk --version) ==="
printf '%s\n' "${CELLS[@]}" | xargs -P "$CONC" -I {} bash -c 'run_cell "$@"' _ {} | tee "$LOGDIR/_results.txt"
echo "=== kernel re-run finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
