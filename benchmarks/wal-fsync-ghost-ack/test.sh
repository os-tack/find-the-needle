#!/bin/sh
set -e
cd /workspace

# Rebuild from source
go build -o kvwal ./...

# Clean any stale WAL state
rm -f /tmp/kvwal.wal /tmp/kvwal.wal.sync

# Start server
./kvwal serve &
SERVER_PID=$!
sleep 0.5

# Write 50 entries — capture acked keys
ACKED=$(./kvwal client write-batch 50)

# Kill server immediately (no graceful shutdown — simulates crash)
kill -9 $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null || true

# Simulate crash: truncate WAL to last fsync point
./kvwal crash-recover

# Restart server (replays WAL)
./kvwal serve &
SERVER_PID=$!
sleep 0.5

# Verify all acknowledged keys are still present
RESULT=$(./kvwal client verify-batch "$ACKED")

kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null || true

echo "$RESULT"

if echo "$RESULT" | grep -q "LOST"; then
    echo "FAIL: acknowledged writes lost after crash"
    exit 1
fi
echo "PASS"
