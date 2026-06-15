#!/usr/bin/env bash
# Archive v4.0.0 kernel + kernel-cpu score directories before v4.1.0 re-run.
# Native arm directories are PRESERVED (they don't change between ostk versions).
#
# Run this ONCE before invoking run_matrix_v41.py for the v4.1.0 sweep.
set -euo pipefail
cd "$(dirname "$0")"

ARCHIVE_DIR="runs-v4.0-archive-$(date +%Y%m%d)"

if [ -d "$ARCHIVE_DIR" ]; then
  echo "ERROR: $ARCHIVE_DIR already exists. Aborting."
  exit 1
fi

mkdir -p "$ARCHIVE_DIR"

count=0
for d in runs/*-kernel runs/*-kernel-cpu; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  echo "  archive $d -> $ARCHIVE_DIR/$name"
  mv "$d" "$ARCHIVE_DIR/$name"
  count=$((count + 1))
done

# Also stash the state file if present
if [ -f runs/.state.jsonl ]; then
  mv runs/.state.jsonl "$ARCHIVE_DIR/.state.jsonl"
  echo "  archived runs/.state.jsonl"
fi

echo ""
echo "archived $count kernel/kernel-cpu directories to $ARCHIVE_DIR/"
echo "native directories preserved in runs/"
echo ""
echo "Native arm score files preserved:"
ls runs/ | grep -- '-native$' | wc -l | xargs echo "  count:"
