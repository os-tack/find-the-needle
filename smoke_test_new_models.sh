#!/usr/bin/env bash
# Smoke-test new model IDs before the full matrix.
# Run ONE cheap benchmark (null-pointer-config, Easy) on each new model × applicable arms.
# Confirms: OpenRouter ID resolves, docker build works, vendor CLI/provider wiring is correct.
#
# Each cell ~1-3 minutes. Total ~30-60 min.
set -uo pipefail
cd "$(dirname "$0")"

BENCH="null-pointer-config"  # cheap, Easy tier

# FRONTIER_2026 (approach-a Control + B sweep) — must match FRONTIER_2026 in
# run_matrix_v41.py. Pre-flight: one Easy bench per model × its tier arms.
#   Tier-1: native + kernel-cpu (true B, native CpuDriver)
#   Tier-2: native + kernel     (B* generic OpenRouter)
declare -a TIER1=(
  "claude-opus-4-8"
  "claude-sonnet-4-6"
  "gpt-5.5"
  "gemini-3.1-pro-preview"
  "devstral-2512"
)
declare -a TIER2=(
  "grok-4.3"
  "deepseek-v4-pro"
  "kimi-k2.6"
  "qwen3-coder"
)

PASS=0
FAIL=0
RESULTS=()

run_one() {
  local model="$1" arm_label="$2" arm_flags="$3"
  local score_file="runs/${model}-${arm_label}/${BENCH}.score.json"
  printf "%-30s %-12s " "$model" "$arm_label"
  if [ -f "$score_file" ]; then
    echo "SKIP (score exists)"
    return 0
  fi
  # shellcheck disable=SC2086
  if ostk bench "$BENCH" --model "$model" $arm_flags --docker >/tmp/smoke-$$.log 2>&1; then
    if [ -f "$score_file" ]; then
      echo "PASS"
      PASS=$((PASS + 1))
      RESULTS+=("PASS $model / $arm_label")
    else
      echo "FAIL (no score file)"
      FAIL=$((FAIL + 1))
      RESULTS+=("FAIL $model / $arm_label (no score)")
      tail -5 /tmp/smoke-$$.log | sed 's/^/    /'
    fi
  else
    echo "FAIL"
    FAIL=$((FAIL + 1))
    RESULTS+=("FAIL $model / $arm_label")
    tail -5 /tmp/smoke-$$.log | sed 's/^/    /'
  fi
  rm -f /tmp/smoke-$$.log
}

echo "=== smoke test: $BENCH on new models ==="
echo "=== started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""

# Tier-1: Control (native) + true B (kernel-cpu)
for model in "${TIER1[@]}"; do
  run_one "$model" "native"     "--arm native --local"
  run_one "$model" "kernel-cpu" "--arm kernel --driver cpu --local"
done

# Tier-2: Control (native) + B* generic (kernel via OpenRouter)
for model in "${TIER2[@]}"; do
  run_one "$model" "native"     "--arm native --local"
  run_one "$model" "kernel"     "--arm kernel --local"
done

echo ""
echo "=== smoke test summary ==="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo ""
if [ "$FAIL" -gt 0 ]; then
  echo "Failing cells:"
  for r in "${RESULTS[@]}"; do
    [[ "$r" == FAIL* ]] && echo "  $r"
  done
fi

exit $FAIL
