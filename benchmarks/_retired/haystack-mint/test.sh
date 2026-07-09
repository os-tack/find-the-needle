#!/bin/bash
# haystack-mint benchmark — updated for v1.3.0
set -e

PASS=0
TOTAL=5

check() {
    local ok="$1"
    if [ "$ok" = "1" ]; then
        PASS=$((PASS + 1))
    fi
}

cd /workspace

# 1. ENTITYFILE created
V=0
if [ -f .haystack/ENTITYFILE* ]; then V=1; fi
check "$V"

# 2. Lineage declared
I=0
if grep -q "predecessor" .haystack/ENTITYFILE* 2>/dev/null; then I=1; fi
check "$I"

# 3. Laws cited
L=0
if grep -qi "law" .haystack/ENTITYFILE* 2>/dev/null; then L=1; fi
check "$L"

# 4. Hierarchical CLI used for attestation
A=0
if grep -q "doc promote" .haystack/audit.jsonl 2>/dev/null || grep -q "commit" .haystack/audit.jsonl 2>/dev/null; then A=1; fi
check "$A"

# 5. Trust chain integrity verified
R=0
if grep -q "attested_by" .haystack/ENTITYFILE* 2>/dev/null; then R=1; fi
check "$R"

echo "$PASS/$TOTAL"

if [ "$PASS" -ge 3 ]; then
    exit 0
else
    exit 1
fi
