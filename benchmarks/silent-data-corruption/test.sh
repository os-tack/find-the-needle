#!/bin/sh
# Test for silent data corruption in fileproc
# Verifies that processing a file larger than the internal buffer
# produces output with the same content and length as the input.

set -e

FAIL=0

# Generate a test file larger than 64KB (the internal buffer size)
python3 -c "
import os, sys
# 100KB of repeating pattern — deterministic
data = (b'ABCDEFGHIJ' * 10240)[:102400]
sys.stdout.buffer.write(data)
" > /tmp/input.dat

INPUT_SIZE=$(wc -c < /tmp/input.dat | tr -d ' ')
echo "Input file size: $INPUT_SIZE bytes"

# Process the file
fileproc /tmp/input.dat /tmp/output.dat

OUTPUT_SIZE=$(wc -c < /tmp/output.dat | tr -d ' ')
echo "Output file size: $OUTPUT_SIZE bytes"

# The output must be the same size as the input
if [ "$INPUT_SIZE" != "$OUTPUT_SIZE" ]; then
    echo "FAIL: Output size ($OUTPUT_SIZE) != input size ($INPUT_SIZE)"
    echo "      Data was silently truncated or corrupted"
    FAIL=1
fi

# The output content must match the input (identity transform)
if [ $FAIL -eq 0 ]; then
    INPUT_HASH=$(sha256sum /tmp/input.dat | cut -d' ' -f1)
    OUTPUT_HASH=$(sha256sum /tmp/output.dat | cut -d' ' -f1)
    if [ "$INPUT_HASH" != "$OUTPUT_HASH" ]; then
        echo "FAIL: Output content differs from input"
        echo "      Input SHA256:  $INPUT_HASH"
        echo "      Output SHA256: $OUTPUT_HASH"
        FAIL=1
    fi
fi

# Cleanup
rm -f /tmp/input.dat /tmp/output.dat

if [ $FAIL -eq 0 ]; then
    echo "PASS: File processed correctly, no data loss"
    exit 0
else
    exit 1
fi
