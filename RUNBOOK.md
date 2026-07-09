# needle-bench operator runbook

Terse. Operator-grade. The trust doctrine everywhere: **the bench fails
closed** — a cell with missing/zero token accounting on a completed run, a
mismatched requested-vs-executed arm, or a malformed payload is INVALID:
excluded from aggregates, counted visibly. Never a silent 0, never a silent
pass. `absent` (never run) is not INVALID.

## Add a model in 3 steps

1. **Roster** — `run_matrix_v41.py`. Frontier Control+B sweep: add to
   `FRONTIER_2026` with explicit arms: `["native", "kernel-cpu"]` for models
   with a native CpuDriver (claude/gemini/mistral families) AND for the gpt
   family — the scheduler (`has_native_cpu_driver`) treats `gpt-*` as
   native-driver and runs kernel-cpu even though the execution is
   generic_kernel; see known issue "gpt scheduler/labeler mismatch" below
   (gpt-5.5's arms are `["native", "kernel-cpu"]`, NOT the OpenRouter-only
   pattern). `["native", "kernel"]` for OpenRouter-only models —
   grok/deepseek/kimi/qwen. Broad-matrix models go in `NEW_3ARM`/`NEW_2ARM`
   instead.
2. **Consolidator** — `consolidate_scores.py`: add the list price to
   `RATE_CARD` (`"model": ($/M in, $/M out)`). Price not announced yet? OMIT
   the entry — cost falls back to self-reported/unpriced, never $0. NEVER
   paste a `(TBD, TBD)` placeholder: it is a NameError at import (preflight
   import-checks the consolidator and refuses to launch, but it is a
   run-blocker either way). If the model has a hand-written CpuDriver in
   haystack, add the NORMALIZED name (lowercase, `.`→`-`) to
   `CPU_DRIVER_MODELS` and the vendor CLI to `NATIVE_CLI_MAP`. A model with
   no native driver stays OUT of `CPU_DRIVER_MODELS` (it will be labeled
   B*/generic_kernel — that's correct, not a bug).
3. **Docs** — one line in `models.txt` (documentation only; nothing executes
   from it).

Then verify offline: `./bench.sh <model> --dry-run` (must resolve the roster,
show the arms, and count cells). `models.txt` has a PENDING section with the
exact edits pre-written for ids that haven't landed yet — it is NOT one line
per model: gpt-5.6 needs 2 code edits (FRONTIER_2026 now; RATE_CARD only once
priced) and gemini-3.5-pro needs 3 (also CPU_DRIVER_MODELS), plus each
model's `models.txt` uncomment.

## Run a model

```bash
./bench.sh <model-id> [--arms native,kernel-cpu] [--bench a,b] [--dry-run]
           [--offline] [--no-board]
./bench.sh --status      # live cell states — never wonder what the bench is doing
```

- **Preflight fails closed before any spend**: roster, provider env key for
  the model's real routing (per arm), LIVE provider probes, docker daemon,
  frozen-bin content-hash pin + the local musl binary that `--local` ships.
  Verified hashes append to `runs/.binary_identity.jsonl` per launch.
- **Live probes** (free, authenticated models-list calls): verify the key is
  VALID and the EXACT model id EXISTS at the provider each arm actually
  routes through (native vendor endpoints; OpenRouter for kernel arms with
  the registry-formatted id). A miss fails closed printing the closest
  catalog matches — dead keys and wrong-model-name-for-provider die here,
  not 20 minutes into a paid run. Keys resolve env → .env (repo, haystack) →
  `ostk secret env`; values are never printed. `--offline` skips probes;
  `--dry-run` implies it.
- **Stall watch on every cell** (`scripts/stall_watch.py`, wired into
  run_matrix for kernel AND native arms): (1) completion detector — score
  landing, or a terminal journal row (agent.completed / end_turn+no-tools)
  polled from the container every ~2s on kernel arms; terminal evidence + a
  child still alive after 10s grace = hung teardown → SIGKILL process tree +
  container, salvage journal, score the cell (bench's own score stamped, or
  synthesized from the journal), publish `teardown_masked: true` ONLY
  because the kill was needed. (2) no-progress watchdog — WARN at 90s, probe
  at 180s (child alive? container state? docker stats), SIGKILL at 300s →
  the cell is WRITTEN as INVALID reason `stall` and the matrix CONTINUES
  (infra retry budget, then quarantine). Progress = child output growth,
  journal growth, container fs activity (vendor CLIs write session files),
  or score appearance. (3) hard per-cell deadline = the bench's own
  wall_clock budget + 600s overhead, capped at 3600s. Every transition is
  timestamped to `runs/.cell_status.jsonl`; `./bench.sh --status` renders
  it. Thresholds: `OSTK_STALL_{WARN,PROBE,KILL,GRACE,TICK}_S` env overrides.
  The launcher stdout spools live to `runs/<model>-<arm>/<bench>.launcher.log`.
- **Native usage capture** (`scripts/native_usage.py`, runs automatically
  after every native cell, before the validity gate): recovers billed token
  buckets + provider cost from the vendor CLI's own telemetry in
  `<bench>.raw/` — opencode `step_finish` JSONL (grok/deepseek), kimi
  `StatusUpdate/TokenUsage` stream, vibe `vibe-meta.json` session stats —
  with full provenance (`native_usage_source`, `native_usage_prev`). Where
  genuinely unavailable on a completed-work cell, the payload is stamped
  `cost_basis: "unavailable"` explicitly (solve verdict untouched). Sweep
  historical runs: `python3 scripts/native_usage.py runs/ [--apply]`.
- **Resumable**: state is `runs/.state.jsonl` (append-only). Interrupt any
  time; re-run the same command to resume. Valid completed cells are skipped;
  INVALID phantoms (deadline / zero-work) are deleted and re-run up to 2
  infra retries, then quarantined to `runs/.quarantine.jsonl` — quarantined
  cells are never counted as success.
- **Postflight**: `scripts/validate.py runs/ --model <model>` runs the same
  `cell_validity` classification the consolidator uses; any INVALID cell
  aborts the pipeline before the board is touched. Run it standalone against
  any runs subtree — it exits nonzero on invalids and flags score files
  sitting in arm-less directories (the historical `arm = None` bug signature).
- **Prereqs for a real run**: docker up, provider key exported, fresh musl
  binary. Refresh it IN HAYSTACK with
  `cargo build --release --target x86_64-unknown-linux-musl`
  (linker `x86_64-linux-musl-gcc`, per haystack `.cargo/config.toml`).
  `make install` does NOT refresh it — that target installs the HOST debug
  build (`target/debug/ostk`) to `~/.cargo/bin` and never touches the musl
  target. `--local` docker-cps
  `target/x86_64-unknown-linux-musl/release/ostk`; if it's missing the
  in-container install silently falls back to a network download, which
  preflight refuses to let happen unnoticed. Preflight also WARNs (loudly,
  recorded to `runs/.binary_identity.jsonl`) when the local musl hash differs
  from the frozen-bin pin — read that WARN before paying: a stale musl binary
  silently benchmarks the OLD kernel.
- Direct runner access (finer control): `python3 run_matrix_v41.py --frontier
  --model <m> --arm kernel-cpu --retry-failed --samples 1 ...`.

## Read the board

`public/experiment-scores.json` (copied verbatim into `dist/` by
`npm run build`; served at needle-bench.cc):

- `models[].ostk` — THE ostk column. An alias of exactly one underlying arm
  (`source_arm`: `cpu` for native_driver models, `kernel` for
  generic_kernel), never a sum: one execution never appears in two columns.
  Twin columns carry `status: not_applicable — ...` instead of a fake 0/0.
- `trust` — fail-closed accounting: `valid_cells`, `invalid_cells`,
  `invalid_by_reason`, `teardown_masked_cells`, `policy_excluded_cells`.
  `invalid_cells[]` lists every excluded cell with reasons.
- `models[].{native,kernel,cpu}.invalid` — per-arm INVALID counts (visible,
  not silently dropped). `masked` counts watchdog-scored cells.
- Costs: every arm carries `cost_usd` + `cost_basis` (and a `cost_bases`
  histogram) — `bucket-split` is the fully-split gold path; `total-as-fresh`
  and `folded-as-fresh` are explicit over-stating floors; `unpriced` is never
  silently published as $0. `tokens` is cache-inclusive billed input+output;
  `tokens_fresh` keeps the old fresh-only number.
- Only within-snapshot native-vs-kernel deltas are trustworthy across
  releases; `runs/.binary_identity.jsonl` + `frozen-bin/` pin the binary.

Regenerate manually: `python3 consolidate_scores.py` then `npm run build`.

## Known issues

- **Kernel teardown hang** (LIVE, root cause →862/→1340): futex deadlock at
  shutdown. Affected cells are scored via a journal+SIGKILL watchdog and are
  published WITH `teardown_masked: true` (per-cell, per-arm `masked` counts,
  `trust.teardown_masked_cells`). Masking is visible, never silent — but the
  underlying kernel bug is unfixed; treat masked cells as carrying a trust
  asterisk.
- **ostk self-cost under-report ~13%** (→2068, open): the kernel's own cost
  accounting under-reports; board costs for kernel arms are a floor until it
  lands.
- **GPT cache re-measure** (→2069, pending): one probe showed 42.8% cache
  read-share after the measurement fix; the formal gpt re-run has not
  happened. gpt-5.5 kernel cells predate it.
- **gpt scheduler/labeler mismatch**: `run_matrix_v41.has_native_cpu_driver`
  treats `gpt-*` as native-driver (schedules kernel-cpu) while the
  consolidator correctly labels the execution generic_kernel (B*). The board
  annotates this (`ostk.note` on gpt-5.5); don't "fix" it by adding gpt to
  `CPU_DRIVER_MODELS`.
- **Non-Anthropic native token accounting** (RECOVERED 2026-07-09): the
  vendor CLIs' score writer recorded 0 billed tokens, but the raw telemetry
  survived in `<bench>.raw/` — `scripts/native_usage.py --apply` recovered
  real buckets + cost for 148 cells (grok-4.3, deepseek-v4-pro, kimi-k2.6:
  all 38 each; devstral-2512: 34), cross-checked (recovered fresh+cache_read
  equals the old folded input_tokens exactly). Those cells are VALID again
  with `native_usage_source` provenance. New native runs capture
  automatically in run_matrix. Cells with genuinely no telemetry on
  completed work carry `cost_basis: "unavailable"` (cost axis only — the
  solve verdict stands).
- **gemini-3.5 family kernel stall** (deferred 2026-06-14): agent stalls past
  the poll deadline on the 3.x thinking path; verify before paying for any
  gemini-3.5-* matrix run.
