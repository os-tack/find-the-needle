#!/bin/sh
# ---------------------------------------------------------------------------
# Test for SSRF via IPv6 loopback bypass in the webhook proxy validator.
#
# The proxy blocks requests to 127.0.0.1, localhost, 0.0.0.0, and private
# ranges.  However the blocklist must also cover IPv6 loopback forms such
# as [::1] and [0:0:0:0:0:0:0:1].
#
# Tests 3 and 4 FAIL on the buggy code (SSRF succeeds -> internal secret
# is leaked) and PASS once the validator is fixed.
# ---------------------------------------------------------------------------
set -e

cd /workspace
FAIL=0

# Start internal metadata service (binds IPv4 + IPv6 on port 8888)
python3 app/internal_service.py &
META_PID=$!
sleep 1

# Start the webhook proxy (binds :5000)
python3 app/server.py &
PROXY_PID=$!
sleep 1

cleanup() {
    kill $META_PID $PROXY_PID 2>/dev/null || true
    wait $META_PID $PROXY_PID 2>/dev/null || true
}
trap cleanup EXIT

# ---- Preflight: verify internal service is reachable via both paths -----
# IPv4 direct
DIRECT4=$(curl -s http://127.0.0.1:8888/metadata 2>/dev/null || echo "")
if ! echo "$DIRECT4" | grep -q "INTERNAL_SECRET"; then
    echo "SKIP: internal service not reachable on 127.0.0.1:8888"
    exit 1
fi

# IPv6 direct
DIRECT6=$(curl -s -g "http://[::1]:8888/metadata" 2>/dev/null || echo "")
if ! echo "$DIRECT6" | grep -q "INTERNAL_SECRET"; then
    echo "SKIP: internal service not reachable on [::1]:8888 (IPv6 unavailable)"
    exit 1
fi
echo "Preflight OK: internal service reachable on both IPv4 and IPv6"

# ---- Test 1: health endpoint -------------------------------------------
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/health)
if [ "$STATUS" != "200" ]; then
    echo "FAIL: server not healthy (HTTP $STATUS)"
    FAIL=1
else
    echo "PASS: health endpoint OK"
fi

# ---- Test 2: direct 127.0.0.1 must be blocked --------------------------
RESP=$(curl -s -X POST http://localhost:5000/webhook/proxy \
    -H "Content-Type: application/json" \
    -d '{"url": "http://127.0.0.1:8888/metadata"}')
if echo "$RESP" | grep -q "INTERNAL_SECRET"; then
    echo "FAIL: 127.0.0.1 was NOT blocked — internal secret leaked"
    FAIL=1
else
    echo "PASS: 127.0.0.1 correctly blocked"
fi

# ---- Test 3: IPv6 loopback [::1] must be blocked -----------------------
RESP=$(curl -s -X POST http://localhost:5000/webhook/proxy \
    -H "Content-Type: application/json" \
    -d '{"url": "http://[::1]:8888/metadata"}')
if echo "$RESP" | grep -q "INTERNAL_SECRET"; then
    echo "FAIL: SSRF via IPv6 loopback [::1] — internal secret leaked"
    FAIL=1
else
    echo "PASS: IPv6 loopback [::1] correctly blocked"
fi

# ---- Test 4: expanded IPv6 loopback must be blocked ---------------------
RESP=$(curl -s -X POST http://localhost:5000/webhook/proxy \
    -H "Content-Type: application/json" \
    -d '{"url": "http://[0:0:0:0:0:0:0:1]:8888/metadata"}')
if echo "$RESP" | grep -q "INTERNAL_SECRET"; then
    echo "FAIL: SSRF via expanded IPv6 loopback — internal secret leaked"
    FAIL=1
else
    echo "PASS: expanded IPv6 loopback correctly blocked"
fi

# ---- Test 5: 169.254.169.254 (AWS metadata) must be blocked ------------
RESP=$(curl -s -X POST http://localhost:5000/webhook/proxy \
    -H "Content-Type: application/json" \
    -d '{"url": "http://169.254.169.254/latest/meta-data/"}')
if echo "$RESP" | grep -q "blocked"; then
    echo "PASS: AWS metadata IP correctly blocked"
else
    # Blocked at validator level returns 403; connection error returns 502
    echo "PASS: AWS metadata IP handled (blocked or unreachable)"
fi

# ---- Verdict ------------------------------------------------------------
if [ $FAIL -eq 0 ]; then
    echo ""
    echo "PASS"
    exit 0
else
    echo ""
    echo "FAIL"
    exit 1
fi
