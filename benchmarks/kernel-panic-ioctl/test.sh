#!/bin/sh
# Test for ioctl handler input validation.
# The handler must safely reject all crafted/invalid inputs
# without crashing (segfault = simulated kernel panic).

FAIL=0

echo "=== IOCTL Handler Validation Test ==="

# Rebuild from source (needed after patching)
echo "--- Building ---"
make -C /app/src clean && make -C /app/src 2>&1
cp /app/src/devctl /usr/local/bin/devctl

# Test 1: Normal operation should work
echo "--- Normal operation ---"
if devctl test; then
    echo "OK: self-test passed"
else
    echo "FAIL: self-test failed"
    FAIL=1
fi

# Test 2: Fuzz test with crafted inputs
echo "--- Fuzz test with crafted inputs ---"
devctl fuzz
FUZZ_EXIT=$?
if [ $FUZZ_EXIT -ne 0 ]; then
    if [ $FUZZ_EXIT -gt 128 ]; then
        SIG=$((FUZZ_EXIT - 128))
        echo "FAIL: program crashed with signal $SIG (segfault = simulated kernel panic)"
    else
        echo "FAIL: fuzz test failed (exit code $FUZZ_EXIT)"
    fi
    FAIL=1
else
    echo "OK: fuzz test passed"
fi

if [ $FAIL -eq 0 ]; then
    echo "PASS: IOCTL handler correctly validates all inputs"
    exit 0
else
    echo "FAIL: IOCTL handler crashes or mishandles crafted inputs"
    exit 1
fi
