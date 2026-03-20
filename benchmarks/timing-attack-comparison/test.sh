#!/bin/sh
# Test for timing side-channel in password comparison.
# The comparison must be constant-time to prevent an attacker
# from learning the password hash character-by-character.

set -e

FAIL=0

echo "=== Timing Attack Resistance Test ==="

# Rebuild from source (needed after patching)
echo "--- Building ---"
cd /app/app && go build -o /usr/local/bin/authserver . 2>&1

# Test 1: Basic authentication works
echo "--- Basic auth test ---"
cd /app
if authserver verify "correct-horse-battery-staple"; then
    echo "OK: correct password accepted"
else
    echo "FAIL: correct password rejected"
    FAIL=1
fi

# Incorrect password should be rejected
if authserver verify "wrong-password" 2>/dev/null; then
    echo "FAIL: wrong password accepted"
    FAIL=1
else
    echo "OK: wrong password rejected"
fi

# Test 2: Timing analysis
echo "--- Timing side-channel analysis ---"
if ! authserver test; then
    echo "FAIL: timing analysis detected side-channel vulnerability"
    FAIL=1
fi

if [ $FAIL -eq 0 ]; then
    echo "PASS: Authentication is constant-time and secure"
    exit 0
else
    echo "FAIL: Authentication comparison leaks timing information"
    exit 1
fi
