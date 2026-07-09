#!/bin/bash
# haystack-boot benchmark — updated for v1.3.0
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

# 1. Boot sequence executed (audit check)
V=0
if grep -q "boot" .haystack/audit.jsonl 2>/dev/null; then V=1; fi
check "$V"

# 2. Identity assigned
I=0
if ls .haystack/ENTITYFILE* 2>/dev/null | grep -q . 2>/dev/null; then I=1; fi
check "$I"

# 3. Tack resolution verified (Tier 1/2)
L=0
if grep -q "tack.resolved" .haystack/audit.jsonl 2>/dev/null; then L=1; fi
check "$L"

# 4. Hierarchical CLI used
A=0
if grep -q "os status" .haystack/audit.jsonl 2>/dev/null || grep -q "kernel ps" .haystack/audit.jsonl 2>/dev/null; then A=1; fi
check "$A"

# 5. Boot confidence reported
R=0
if grep -q "boot confidence" .haystack/audit.jsonl 2>/dev/null || [ -f .haystack/registers-dump.md ]; then R=1; fi
check "$R"

echo "$PASS/$TOTAL"

if [ "$PASS" -ge 3 ]; then
    exit 0
else
    exit 1
fi
