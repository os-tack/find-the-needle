#!/bin/sh
# needle-bench test script
# Exit 0 = bug is fixed (pass)
# Exit 1 = bug still present (fail)
set -e

echo "=== postgres-migration-schema-drift test ==="

PGDATA=/var/lib/postgresql/data

# Initialize PostgreSQL if needed
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    su postgres -c "initdb -D $PGDATA"
fi

# Start PostgreSQL
su postgres -c "pg_ctl -D $PGDATA -l /var/log/postgresql.log start" || true

# Wait for PostgreSQL to be ready
for i in $(seq 1 30); do
    if su postgres -c "pg_isready" >/dev/null 2>&1; then
        echo "PostgreSQL is ready"
        break
    fi
    sleep 1
done

# Create database if it doesn't exist
su postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='ordersdb'\"" | grep -q 1 || \
    su postgres -c "createdb ordersdb"

# Drop and recreate tables to ensure clean state
su postgres -c "psql -d ordersdb -c 'DROP TABLE IF EXISTS orders CASCADE;'"

# Apply migrations in order
for migration in /workspace/migrations/*.sql; do
    echo "Applying migration: $migration"
    su postgres -c "psql -d ordersdb -f $migration"
done

# Start the API server in background
/workspace/orders-api &
API_PID=$!

cleanup() {
    kill "$API_PID" 2>/dev/null || true
    su postgres -c "pg_ctl -D $PGDATA stop" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for API to be ready
for i in $(seq 1 20); do
    if curl -s http://127.0.0.1:8080/health >/dev/null 2>&1; then
        echo "API server is ready"
        break
    fi
    sleep 0.5
done

# --- Test 1: Health check ---
echo ""
echo "Test 1: GET /health"
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/health)
if [ "$HEALTH_STATUS" != "200" ]; then
    echo "FAIL: /health returned $HEALTH_STATUS (expected 200)"
    exit 1
fi
echo "PASS: /health returned 200"

# --- Test 2: Create an order ---
echo ""
echo "Test 2: POST /orders"
CREATE_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST http://127.0.0.1:8080/orders \
    -H "Content-Type: application/json" \
    -d '{"user_id": 42, "total": 99.95, "shipping_address": "123 Main St"}')

CREATE_BODY=$(echo "$CREATE_RESPONSE" | head -n -1)
CREATE_STATUS=$(echo "$CREATE_RESPONSE" | tail -n 1)

echo "Status: $CREATE_STATUS"
echo "Body: $CREATE_BODY"

if [ "$CREATE_STATUS" != "200" ]; then
    echo "FAIL: POST /orders returned $CREATE_STATUS (expected 200)"
    exit 1
fi

# Extract order ID from response
ORDER_ID=$(echo "$CREATE_BODY" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")
echo "Created order ID: $ORDER_ID"
echo "PASS: POST /orders returned 200"

# --- Test 3: Retrieve the order ---
echo ""
echo "Test 3: GET /orders/$ORDER_ID"
GET_RESPONSE=$(curl -s -w "\n%{http_code}" http://127.0.0.1:8080/orders/$ORDER_ID)

GET_BODY=$(echo "$GET_RESPONSE" | head -n -1)
GET_STATUS=$(echo "$GET_RESPONSE" | tail -n 1)

echo "Status: $GET_STATUS"
echo "Body: $GET_BODY"

if [ "$GET_STATUS" != "200" ]; then
    echo "FAIL: GET /orders/$ORDER_ID returned $GET_STATUS (expected 200)"
    exit 1
fi

# Verify the response has expected fields
echo "$GET_BODY" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['id'] == $ORDER_ID, f'id mismatch: {data[\"id\"]}'
assert data['user_id'] == 42, f'user_id mismatch: {data[\"user_id\"]}'
assert data['order_status'] == 'pending', f'order_status mismatch: {data.get(\"order_status\", \"MISSING\")}'
print('PASS: GET /orders/$ORDER_ID returned valid order with order_status field')
"

echo ""
echo "ALL TESTS PASSED"
exit 0
