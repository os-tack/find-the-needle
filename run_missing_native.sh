#!/usr/bin/env bash
# Run missing benchmarks on NATIVE arm for non-driver models.
# (Their kernel runs are handled by run_missing.sh kernel-only groups)
set -euo pipefail
cd "$(dirname "$0")"

BENCHMARKS=(
  api-version-field-drop
  auth-bypass-path-traversal
  compiler-macro-expansion
  data-corruption-concurrent-write
  deadlock-transfer
  encoding-mojibake
  goroutine-leak-handler
  linearizability-stale-read
  performance-cliff-hash
  raft-snapshot-commit-gap
  silent-data-corruption
  split-brain-leader-election
  sql-injection-search
  timing-attack-comparison
  tls-chain-ordering-strict
  wal-fsync-ghost-ack
)

GROUP="${1:?Usage: $0 <group>}"

case "$GROUP" in
  openai)   MODELS=(gpt-4.1 gpt-5-codex o3 o4-mini) ;;
  xai)      MODELS=(grok-3 grok-3-fast grok-3-mini grok-4 grok-4-fast grok-4.1-fast grok-4.20 grok-code-fast-1) ;;
  deepseek) MODELS=(deepseek-r1-0528 deepseek-v3.2) ;;
  other)    MODELS=(qwen3-coder qwen3-coder-flash qwen3-coder-plus kimi-k2.5 llama-4-maverick) ;;
  *) echo "Unknown group: $GROUP"; exit 1 ;;
esac

echo "=== needle-bench fleet: native-$GROUP started=$(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

for model in "${MODELS[@]}"; do
  for bench in "${BENCHMARKS[@]}"; do
    score_file="runs/${model}-native/${bench}.score.json"
    if [ -f "$score_file" ]; then
      echo "SKIP $model / $bench / native (exists)"
      continue
    fi
    echo "RUN  $model / $bench / native"
    if ! ostk bench "$bench" --model "$model" --arm native --local --docker --keep 2>&1; then
      echo "WARN: failed $model / $bench / native"
    fi
  done
done

echo "=== done: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
