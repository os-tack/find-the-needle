#!/bin/sh
# Test for data corruption in concurrent file writer.
# Writes a file using multiple concurrent threads and verifies
# each segment is owned by exactly one worker (no overlapping assignments).

set -e

FAIL=0
OUTPUT=/tmp/concurrent_test.dat

echo "=== Concurrent Write Integrity Test ==="

# Rebuild from source (needed after patching)
echo "--- Building ---"
cd /app && cargo build --release 2>&1
cp /app/target/release/concurrent-writer /usr/local/bin/concurrent-writer

# Run the concurrent write and capture stderr (worker assignment info)
rm -f "$OUTPUT"
concurrent-writer write "$OUTPUT" 2>/tmp/concurrent_write_log.txt

# Test 1: Basic data integrity (fill pattern check)
if ! concurrent-writer verify "$OUTPUT"; then
    echo "FAIL: Corruption detected in fill pattern"
    FAIL=1
fi

# Test 2: Check for overlapping segment assignments.
# Each segment should be assigned to exactly one worker. If any segment
# appears in multiple workers' assignment lists, we have a data race
# (even if the fill pattern happens to be the same, concurrent writes
# to the same file offset are undefined behavior).
echo "--- Checking segment assignment overlap ---"

# Parse worker segment assignments from the log
# Format: "Worker N completed: segments [a, b, c, ...]"
OVERLAP_FOUND=0
ALL_SEGMENTS=""
for seg_num in $(sed -n 's/.*segments \[\(.*\)\]/\1/p' /tmp/concurrent_write_log.txt | tr ',' '\n' | tr -d ' []'); do
    if echo "$ALL_SEGMENTS" | grep -qw "$seg_num"; then
        echo "FAIL: Segment $seg_num is assigned to multiple workers (overlapping write)"
        OVERLAP_FOUND=1
    fi
    ALL_SEGMENTS="$ALL_SEGMENTS $seg_num"
done

if [ "$OVERLAP_FOUND" -eq 1 ]; then
    echo "FAIL: Overlapping segment assignments detected — concurrent writes to same offsets"
    echo "Worker assignments:"
    cat /tmp/concurrent_write_log.txt
    FAIL=1
else
    echo "OK: No overlapping segment assignments"
fi

rm -f "$OUTPUT" /tmp/concurrent_write_log.txt

if [ $FAIL -eq 0 ]; then
    echo "PASS: All concurrent write runs produced correct output"
    exit 0
else
    echo "FAIL: Data corruption from concurrent writes"
    exit 1
fi
