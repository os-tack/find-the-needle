#!/usr/bin/env bash
# flakiness_sweep.sh — benchmark-validity flakiness audit (config-dev, task #10 / #3).
#
# For each benchmark, run its test.sh N times on the UNMODIFIED workspace
# (NO agent, NO API cost — just build the image, cached, and run test.sh).
# ANY spurious PASS on the unmodified broken scenario = flaky false-positive
# = the bench can be "resolved" without a fix = EXCLUDE from the headline.
#
# Mirrors the real harness resolution check (haystack bench.rs resolution_test_cmd):
#   git config --global --add safe.directory '*'; export GOFLAGS=-buildvcs=false; cd <workdir> && bash test.sh
# The workdir is the image's configured WORKDIR (read via docker inspect).
#
# Output: a per-benchmark flakiness profile (spurious_passes/N) to stdout and
# flakiness-profile.tsv. ANY bench with spurious_passes>=1 is INVALID.
#
# DOCKER-HEAVY — run only when the main API sweep has freed up docker.
# Honors a low concurrency cap to avoid thrashing the runners.
#
# Usage:
#   ./flakiness_sweep.sh                 # N=8, all 40 benches, CONC=2
#   N=5 CONC=1 ./flakiness_sweep.sh      # tune samples / concurrency
#   ./flakiness_sweep.sh bench-a bench-b # only the named benches
set -uo pipefail
cd "$(dirname "$0")"

N="${N:-8}"            # samples per benchmark on the unmodified workspace
CONC="${CONC:-2}"      # max benchmarks built/run in parallel (keep low; runners use docker)
OUT="flakiness-profile.tsv"
LOGDIR="flakiness-logs"
mkdir -p "$LOGDIR"

# Bench list: args, else all non-_ benchmark dirs.
if [ "$#" -gt 0 ]; then
  BENCHES=("$@")
else
  BENCHES=()
  for d in benchmarks/*/; do
    b="$(basename "$d")"
    [ "${b#_}" != "$b" ] && continue   # skip _-prefixed
    [ -f "$d/Dockerfile" ] && [ -f "$d/test.sh" ] && BENCHES+=("$b")
  done
fi

audit_one() {
  local bench="$1" n="$2"
  local dir="benchmarks/$bench"
  local img="nb-flake-$bench"
  local log="$LOGDIR/$bench.log"
  : > "$log"
  # Build (cached after first time). Quiet unless it errors.
  if ! docker build -q -t "$img" "$dir" >>"$log" 2>&1; then
    echo "$bench	BUILD_FAIL	0	$n	ERROR"
    return
  fi
  # Resolve the image's configured WORKDIR (fallback /workspace).
  local workdir
  workdir="$(docker image inspect -f '{{.Config.WorkingDir}}' "$img" 2>/dev/null)"
  [ -z "$workdir" ] && workdir="/workspace"
  local cmd="git config --global --add safe.directory '*' 2>/dev/null; export GOFLAGS=-buildvcs=false; cd $workdir && bash test.sh"
  local passes=0 runs=0
  for i in $(seq 1 "$n"); do
    runs=$((runs+1))
    if docker run --rm "$img" sh -c "$cmd" >>"$log" 2>&1; then
      passes=$((passes+1))   # exit 0 on the UNMODIFIED scenario = spurious pass
      echo "  [trial $i] SPURIOUS PASS (exit 0 on unmodified)" >>"$log"
    else
      echo "  [trial $i] fail (bug detected, expected)" >>"$log"
    fi
  done
  docker rmi "$img" >/dev/null 2>&1 || true
  local verdict="VALID"
  [ "$passes" -ge 1 ] && verdict="INVALID(flaky)"
  echo "$bench	$verdict	$passes	$runs	spurious_pass_rate=$passes/$runs"
}
export -f audit_one
export LOGDIR

echo "=== flakiness sweep: ${#BENCHES[@]} benches × N=$N samples, CONC=$CONC ==="
echo "=== started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
printf 'benchmark\tverdict\tspurious_passes\truns\tnote\n' > "$OUT"
printf '%s\n' "${BENCHES[@]}" \
  | xargs -P "$CONC" -I {} bash -c 'audit_one "$@"' _ {} "$N" \
  | tee -a "$OUT"

echo ""
echo "=== flakiness sweep done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "INVALID (flaky, >=1 spurious pass):"
awk -F'\t' 'NR>1 && $3+0>=1 {print "  "$1"  ("$5")"}' "$OUT" | sort
echo "Profile written to $OUT; per-bench logs in $LOGDIR/"
