#!/bin/bash
# parallel_sweep.sh — N parallel run_matrix_v41.py workers, sharded by model.
#
# Each worker handles a disjoint slice of the roster, so writes to
# state.jsonl / score.json / samples.json don't collide. Container
# names are timestamped → no docker-name collisions.
#
# Usage:
#   ./parallel_sweep.sh             # default N=10, full matrix
#   ./parallel_sweep.sh 8 priority  # 8 workers, priority-only roster
#
# Logs land in runs/sweep-worker-<i>.log.
set -euo pipefail
cd "$(dirname "$0")"

N="${1:-10}"
SCOPE="${2:-full}"

# Full roster (mirrors run_matrix_v41.py EXISTING_*ARM + NEW_*ARM lists)
FULL_ROSTER=(
  claude-haiku-4-5 claude-sonnet-4-6 claude-opus-4-6 claude-opus-4-7
  devstral-2512 devstral-medium devstral-small-latest
  kimi-k2.5 kimi-k2.6
  gemini-2.5-flash gemini-2.5-pro gemini-3-flash-preview
  gemini-3.1-pro-preview gemini-3.1-flash-lite-preview
  codestral-2508 gpt-4.1 gpt-5-codex
  gpt-5.2 gpt-5.4 gpt-5.5 gpt-5.5-pro
  o3 o4-mini
  grok-3 grok-3-mini grok-4 grok-4-fast
  grok-4.1-fast grok-4.20 grok-code-fast-1
  deepseek-r1 deepseek-r1-0528 deepseek-v3.2
  qwen3-coder qwen3-coder-flash qwen3-coder-plus
  llama-4-maverick
)

PRIORITY_ROSTER=(
  claude-opus-4-7 gpt-5.2 gpt-5.4 gpt-5.5 gpt-5.5-pro
  kimi-k2.6 gemini-3.1-flash-lite-preview claude-haiku-4-5
  gemini-3.1-pro-preview gpt-5-codex
)

if [ "$SCOPE" = "priority" ]; then
  ROSTER=("${PRIORITY_ROSTER[@]}")
else
  ROSTER=("${FULL_ROSTER[@]}")
fi

echo "[parallel] N=$N workers, scope=$SCOPE, roster=${#ROSTER[@]} models"
mkdir -p runs/parallel-sweep

# Distribute models round-robin across N workers
for i in $(seq 0 $((N-1))); do
  CHUNK=()
  for j in $(seq 0 $((${#ROSTER[@]}-1))); do
    if [ $((j % N)) -eq "$i" ]; then
      CHUNK+=("${ROSTER[$j]}")
    fi
  done
  if [ "${#CHUNK[@]}" -eq 0 ]; then continue; fi
  ARGS=()
  for m in "${CHUNK[@]}"; do ARGS+=(--model "$m"); done
  LOG="runs/parallel-sweep/worker-$i.log"
  nohup python3 -u run_matrix_v41.py \
    --ostk-version-gate 4.6 \
    --samples 1 \
    "${ARGS[@]}" \
    > "$LOG" 2>&1 &
  PID=$!
  echo "[parallel] worker $i pid=$PID models=${CHUNK[*]}"
  echo "$PID" > "runs/parallel-sweep/worker-$i.pid"
done

echo "[parallel] all $N workers launched. Logs at runs/parallel-sweep/worker-*.log"
echo "[parallel] kill all: pkill -KILL -f run_matrix_v41.py"
