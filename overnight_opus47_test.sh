#!/usr/bin/env bash
# Single-cell overnight test:
#   claude-opus-4-7 / null-pointer-config / kernel / --driver cpu / --local
# Run on ostk v4.1.0. Logs everything for morning review.
set -uo pipefail
cd "$(dirname "$0")"

LOG="overnight_opus47_test.log"
SCORE="runs/claude-opus-4-7-kernel-cpu/null-pointer-config.score.json"

{
  echo "================================================================"
  echo "  OVERNIGHT TEST: claude-opus-4-7 / null-pointer-config"
  echo "                  arm=kernel  driver=cpu  --local"
  echo "                  ostk $(ostk --version 2>/dev/null)"
  echo "  started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "================================================================"
  echo ""
  echo "--- pre-flight ---"
  echo "ostk processes (should be empty):"
  pgrep -lf "^ostk " | grep -v keyboxd || echo "  none"
  echo ""
  echo "binary:"
  ls -la ~/projects/haystack/target/x86_64-unknown-linux-musl/release/ostk
  echo ""
  echo "--- bench invocation ---"
  START=$(date +%s)
  ostk bench null-pointer-config \
    --model claude-opus-4-7 \
    --arm kernel \
    --driver cpu \
    --local \
    --docker
  EXIT=$?
  END=$(date +%s)
  echo ""
  echo "--- bench result ---"
  echo "exit code:  $EXIT"
  echo "wall time:  $((END - START))s"
  echo "finished:   $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo ""
  echo "--- score file ---"
  if [ -f "$SCORE" ]; then
    echo "PATH: $SCORE"
    echo "SIZE: $(wc -c < "$SCORE") bytes"
    echo ""
    echo "CONTENTS:"
    cat "$SCORE" | python3 -m json.tool 2>&1 || cat "$SCORE"
    echo ""
    echo "--- SPEC invariant 5 audit ---"
    python3 -c "
import json
SPEC = ['resolved','turns_to_fix','input_tokens','output_tokens','estimated_cost_usd',
        'tool_uses','wall_clock','summary','stop_reason','arm','benchmark','model','timestamp']
d = json.load(open('$SCORE'))
for k in SPEC:
    mark = chr(10003) if k in d else chr(10007)
    print(f'  {mark}  {k}')
extra = sorted(set(d) - set(SPEC))
if extra: print(f'  extra fields: {extra}')
"
  else
    echo "SCORE FILE NOT WRITTEN — fix did not apply or model unsupported"
  fi
  echo ""
  echo "--- post-flight ---"
  echo "remaining ostk processes (should be empty if clean exit):"
  pgrep -lf "^ostk " | grep -v keyboxd || echo "  none"
  echo "remaining containers:"
  docker ps --filter name=nb-null-pointer-config-kernel-cpu-claude-opus-4-7 --format '  {{.Status}} {{.Names}}'
  echo ""
  echo "================================================================"
  echo "  END OVERNIGHT TEST"
  echo "================================================================"
} > "$LOG" 2>&1
