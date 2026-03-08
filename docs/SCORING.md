# needle-bench Scoring

> Version 1.0 — 2026-03-08

## Overview

Every benchmark run produces 11 metrics. No metric is optional. Scoring is deterministic — same run, same scores.

## Metrics

### 1. `resolved` (boolean)

Did the agent's patch make `test.sh` exit 0?

- `true`: test passes after agent modifications
- `false`: test still fails or agent exhausted limits

This is the only metric that matters for the leaderboard headline number. Everything else is texture.

### 2. `turns_to_discovery` (integer)

Number of conversation turns before the agent first identifies the correct file and region containing the bug.

- Measured by comparing agent's edits/mentions against the files touched in `solution.patch`
- If never discovered: set to the turn limit

### 3. `turns_to_fix` (integer)

Number of conversation turns before the agent produces a patch that makes `test.sh` pass.

- If never fixed: set to the turn limit
- `turns_to_fix >= turns_to_discovery` always

### 4. `signal_to_noise` (float, 0.0–1.0)

Ratio of productive actions to total actions.

```
signal_to_noise = productive_turns / total_turns
```

A turn is "productive" if it either:
- Reads/examines a file that is relevant to the bug (touched by solution.patch or its direct dependencies)
- Makes an edit that moves toward the fix

### 5. `false_positives` (integer)

Number of distinct files the agent edited that are NOT touched by `solution.patch`.

- Edits to test files don't count (agents commonly run tests)
- Reverted edits don't count
- Only final-state modifications at scoring time

### 6. `token_cost` (integer)

Total tokens consumed (input + output) across all turns.

- Measured from the model API response metadata
- Includes tool call tokens

### 7. `tokens_per_correct_line` (float)

Efficiency metric: tokens spent per correctly-changed line.

```
tokens_per_correct_line = token_cost / correct_lines_changed
```

- `correct_lines_changed` = lines in the agent's final patch that match lines in `solution.patch`
- If zero correct lines: set to `Infinity`

### 8. `recovery_events` (integer)

Number of times the agent went down an incorrect path and self-corrected.

A recovery event is detected when:
- Agent reverts a previous edit, OR
- Agent explicitly acknowledges a wrong approach and changes direction

### 9. `recovery_rate` (float, 0.0–1.0)

```
recovery_rate = successful_recoveries / recovery_events
```

A recovery is "successful" if the agent eventually reaches the correct fix after the recovery event. If `recovery_events` is 0, `recovery_rate` is `1.0` (no recovery needed = perfect).

### 10. `wall_clock` (float, seconds)

Total wall-clock time from first agent turn to final scoring.

- Includes all pauses, retries, and tool execution time
- Measured by the harness, not the agent

### 11. `blind_discovery` (boolean)

Did the agent find the bug without the optional `PROMPT` directive?

- `true` if the Agentfile has no `PROMPT` directive and the agent resolved the bug
- `false` otherwise
- Benchmarks with `PROMPT` always score `false` here
- This metric rewards agents that can diagnose from test output alone

## Score Record Format

```json
{
  "benchmark": "off-by-one-redis",
  "agent": "claude-opus-4-20250514",
  "timestamp": "2026-03-08T12:00:00Z",
  "resolved": true,
  "turns_to_discovery": 3,
  "turns_to_fix": 7,
  "signal_to_noise": 0.82,
  "false_positives": 1,
  "token_cost": 45200,
  "tokens_per_correct_line": 3015,
  "recovery_events": 1,
  "recovery_rate": 1.0,
  "wall_clock": 142.5,
  "blind_discovery": true
}
```

## Leaderboard Ranking

Primary sort: `resolved` (descending — solvers first)
Secondary sort: `turns_to_fix` (ascending — fewer turns wins)
Tertiary sort: `token_cost` (ascending — cheaper wins)

Ties at all three levels are broken by `wall_clock` (ascending).

## Aggregation

When an agent runs multiple benchmarks, aggregate scores are:
- `resolve_rate`: percentage of benchmarks resolved
- `mean_turns_to_fix`: geometric mean of turns_to_fix across resolved benchmarks
- `mean_token_cost`: geometric mean of token_cost across resolved benchmarks
- `blind_discovery_rate`: percentage of PROMPT-free benchmarks resolved
