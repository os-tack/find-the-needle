# needle-bench

**Your worst debugging day, everyone's benchmark.**

A benchmark suite of 29 scenarios for AI coding agents, built from real bugs in real codebases. No synthetic tasks. No contrived puzzles. Just broken containers and one prompt: *find the needle.*

> **v4.6.1 — 2026-04-28 — what to trust, what not to.**
>
> The kernel + kernel-cpu arms are the trustable signal in this release.
> Per-cell turns, tokens, and cost on those arms reflect real model work
> against real bugs. The 50-turn cap that quietly truncated kernel-arm
> agents in v4.6.0 is gone (Agentfile parser bug — `LIMIT max_turns`
> vs `LIMIT turns`); cells now run to genuine completion.
>
> **Native arm: read with caution.** Vendor-CLI tokens include harness
> scaffolding the kernel arms don't pay, so cost/token comparisons across
> arms aren't apples-to-apples. The claude-code harness specifically has
> a verification disconnect — the agent reports "test.sh passes" while
> ostk's post-run verification disagrees — so claude-code native cells
> may be under-reported. opencode/codex/kimi-cli native paths look fine.
>
> **Deadline-killed cells show 0 turns / $0.** Pre-SIGTERM journal
> snapshot is broken; affected cells under-report. If you see `t=0` on
> a cell that took 11 minutes, that's a slow-model-vs-wall-clock
> result, not infrastructure failure.
>
> Resolution rates support the leaderboard's PASS/FAIL claims. Token
> and cost figures support kernel-vs-kernel-cpu comparison; cross-arm
> cost claims are not yet supported.

## Operator runbook

Running the bench (not writing benchmarks)? See [RUNBOOK.md](RUNBOOK.md):
one-command runs via `./bench.sh <model-id> [--dry-run]` (preflight →
resumable matrix → fail-closed validation → board regen), adding a model in
3 steps, reading the board's trust block, and known measurement caveats.

## How it works

Each benchmark is a Docker container with a real bug. The agent gets tools (`shell`, `file:read`, `file:edit`), a time limit, and a test that fails. The agent explores, diagnoses, and patches. The test either passes or it doesn't.

```
benchmarks/off-by-one-pagination/
  Dockerfile              # broken codebase, containerized
  Agentfile               # agent config: tools, limits
  .bench/solution.patch   # sealed truth (agent never sees this)
  test.sh                 # exit 0 = fixed, exit 1 = broken
```

Scenarios span concurrency bugs, security bypasses, encoding issues, k8s operational failures, and more.

## Quick start

```bash
# Validate all benchmarks
make validate

# Run a specific benchmark
make run BENCH=off-by-one-pagination

# List available benchmarks
make list
```

## 11 metrics, no opinions

Every run produces the same 11 numbers. See [SCORING.md](docs/SCORING.md).

| Metric | What it measures |
|--------|-----------------|
| resolved | Did the agent fix the bug? |
| turns_to_discovery | How fast did it find the right file? |
| turns_to_fix | How fast did it produce a working patch? |
| signal_to_noise | What fraction of actions were productive? |
| false_positives | How many wrong files did it edit? |
| token_cost | Total tokens consumed |
| tokens_per_correct_line | Efficiency per correct change |
| recovery_events | How many times did it self-correct? |
| recovery_rate | How often did self-correction succeed? |
| wall_clock | Total time |
| blind_discovery | Did it find the bug with no hints? |

## Submit a benchmark

Your worst debugging day is everyone's benchmark. See [CONTRIBUTING.md](docs/CONTRIBUTING.md).

```bash
cp -r benchmarks/_template benchmarks/your-bug-name
# Edit the files, then:
make validate BENCH=your-bug-name
```

## Leaderboard

Scores are published at [needle-bench.cc](https://needle-bench.cc). 24 models evaluated across 28 benchmarks so far.

Primary rank: resolve rate. Tiebreaker: fewer turns, then fewer tokens.

To regenerate the public leaderboard from individual run scores:

```bash
python3 consolidate_scores.py          # writes public/scores.json
python3 consolidate_scores.py --dry-run # preview without writing
```

## Spec

The full benchmark format specification is in [SPEC.md](docs/SPEC.md).

## License

Apache 2.0. See [LICENSE](LICENSE).

---

*[os-tack/find-the-needle](https://github.com/os-tack/find-the-needle) -- built by Claude Code.*
