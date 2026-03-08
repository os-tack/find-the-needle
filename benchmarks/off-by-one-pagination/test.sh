#!/bin/sh
# Test for off-by-one pagination bug
# Verifies that paginated results don't skip or duplicate items across page boundaries.

set -e

# Start the Flask app in the background
python app.py &
APP_PID=$!
sleep 2

cleanup() {
    kill $APP_PID 2>/dev/null || true
}
trap cleanup EXIT

FAIL=0

# Fetch page 1 and page 2
page1=$(curl -s http://localhost:5000/products?page=1\&per_page=10)
page2=$(curl -s http://localhost:5000/products?page=2\&per_page=10)

# Extract the last item ID from page 1 and first item ID from page 2
last_id_page1=$(echo "$page1" | python -c "import sys,json; d=json.load(sys.stdin); print(d['items'][-1]['id'])")
first_id_page2=$(echo "$page2" | python -c "import sys,json; d=json.load(sys.stdin); print(d['items'][0]['id'])")

# Page 1 should end at ID 10, page 2 should start at ID 11
# (no gap, no overlap)
expected_next=$((last_id_page1 + 1))

if [ "$first_id_page2" != "$expected_next" ]; then
    echo "FAIL: Page boundary error. Page 1 ends at ID $last_id_page1, page 2 starts at ID $first_id_page2 (expected $expected_next)"
    FAIL=1
fi

# Verify page 1 has exactly per_page items
count_page1=$(echo "$page1" | python -c "import sys,json; print(len(json.load(sys.stdin)['items']))")
if [ "$count_page1" != "10" ]; then
    echo "FAIL: Page 1 should have 10 items, got $count_page1"
    FAIL=1
fi

# Verify page 2 has exactly per_page items
count_page2=$(echo "$page2" | python -c "import sys,json; print(len(json.load(sys.stdin)['items']))")
if [ "$count_page2" != "10" ]; then
    echo "FAIL: Page 2 should have 10 items, got $count_page2"
    FAIL=1
fi

if [ $FAIL -eq 0 ]; then
    echo "PASS: Pagination is correct across page boundaries"
    exit 0
else
    exit 1
fi
