#!/bin/bash
# ---------------------------------------------------------------------------
# Test for Unicode BIDI override privilege escalation.
#
# A user registers with a username containing a RIGHT-TO-LEFT OVERRIDE
# character (U+202E) so that "\u202enimda" renders visually as "admin".
# The auth middleware compares the display-rendered username against the
# admin allowlist, granting admin access to the attacker.
#
# Test 3 FAILS on the buggy code (attacker gets admin) and PASSES once
# the registration handler strips BIDI control characters and the auth
# middleware compares against the stored username, not the rendered form.
# ---------------------------------------------------------------------------
set -e

cd /workspace
FAIL=0

# Start the Flask app
python3 app/server.py &
APP_PID=$!
sleep 2

cleanup() {
    kill $APP_PID 2>/dev/null || true
    wait $APP_PID 2>/dev/null || true
}
trap cleanup EXIT

# ---- Test 1: Normal registration and login work --------------------------
RESP=$(curl -s -X POST http://localhost:5000/register \
    -H "Content-Type: application/json" \
    -d '{"username": "bob", "password": "bobpass123"}')
if echo "$RESP" | grep -q "registration successful"; then
    echo "PASS: normal user registration works"
else
    echo "FAIL: normal registration failed: $RESP"
    FAIL=1
fi

TOKEN=$(curl -s -X POST http://localhost:5000/login \
    -H "Content-Type: application/json" \
    -d '{"username": "bob", "password": "bobpass123"}' | jq -r '.token')
if [ "$TOKEN" != "null" ] && [ -n "$TOKEN" ]; then
    echo "PASS: normal user login works"
else
    echo "FAIL: normal login failed"
    FAIL=1
fi

# ---- Test 2: Real admin can access admin dashboard -----------------------
ADMIN_TOKEN=$(curl -s -X POST http://localhost:5000/login \
    -H "Content-Type: application/json" \
    -d '{"username": "admin", "password": "admin_secret_2024"}' | jq -r '.token')

ADMIN_RESP=$(curl -s http://localhost:5000/admin/dashboard \
    -H "Authorization: Bearer $ADMIN_TOKEN")
if echo "$ADMIN_RESP" | jq -e '.admin == true' > /dev/null 2>&1; then
    echo "PASS: real admin can access dashboard"
else
    echo "FAIL: real admin cannot access dashboard: $ADMIN_RESP"
    FAIL=1
fi

# ---- Test 3: BIDI-injected username must NOT get admin access ------------
# Register with U+202E RIGHT-TO-LEFT OVERRIDE + "nimda"
# Visual rendering: "admin" (reversed), but stored bytes are \xe2\x80\xaenimda
BIDI_USER=$(printf '\xe2\x80\xaenimda')

REG_RESP=$(curl -s -X POST http://localhost:5000/register \
    -H "Content-Type: application/json" \
    --data-binary "{\"username\": \"${BIDI_USER}\", \"password\": \"attackpass\"}")

# The registration might succeed (buggy) or reject the username (fixed).
# Either way, if we can log in, check admin access.
LOGIN_RESP=$(curl -s -X POST http://localhost:5000/login \
    -H "Content-Type: application/json" \
    --data-binary "{\"username\": \"${BIDI_USER}\", \"password\": \"attackpass\"}")
ATTACK_TOKEN=$(echo "$LOGIN_RESP" | jq -r '.token')

if [ "$ATTACK_TOKEN" != "null" ] && [ -n "$ATTACK_TOKEN" ]; then
    # User was registered and logged in — now check if they got admin
    ATTACK_RESP=$(curl -s http://localhost:5000/admin/dashboard \
        -H "Authorization: Bearer $ATTACK_TOKEN")
    if echo "$ATTACK_RESP" | jq -e '.admin == true' > /dev/null 2>&1; then
        echo "FAIL: BIDI-injected user got admin access (privilege escalation)"
        FAIL=1
    else
        echo "PASS: BIDI-injected user correctly denied admin access"
    fi
else
    # Registration was rejected or login failed — this is the fixed behavior
    # (stripping BIDI chars means the username becomes "nimda", which is fine,
    #  but it won't match "admin" in the allowlist)
    echo "PASS: BIDI-injected username was rejected or normalized"
fi

# ---- Test 4: Normal user cannot access admin dashboard -------------------
NON_ADMIN_RESP=$(curl -s http://localhost:5000/admin/dashboard \
    -H "Authorization: Bearer $TOKEN")
if echo "$NON_ADMIN_RESP" | jq -e '.admin == true' > /dev/null 2>&1; then
    echo "FAIL: non-admin user got admin access"
    FAIL=1
else
    echo "PASS: non-admin user correctly denied admin access"
fi

# ---- Test 5: Other BIDI variants also blocked ----------------------------
# U+202D LEFT-TO-RIGHT OVERRIDE variant
BIDI2_USER=$(printf '\xe2\x80\xadnimda')

curl -s -X POST http://localhost:5000/register \
    -H "Content-Type: application/json" \
    --data-binary "{\"username\": \"${BIDI2_USER}\", \"password\": \"attackpass2\"}" > /dev/null 2>&1

LOGIN2_RESP=$(curl -s -X POST http://localhost:5000/login \
    -H "Content-Type: application/json" \
    --data-binary "{\"username\": \"${BIDI2_USER}\", \"password\": \"attackpass2\"}")
ATTACK2_TOKEN=$(echo "$LOGIN2_RESP" | jq -r '.token')

if [ "$ATTACK2_TOKEN" != "null" ] && [ -n "$ATTACK2_TOKEN" ]; then
    ATTACK2_RESP=$(curl -s http://localhost:5000/admin/dashboard \
        -H "Authorization: Bearer $ATTACK2_TOKEN")
    if echo "$ATTACK2_RESP" | jq -e '.admin == true' > /dev/null 2>&1; then
        echo "FAIL: LRO-injected user got admin access"
        FAIL=1
    else
        echo "PASS: LRO-injected user correctly denied admin access"
    fi
else
    echo "PASS: LRO-injected username was rejected or normalized"
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
