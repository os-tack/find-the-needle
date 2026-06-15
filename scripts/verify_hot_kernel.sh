#!/usr/bin/env bash
# verify_hot_kernel.sh — assert the kernel is "fully hot" inside a fresh
# bench container. Exits non-zero if any required feature is missing.
#
# Run inside the container after install_and_boot_kernel completes, before
# kicking off a bench cell. Failures here mean the matrix sweep would
# produce degraded data — fix the container setup first.
#
# Usage:
#   docker run --rm needle-bench-<scenario> bash /path/to/verify_hot_kernel.sh
#   (or via `docker exec` against a running bench container)

set -uo pipefail

PASS=0
FAIL=0
WARN=0

ok()    { echo "  [ok]   $*"; PASS=$((PASS+1)); }
fail()  { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
warn()  { echo "  [warn] $*"; WARN=$((WARN+1)); }

cd /app 2>/dev/null || cd "${WORKDIR:-/app}" 2>/dev/null || true

echo "=== verify_hot_kernel ==="

# 1. ostk binary installed
if command -v ostk >/dev/null 2>&1; then
  ok "ostk on PATH ($(ostk --version 2>/dev/null | head -1))"
else
  fail "ostk not on PATH"
fi

# 2. .ostk/ initialized
if [ -d .ostk ]; then
  ok ".ostk/ exists"
else
  fail ".ostk/ missing — ostk init never ran"
fi

# 3. boot succeeds with confidence 1.0
boot_out=$(ostk boot 2>&1)
if echo "$boot_out" | grep -q "boot confidence: 1.00"; then
  ok "boot confidence: 1.00"
else
  fail "boot confidence not 1.00"
  echo "$boot_out" | tail -5 | sed 's/^/        /'
fi

# 4. POST checks
if echo "$boot_out" | grep -qE "POST [0-9]+/[0-9]+"; then
  ok "POST checks reported"
else
  warn "POST report missing"
fi

# 5. Daemon alive
if pgrep -f "ostk daemon" >/dev/null 2>&1; then
  ok "daemon: alive"
else
  fail "daemon: not running"
fi

# 6. Embeddings active
if echo "$boot_out" | grep -q "embeddings: active"; then
  ok "embeddings: active"
elif echo "$boot_out" | grep -q "embeddings: not available"; then
  warn "embeddings: not available — run \`ostk embeddings download\`"
else
  warn "embeddings status unknown"
fi

# 7. FUSE/VFS — soft-fail; many env's lack /dev/fuse but kernel still works
if echo "$boot_out" | grep -qE "FUSE available"; then
  ok "FUSE available"
elif mount | grep -q "/ostk/vfs" 2>/dev/null; then
  ok "/ostk/vfs mounted"
else
  warn "FUSE not detected (degraded VFS — non-fatal)"
fi

# 8. journal alive
if [ -f .ostk/journal.jsonl ]; then
  rows=$(wc -l < .ostk/journal.jsonl)
  if [ "$rows" -ge 3 ]; then
    ok "journal.jsonl: $rows rows"
  else
    fail "journal.jsonl: only $rows rows (expected >=3)"
  fi
else
  fail "journal.jsonl missing"
fi

# 9. HUMANFILE attestation (advisory)
if [ -f .ostk/.HUMANFILE.oae.json ] || [ -f .ostk/HUMANFILE.oae.json ]; then
  ok "HUMANFILE attestation present"
else
  warn "HUMANFILE not attested (T2 ceiling — soft-fail)"
fi

echo ""
echo "summary: $PASS pass, $WARN warn, $FAIL fail"
[ "$FAIL" -eq 0 ]
