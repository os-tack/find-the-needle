#!/bin/sh
# Test for code generation correctness.
# Simple accessor tests pass, but complex nested accessors reveal
# incorrect return types in generated code.

set -e

FAIL=0

echo "=== Code Generation Engine Tests ==="

# Rebuild from source (needed after patching)
echo "--- Building ---"
cd /app && cargo build --release 2>&1
cp /app/target/release/codegen-engine /usr/local/bin/codegen-engine

# Test 1: Simple accessors (single-level field access)
echo "--- Simple accessor test ---"
if ! codegen-engine test-simple; then
    echo "FAIL: simple accessor test"
    FAIL=1
fi

# Test 2: Complex nested accessors (chained reference access)
echo "--- Complex nested accessor test ---"
if ! codegen-engine test-complex; then
    echo "FAIL: complex nested accessor test"
    FAIL=1
fi

# Test 3: Verify generated code compiles conceptually
echo "--- Generated code inspection ---"
codegen-engine generate complex > /tmp/generated.rs
echo "Generated code written to /tmp/generated.rs"

# Check that Ref-typed fields have reference return types.
# Use fixed-string grep (-F) to match "-> Company {" literally.
# The bug produces "-> Company {" (owned); the fix produces "-> &Company {" (reference).
if grep -Fq "-> Company {" /tmp/generated.rs && ! grep -Fq "-> &Company {" /tmp/generated.rs; then
    echo "FAIL: company getter returns owned type instead of reference"
    FAIL=1
fi
if grep -Fq "-> Address {" /tmp/generated.rs && ! grep -Fq "-> &Address {" /tmp/generated.rs; then
    echo "FAIL: address getter returns owned type instead of reference"
    FAIL=1
fi

rm -f /tmp/generated.rs

if [ $FAIL -eq 0 ]; then
    echo "PASS: All code generation tests passed"
    exit 0
else
    echo "FAIL: Code generation produces incorrect accessor signatures"
    exit 1
fi
