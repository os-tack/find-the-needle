#!/bin/sh
# needle-bench test script
# Exit 0 = bug is fixed (pass)
# Exit 1 = bug still present (fail)
set -e

echo "=== nginx-upstream-port-mismatch test ==="

# Start services in the background
python3 /workspace/app/server.py &
FLASK_PID=$!

nginx -g 'daemon off;' &
NGINX_PID=$!

cleanup() {
    kill "$FLASK_PID" "$NGINX_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for Flask
for i in $(seq 1 20); do
    if curl -s http://127.0.0.1:5000/health >/dev/null 2>&1; then
        echo "Flask is ready"
        break
    fi
    sleep 0.5
done

# Wait for nginx
for i in $(seq 1 10); do
    if curl -s http://127.0.0.1:80/health >/dev/null 2>&1; then
        echo "nginx is ready"
        break
    fi
    sleep 0.5
done

# --- Test 1: health check through nginx (should always pass) ---
echo ""
echo "Test 1: GET /health through nginx"
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:80/health)
if [ "$HEALTH_STATUS" != "200" ]; then
    echo "FAIL: /health returned $HEALTH_STATUS (expected 200)"
    exit 1
fi
echo "PASS: /health returned 200"

# --- Test 2: API data through nginx (fails when port is wrong) ---
echo ""
echo "Test 2: GET /api/data through nginx"
API_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:80/api/data)
if [ "$API_STATUS" != "200" ]; then
    echo "FAIL: /api/data returned $API_STATUS (expected 200)"
    exit 1
fi

API_BODY=$(curl -s http://127.0.0.1:80/api/data)
echo "Response: $API_BODY"

# Verify the response contains expected JSON fields
echo "$API_BODY" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['status'] == 'ok', f'status={data[\"status\"]}'
assert len(data['items']) == 3, f'items count={len(data[\"items\"])}'
print('PASS: /api/data returned valid JSON with 3 items')
"

echo ""
echo "ALL TESTS PASSED"
exit 0
