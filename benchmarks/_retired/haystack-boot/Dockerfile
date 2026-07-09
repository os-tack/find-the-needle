FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y --no-install-recommends bash git gnupg 

WORKDIR /workspace

# Initialize a git repo + haystack project
RUN git init && \
    git config user.email "bench@needle-bench.cc" && \
    git config user.name "bench"

# Create .haystack with tack bootloader and minimal state
RUN mkdir -p .haystack/needles && \
    echo '0' > .haystack/needles/counter && \
    echo '' > .haystack/needles/issues.jsonl && \
    echo '' > .haystack/audit.jsonl && \
    echo '1' > .haystack/identity_counter

# The tack bootloader — the model must execute these directives
RUN cat > .haystack/boot.md << 'BOOTEOF'
# boot.md — @haystack.prime

:verify .primefile
:post
:clock
:reap

:init @haystack.prime+N
:load .language
:load fcp-haystack

:boot
:refine
:compile
:work
BOOTEOF

# .primefile — minimal but present
RUN cat > .haystack/.primefile << 'EOF'
---BEGIN HAYSTACK.PRIME KERNEL DECLARATION---

KERNEL: @haystack.prime
VERSION: bench
CREATED: 2026-03-13T00:00:00Z

AUTHORITY:
  Primary: @bench (benchmark authority)
  Kernel: @haystack.prime

---END HAYSTACK.PRIME KERNEL DECLARATION---
EOF

# .language — minimal verb table
RUN cat > .haystack/.language << 'LANGEOF'
# .language — compiled tack dialect
# verb | tier | layer | last_gen | half_life | momentum | resolution
:boot        | 1 | kernel   | 0    | 0     | 1.00 | haystack boot
:verify      | 1 | kernel   | 0    | 0     | 1.00 | haystack verify
:post        | 1 | kernel   | 0    | 0     | 1.00 | haystack post
:clock       | 1 | kernel   | 0    | 0     | 1.00 | haystack clock
:reap        | 1 | kernel   | 0    | 0     | 1.00 | haystack reap
:init        | 1 | kernel   | 0    | 0     | 1.00 | haystack init
:shutdown    | 1 | kernel   | 0    | 0     | 1.00 | haystack shutdown
LANGEOF

# HUMANFILE — minimal
RUN cat > .haystack/HUMANFILE << 'EOF'
# HUMANFILE — bench operator
Last updated: 2026-03-13
EOF

# The test script checks if the model executed the boot sequence
COPY test.sh /workspace/test.sh
RUN chmod +x test.sh

# Commit initial state so git is clean
RUN git add -A && git commit -m "init: haystack boot benchmark"
