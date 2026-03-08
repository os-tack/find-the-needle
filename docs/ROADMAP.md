# needle-bench Roadmap — 25 Benchmarks

3 shipped. 22 planned. Every benchmark is a real bug someone hit in production.

## Shipped

| # | Name | Language | Difficulty |
|---|------|----------|------------|
| 1 | off-by-one-pagination | Python | Easy |
| 2 | race-condition-counter | JavaScript | Medium |
| 3 | silent-data-corruption | Rust | Hard |

## Planned — Easy (8)

| # | Name | Language | Description |
|---|------|----------|-------------|
| 4 | null-pointer-config | Go | Config struct field left nil, crashes on first request with optional feature enabled |
| 5 | off-by-one-array-slice | Python | Array slicing boundary drops the last element in batch processing |
| 6 | type-coercion-comparison | JavaScript | Loose equality (`==`) between string and number causes silent filter bypass |
| 7 | missing-input-validation | TypeScript | API accepts negative quantity, creates impossible inventory state |
| 8 | wrong-operator-discount | Python | Uses `+` instead of `*` for percentage discount calculation |
| 9 | encoding-mojibake | Java | UTF-8 file read with Latin-1 decoder corrupts non-ASCII customer names |
| 10 | timezone-scheduling | Python | Cron job uses UTC but compares against local time, skips events near midnight |
| 11 | import-cycle-startup | Python | Circular import between two modules causes AttributeError on cold start |

## Planned — Medium (8)

| # | Name | Language | Description |
|---|------|----------|-------------|
| 12 | goroutine-leak-handler | Go | HTTP handler spawns goroutine per request but never cancels on client disconnect |
| 13 | memory-leak-event-listener | JavaScript | Event listeners registered in loop, never removed — memory grows until OOM |
| 14 | deadlock-transfer | Java | Two accounts lock in opposite order during concurrent transfers |
| 15 | cache-stale-invalidation | Python | Cache TTL set but never invalidated on write — stale reads for up to 5 minutes |
| 16 | auth-bypass-path-traversal | Go | Middleware checks auth on `/api/` prefix but `/../api/` bypasses it |
| 17 | sql-injection-search | Python | Search endpoint concatenates user input into SQL WHERE clause |
| 18 | rate-limit-bypass-header | TypeScript | Rate limiter keys on X-Forwarded-For, attacker rotates header to bypass |
| 19 | api-version-field-drop | Go | v2 API endpoint silently drops fields that existed in v1, breaks clients |

## Planned — Hard (6)

| # | Name | Language | Description |
|---|------|----------|-------------|
| 20 | data-corruption-concurrent-write | Rust | Two threads write overlapping byte ranges to same file without synchronization |
| 21 | split-brain-leader-election | Go | Network partition causes two nodes to both believe they are leader |
| 22 | compiler-macro-expansion | Rust | Proc macro generates code with incorrect lifetime, passes simple tests but segfaults on complex input |
| 23 | kernel-panic-ioctl | C | Custom ioctl handler doesn't validate user pointer, kernel panics on crafted input |
| 24 | timing-attack-comparison | Go | Password comparison uses `==` instead of constant-time compare, leaks length via timing |
| 25 | performance-cliff-hash | Java | HashMap with bad hash function degrades from O(1) to O(n) at 10k entries |
