#!/bin/bash
set -e

cd /app

# Rebuild from source (needed after patching)
echo "--- Building ---"
mkdir -p build && javac -d build app/*.java 2>&1

# Generate the report
java -cp build Main app/customers.csv /tmp/report.txt

echo "--- Report Output ---"
cat /tmp/report.txt
echo "---------------------"

# Check that non-ASCII names survived the round-trip correctly
FAILURES=0

check_name() {
    local expected="$1"
    if grep -q "$expected" /tmp/report.txt; then
        echo "OK: Found '$expected'"
    else
        echo "FAIL: Expected '$expected' but not found in report"
        FAILURES=$((FAILURES + 1))
    fi
}

check_name "Renée Duböis"
check_name "Hans Müller"
check_name "María García"
check_name "Søren Kirkegård"
check_name "François Léger"
check_name "Zoltán Kovács"
check_name "München"
check_name "København"
check_name "Budapest"

# Also verify ASCII names work (sanity check)
check_name "John Smith"
check_name "Alice Johnson"
check_name "Bob Williams"

if [ "$FAILURES" -gt 0 ]; then
    echo ""
    echo "FAIL: $FAILURES name(s) corrupted in report output"
    exit 1
fi

echo ""
echo "PASS: All customer names preserved correctly"
exit 0
