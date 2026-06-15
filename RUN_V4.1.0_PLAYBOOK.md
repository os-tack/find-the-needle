# needle-bench v4.1.0 sweep — playbook

## Prep (done)
- [x] `models.txt` updated: Opus/Sonnet/Haiku 4.7, GPT-5.2/5.4/5.5/5.5-Pro, Kimi K2.6, Gemini 3.1 Flash Lite, Qwen 3.6-35B-A3B, Ternary-Bonsai-8B
- [x] `models.conf` updated with provider routing for 4.7 family + mlx
- [x] `run_matrix_v41.py` — resumable coordinator with per-cell state
- [x] `archive_v40_kernel.sh` — moves kernel/kernel-cpu dirs aside pre-sweep
- [x] `smoke_test_new_models.sh` — 1 benchmark per new model, pre-flight check

## Wait for v4.1.0 (external)
- `ostk --version` flips from `4.0.0` to `4.1.x`
- `ostk mlx` subcommand becomes available
- Confirm MLX arm wiring — see "MLX arm resolution" below

## v4.1.0 install day — order of operations

### 1. Upgrade ostk
```
cd ~/projects/haystack
git pull
make install   # or: cargo install --path .
ostk --version   # should show 4.1.x
ostk mlx models  # should list MLX models (smoke test for v4.1)
```

### 2. Smoke test new model IDs (catch bad OpenRouter IDs before the big run)
```
cd ~/projects/needle-bench
./smoke_test_new_models.sh 2>&1 | tee smoke.log
```
Expect: most cells PASS. Any FAIL means: bad model ID, missing vendor CLI, or docker wiring issue. Fix before proceeding.

If GPT-5.5 IDs are wrong, edit `models.txt` + `run_matrix_v41.py` roster.

### 3. Smoke test MLX-bonsai arm
```
# Start mlx_lm.server for the bonsai model
ostk mlx start --model prism-ml/Ternary-Bonsai-8B-mlx-2bit
ostk mlx status   # confirm "ready"

# Smoke test it
RUN_MLX=1 ./smoke_test_new_models.sh   # runs just the MLX cells
```

### 4. Archive v4.0.0 kernel scores
```
./archive_v40_kernel.sh
# moves runs/*-kernel runs/*-kernel-cpu -> runs-v4.0-archive-YYYYMMDD/
# native dirs stay put (native arm is ostk-version-independent)
```

### 5. Coordinated sweep
```
# Full matrix: existing models (kernel arms only) + new models (all arms)
nohup python3 run_matrix_v41.py --ostk-version-gate 4.1 2>&1 | tee matrix-v41.log &

# Progress:
tail -f matrix-v41.log
# Or count completions:
wc -l runs/.state.jsonl
```

### 6. If the run is interrupted (reboot, crash, you hit Ctrl-C)
Just re-run the same command — it reads `runs/.state.jsonl` and skips:
- cells with score files
- cells marked `success` in state
- cells marked `transient_fail` that exhausted retries (unless `--retry-failed`)
- cells marked `failed` (unless `--retry-failed`)

### 7. Retry hard failures after investigation
```
# Look at the punch list printed at end of run_matrix_v41.py
# Investigate individual failures:
ostk bench <bench> --model <model> <arm-flags> --docker --keep

# Once fixed, retry the whole failed-cell set:
python3 run_matrix_v41.py --retry-failed
```

### 8. Consolidate + publish
`run_matrix_v41.py` auto-invokes `consolidate_scores.py` on clean completion.
To run manually:
```
python3 consolidate_scores.py
# Writes public/scores.json + public/experiment-scores.json
```

## Targeted runs (subsets)
```
# Just the new Anthropic family
python3 run_matrix_v41.py --model claude-opus-4-7 --model claude-sonnet-4-7 --model claude-haiku-4-7

# Just one arm across everyone
python3 run_matrix_v41.py --arm kernel-cpu

# Single benchmark across the new models
python3 run_matrix_v41.py --only-new --bench auth-bypass-path-traversal

# Dry-run (no execution, no state writes)
python3 run_matrix_v41.py --dry-run --only-new
```

## MLX arm resolution — TBD

As of v4.0.0, `ostk bench` has no MLX path. One of the following lands with v4.1.0:

**Option A — `--driver mlx` flag** (most likely; mirrors `--driver cpu`):
```
ostk bench <bench> --model ternary-bonsai-8b --arm kernel --driver mlx --local --docker
```
Score dir: `runs/ternary-bonsai-8b-kernel-mlx/`
→ In `run_matrix_v41.py`, add `"kernel-mlx"` to `ARM_FLAGS` with `["--arm", "kernel", "--driver", "mlx", "--local"]`.

**Option B — model prefix routing** (`mlx/` → use mlx driver automatically):
```
ostk bench <bench> --model mlx/ternary-bonsai-8b --arm kernel --local --docker
```
Score dir: `runs/mlx-ternary-bonsai-8b-kernel/`
→ In coordinator, use the prefixed name in the roster.

**Option C — openrouter with base_url override** (`mlx_lm.server` exposes OpenAI-compatible API):
```
OPENAI_BASE_URL=http://127.0.0.1:11942/v1 \
  ostk bench <bench> --model prism-ml/Ternary-Bonsai-8B-mlx-2bit --arm kernel --local --docker
```

**Decision**: run one smoke cell under each option at v4.1.0 drop. Whichever produces a valid score file is the one we adopt. Update `run_matrix_v41.py` roster + `ARM_FLAGS` accordingly before the sweep.

## Cost envelope (rough)

~1100 cells pending (new models + kernel arms re-run on existing).
At avg ~$0.05/cell (mix of Haiku/Flash/cheap-tier) with Opus 4.7 / GPT-5.5-Pro at $0.50+:
- Estimate: **$150-300** for the full sweep.
- GPT-5.5-Pro alone across 40 benchmarks × 2 arms at ~$1/bench = ~$80.

## State file reference
`runs/.state.jsonl` — append-only, one JSON row per attempt:
```json
{"ts":"2026-04-23T...","model":"claude-opus-4-7","bench":"auth-bypass-path-traversal","arm":"kernel","status":"success","attempts":1,"tail":"(42s)"}
```
Latest row per cell wins. Safe to delete to force a full re-run (combined with archive or score-file cleanup).
