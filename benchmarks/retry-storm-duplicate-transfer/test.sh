#!/bin/sh
set -e
cd /workspace

# Clean any prior state
rm -f /tmp/bank.db

# Seed accounts: A=1000, B=1000
python3 app/models.py

# Start all services via supervisord
supervisord -c app/supervisord.conf &
sleep 2

# Configure chaos: delay responses by 2 seconds (gateway timeout is 1s)
curl -s -X POST http://localhost:8082/config \
    -H "Content-Type: application/json" \
    -d '{"drop_response_ms": 2000}'

# Send transfer A->B for 500 (gateway will retry on timeout)
curl -s http://localhost:8080/transfer \
    -H "Content-Type: application/json" \
    -d '{"from":"A","to":"B","amount":500,"idempotency_key":"txn-001"}' || true

# Wait for all retries to complete through the proxy
sleep 3

# Check balances
BALANCE_A=$(curl -s http://localhost:8080/balance/A | python3 -c "import sys,json; print(json.load(sys.stdin)['balance'])")
BALANCE_B=$(curl -s http://localhost:8080/balance/B | python3 -c "import sys,json; print(json.load(sys.stdin)['balance'])")

# Shut down services
kill $(cat /tmp/supervisord.pid) 2>/dev/null || true

echo "A=$BALANCE_A B=$BALANCE_B"

if [ "$BALANCE_A" != "500" ] || [ "$BALANCE_B" != "1500" ]; then
    echo "FAIL: duplicate transfer (A=$BALANCE_A, B=$BALANCE_B)"
    exit 1
fi
echo "PASS"
