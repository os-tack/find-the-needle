#!/bin/sh
set -e

# Start the Flask app in the background
cd /app
python app.py &
APP_PID=$!

# Wait for the server to be ready
for i in $(seq 1 10); do
    if curl -s http://localhost:5000/health > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

PASS=true

# Fetch page 1 and page 2
PAGE1=$(curl -s http://localhost:5000/products?page=1&per_page=10)
PAGE2=$(curl -s http://localhost:5000/products?page=2&per_page=10)

# Extract item IDs from each page
PAGE1_IDS=$(echo "$PAGE1" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(' '.join(str(i['id']) for i in items))")
PAGE2_IDS=$(echo "$PAGE2" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(' '.join(str(i['id']) for i in items))")

echo "Page 1 IDs: $PAGE1_IDS"
echo "Page 2 IDs: $PAGE2_IDS"

# Check for overlap: no ID from page 1 should appear in page 2
for id in $PAGE1_IDS; do
    for id2 in $PAGE2_IDS; do
        if [ "$id" = "$id2" ]; then
            echo "FAIL: Product ID $id appears on both page 1 and page 2"
            PASS=false
        fi
    done
done

# Verify page 1 has exactly 10 items
PAGE1_COUNT=$(echo "$PAGE1" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))")
if [ "$PAGE1_COUNT" != "10" ]; then
    echo "FAIL: Page 1 should have 10 items, got $PAGE1_COUNT"
    PASS=false
fi

# Verify page 2 has exactly 10 items
PAGE2_COUNT=$(echo "$PAGE2" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))")
if [ "$PAGE2_COUNT" != "10" ]; then
    echo "FAIL: Page 2 should have 10 items, got $PAGE2_COUNT"
    PASS=false
fi

# Verify page 1 starts at ID 1 and page 2 starts at ID 11
PAGE1_FIRST=$(echo "$PAGE1" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['id'])")
PAGE2_FIRST=$(echo "$PAGE2" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['id'])")

if [ "$PAGE1_FIRST" != "1" ]; then
    echo "FAIL: Page 1 should start at ID 1, got $PAGE1_FIRST"
    PASS=false
fi

if [ "$PAGE2_FIRST" != "11" ]; then
    echo "FAIL: Page 2 should start at ID 11, got $PAGE2_FIRST"
    PASS=false
fi

# Cleanup
kill $APP_PID 2>/dev/null || true

if [ "$PASS" = "true" ]; then
    echo "PASS: Pagination is correct"
    exit 0
else
    echo "FAIL: Pagination has errors"
    exit 1
fi
