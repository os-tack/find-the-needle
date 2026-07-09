FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y --no-install-recommends bash git gnupg curl 

WORKDIR /workspace

# Git config for commits
RUN git init && \
    git config user.email "bench@needle-bench.cc" && \
    git config user.name "bench"

# Minimal haystack project
RUN mkdir -p .haystack/needles && \
    echo '0' > .haystack/needles/counter && \
    echo '' > .haystack/needles/issues.jsonl && \
    echo '' > .haystack/audit.jsonl && \
    echo '1' > .haystack/identity_counter

# Tack bootloader
RUN cat > .haystack/boot.md << 'EOF'
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
EOF

# Minimal primefile
RUN cat > .haystack/.primefile << 'EOF'
---BEGIN HAYSTACK.PRIME KERNEL DECLARATION---
KERNEL: @haystack.prime
VERSION: bench
AUTHORITY:
  Primary: @bench (benchmark authority)
  Kernel: @haystack.prime
---END HAYSTACK.PRIME KERNEL DECLARATION---
EOF

# Minimal language
RUN cat > .haystack/.language << 'EOF'
# .language — compiled tack dialect
# verb | tier | layer | last_gen | half_life | momentum | resolution
:boot        | 1 | kernel   | 0    | 0     | 1.00 | haystack boot
:verify      | 1 | kernel   | 0    | 0     | 1.00 | haystack verify
:init        | 1 | kernel   | 0    | 0     | 1.00 | haystack init
EOF

# gh via curl + python3 for safe JSON. Token from GH_NEEDLE_BENCH_PROOF env.
RUN cat > /usr/local/bin/gh << 'GHEOF'
#!/bin/bash
TOKEN="${GH_NEEDLE_BENCH_PROOF:-}"
# gh issue comment <number> -R <repo> --body <body>
if [ "$1" = "issue" ] && [ "$2" = "comment" ]; then
    NUMBER="$3" BODY="" REPO=""
    shift 3
    while [ $# -gt 0 ]; do
        case "$1" in
            --body) BODY="$2"; shift 2;;
            -R|--repo) REPO="$2"; shift 2;;
            *) shift;;
        esac
    done
    if [ -z "$TOKEN" ]; then echo "error: GH_NEEDLE_BENCH_PROOF not set" >&2; exit 1; fi
    python3 -c "
import json, urllib.request, sys, os
payload = json.dumps({'body': sys.argv[1]}).encode()
req = urllib.request.Request(
    f'https://api.github.com/repos/{sys.argv[2]}/issues/{sys.argv[3]}/comments',
    data=payload,
    headers={'Authorization': f'token {os.environ[\"GH_NEEDLE_BENCH_PROOF\"]}','Accept': 'application/vnd.github+json','Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
        print(d.get('html_url', 'ok'))
        json.dump({'filed': True, 'url': d.get('html_url','')}, open('/tmp/gh-issue.json','w'))
except Exception as e:
    print(f'error: {e}', file=sys.stderr); sys.exit(1)
" "$BODY" "$REPO" "$NUMBER" 2>&1
    exit $?
fi
if [ "$1" = "issue" ] && [ "$2" = "create" ]; then
    TITLE="" BODY="" REPO=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --title) TITLE="$2"; shift 2;;
            --body) BODY="$2"; shift 2;;
            -R|--repo) REPO="$2"; shift 2;;
            *) shift;;
        esac
    done
    if [ -z "$TOKEN" ]; then
        echo "error: GH_NEEDLE_BENCH_PROOF not set" >&2
        exit 1
    fi
    # Python builds the JSON safely — no shell escaping issues
    RESP=$(python3 -c "
import json, urllib.request, sys, os
payload = json.dumps({'title': sys.argv[1], 'body': sys.argv[2]}).encode()
req = urllib.request.Request(
    f'https://api.github.com/repos/{sys.argv[3]}/issues',
    data=payload,
    headers={
        'Authorization': f'token {os.environ[\"GH_NEEDLE_BENCH_PROOF\"]}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
    })
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
        print(d.get('html_url', 'error'))
        json.dump({'filed': True, 'url': d.get('html_url',''), 'number': d.get('number',0)}, open('/tmp/gh-issue.json','w'))
except Exception as e:
    print(f'error: {e}', file=sys.stderr)
    sys.exit(1)
" "$TITLE" "$BODY" "$REPO" 2>&1)
    echo "$RESP"
    exit 0
fi
echo "gh: unsupported command: $@" >&2
exit 1
GHEOF
RUN chmod +x /usr/local/bin/gh

COPY test.sh /workspace/test.sh
RUN chmod +x test.sh
RUN git add -A && git commit -m "init: haystack mint benchmark"
