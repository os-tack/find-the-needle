#!/bin/bash
set -e

# Start Redis
redis-server --daemonize yes --loglevel warning
sleep 1

# Start the app in the background
cd /app
node server.js &
APP_PID=$!
sleep 2

# Wait for the server to be ready
for i in $(seq 1 10); do
    if curl -s http://localhost:3000/health > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# Reset the counter
curl -s -X POST http://localhost:3000/counter/reset > /dev/null

# Verify reset worked
INITIAL=$(curl -s http://localhost:3000/counter | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])" 2>/dev/null || echo "error")
if [ "$INITIAL" != "0" ]; then
    echo "FAIL: Counter did not reset to 0, got $INITIAL"
    kill $APP_PID 2>/dev/null || true
    exit 1
fi

# Fire 100 concurrent increments
CONCURRENCY=100
echo "Firing $CONCURRENCY concurrent increments..."

for i in $(seq 1 $CONCURRENCY); do
    curl -s -X POST http://localhost:3000/counter/increment > /dev/null &
done

# Wait for all background curl processes to finish
wait

# Small delay to ensure all writes have landed
sleep 1

# Read the final counter value
FINAL=$(curl -s http://localhost:3000/counter | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

echo "Expected: $CONCURRENCY"
echo "Got:      $FINAL"

# Cleanup
kill $APP_PID 2>/dev/null || true

if [ "$FINAL" = "$CONCURRENCY" ]; then
    echo "PASS: Counter is correct"
    exit 0
else
    echo "FAIL: Counter should be $CONCURRENCY but is $FINAL (lost $(($CONCURRENCY - $FINAL)) increments)"
    exit 1
fi
