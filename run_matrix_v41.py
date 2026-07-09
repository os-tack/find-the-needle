#!/usr/bin/env python3
"""
needle-bench v4.1.0 matrix coordinator — resumable, per-cell retries.

State file: runs/.state.jsonl  (append-only, one JSON row per cell attempt)
Cell:      (model, bench, arm)
Status:    pending | success | failed | transient_fail

Resume semantics:
  - success: skip
  - transient_fail (< MAX_RETRIES): retry with backoff
  - failed (hard): skip; listed in final punch-list
  - missing: run

Invoke:
  ./run_matrix_v41.py                      # full matrix per ROSTER config
  ./run_matrix_v41.py --only-new           # only newly-added models
  ./run_matrix_v41.py --arm kernel         # restrict arms
  ./run_matrix_v41.py --model claude-opus-4-7  # restrict models
  ./run_matrix_v41.py --dry-run            # show what would run
  ./run_matrix_v41.py --retry-failed       # also retry hard-failed cells
"""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys, time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent.resolve()
RUNS = ROOT / "runs"
STATE = RUNS / ".state.jsonl"

# stall_watch / native_usage live in scripts/ (no package __init__ — path import).
sys.path.insert(0, str(ROOT / "scripts"))
import native_usage  # noqa: E402  (native-arm billed-usage capture)
import stall_watch   # noqa: E402  (merciless per-cell stall detection)

# ---------- roster ----------
# Each entry: model -> dict(arms=[...], category="existing"|"new", new_in_v41=bool)
# "existing": already had v4.0.0 data; on v4.1.0, only kernel arms re-run.
# "new": first-time bench; on v4.1.0, ALL applicable arms run.
# arms[] lists which arms are applicable (mlx is kernel-only; some models have no native).
EXISTING_3ARM = [
    "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6",
    "devstral-2512", "devstral-medium", "devstral-small",
    "kimi-k2.5",
]
EXISTING_2ARM = [
    # gemini models: native via GeminiCpuDriver (kernel-cpu arm) — no CLI harness
    "gemini-2.5-flash", "gemini-2.5-pro",
    "gemini-3-flash-preview", "gemini-3.1-pro-preview",
    "codestral-2508", "gpt-4.1", "gpt-5-codex", "o3", "o4-mini",
    "grok-3", "grok-3-mini", "grok-4", "grok-4-fast",
    "grok-4.1-fast", "grok-4.20", "grok-code-fast-1",
    "deepseek-r1", "deepseek-r1-0528", "deepseek-v3.2",
    "qwen3-coder", "qwen3-coder-flash", "qwen3-coder-plus",
    "llama-4-maverick",
]
NEW_3ARM = [
    "claude-opus-4-7",  # sonnet-4-7 and haiku-4-7 not released — dropped 2026-04-28
    "kimi-k2.6",
    # Audit 2026-04-28: codex CLI 0.125.0 accepts all gpt-5.x — promote to 3ARM
    "gpt-5.2", "gpt-5.4",
    "gpt-5.5",  # gpt-5.5-pro dropped 2026-06-14 (owner: too expensive at $30/$180 API list)
]
NEW_2ARM = [
    # gemini models: native arm uses GeminiCpuDriver via kernel-cpu — no CLI harness
    "gemini-3.1-flash-lite-preview",
    "qwen3.6-35b-a3b",
]
# MLX-bonsai: local inference via ostk v4.1.0 `mlx` subcommand.
# Kernel-only (no native CLI, no remote provider).
# MLX-bonsai: local inference via ostk v4.1.0 `mlx` subcommand.
# Kernel-only — no native CLI exists, no remote provider.
# Arms TBD on v4.1.0 release:
#   - "kernel-mlx" is the likely label (driver=mlx, via `--arm kernel --driver mlx`)
#   - may also run under plain "kernel" with openrouter base_url pointed at mlx_lm.server
# For now: we list it under EXISTING arm names and will adjust on v4.1.0 drop.
NEW_MLX_KERNEL = [
    "ternary-bonsai-8b",
]

# ---------- frontier 2026 (approach-a Control + B sweep) ----------
# Curated 10-model roster for the clean Control(native) + B baseline on the
# current frontier running ostk v7.6.0. Selected via `--frontier`, which
# bypasses the EXISTING/NEW category logic and the has_native_cpu_driver
# gating entirely: each entry's arms are taken verbatim.
#   Tier-1 (native CpuDriver → Control + true B):  ["native", "kernel-cpu"]
#   Tier-2 (no native driver → Control + B* generic): ["native", "kernel"]
FRONTIER_2026 = {  # model -> arms (exact, no gating)
    # Tier-1: true native driver, true B = kernel-cpu
    # claude-fable-5: Anthropic frontier (added 2026-07-09). Routes native ->
    # claude-code CLI (prefix claude-, bench.rs detect_native_harness) and
    # kernel-cpu -> native AnthropicClient CpuDriver (providers.rs claude* ->
    # Anthropic), so Tier-1 like the other claude models: one ostk execution
    # under kernel-cpu, never a redundant plain-kernel twin.
    "claude-fable-5":    ["native", "kernel-cpu"],
    "claude-opus-4-8":   ["native", "kernel-cpu"],
    # claude-sonnet-5: added 2026-07-09. Tier-1 routing like the other claude
    # models. The v7.7.1 pinned binaries (musl 6246fda9 / host ceb69933) have
    # no claude-sonnet-5 CPU model entry — the kernel WOULD dispatch it via
    # the sonnet-default pricing fallback ($3/$15), but Sonnet 5 is on
    # INTRODUCTORY pricing ($2/$10) through 2026-08-31, so kernel cells on
    # this pin would overcount cost 1.5x. Run native-only (`--arms native`)
    # until a re-pin carries the entry (added to haystack src/cpu/session.rs
    # 2026-07-09, intro-priced branch, delete on 2026-09-01). Native cost
    # caveat: CLI-reported costUSD matched neither intro nor standard rate on
    # spot-check (long-context premium above 200K input suspected) — treat
    # native cost axis as CLI-estimated; re-price from token counts if it
    # matters.
    "claude-sonnet-5":   ["native", "kernel-cpu"],
    "claude-sonnet-4-6": ["native", "kernel-cpu"],
    "gemini-3.1-pro-preview": ["native", "kernel-cpu"],
    # gemini-3.5-flash: DEFERRED 2026-06-14. The is_gemini_3 thinking-config gate
    # (cpu/gemini.rs) was fixed to include the 3.5 family, but the kernel arm then
    # surfaced a SECOND bug: the agent makes initial api.calls then stalls/over-runs
    # the 660s poll deadline on even an easy bench (0 turns/0 tokens/deadline_exceeded
    # — while gemini-3.1-pro-preview is healthy on the SAME driver). Needs deeper
    # gemini thinking-response/loop work (likely thoughtSignature echo across turns).
    # gemini-3.1-pro-preview remains the gemini frontier data point. (qwen3-coder
    # also dropped — native DashScope key-blocked from ES.)
    # 2026-07-09: re-rostered for a bounded smoke re-test of the kernel-cpu arm
    # under the stall-watch machinery (consolidator already carries its
    # RATE_CARD/CPU_DRIVER_MODELS/NATIVE_CLI_MAP entries). The June-14 stall
    # above is still the expectation until proven otherwise — do NOT pay for a
    # full matrix run on this id without a green smoke cell first.
    "gemini-3.5-flash":  ["native", "kernel-cpu"],
    "devstral-2512":     ["native", "kernel-cpu"],
    # gpt-5.5: runs the kernel-cpu arm but ostk v7.6.0 has no native OpenAI
    # Responses-API driver, so it falls back to generic OpenRouter → labeled B*
    # (generic_kernel) in consolidate_scores. gpt-5.5-pro dropped 2026-06-14
    # (owner: too expensive at $30/$180 API list, and B* anyway).
    "gpt-5.5":           ["native", "kernel-cpu"],
    # Tier-2: no native driver, B* = generic OpenRouter kernel. Native Control
    # runs via the opencode fallback against the provider's own key (xAI /
    # DeepSeek keys added by owner 2026-06-15). Not a first-party vendor CLI
    # (xAI/DeepSeek ship none), but own-key on the provider endpoint — noted in
    # results. kimi native = kimi CLI.
    "grok-4.3":          ["native", "kernel"],
    "deepseek-v4-pro":   ["native", "kernel"],
    "kimi-k2.6":         ["native", "kernel"],
}

@dataclass
class ModelSpec:
    name: str
    arms: list[str]
    category: str  # "existing" | "new"

def has_native_cpu_driver(model: str) -> bool:
    """Whether `model` has a hand-written native CpuDriver in haystack.
    kernel-cpu arm is only meaningful for these models — for everything else
    kernel-cpu is identical to kernel (both route via OpenRouterClient), so
    running both is redundant compute.

    Native drivers (haystack/src/cpu/{anthropic,gemini,mistral,openrouter}.rs):
    - Anthropic:  claude-*
    - Google:     gemini-*
    - Mistral:    mistral-*, codestral-*, devstral-*, ministral-*, magistral-*
    - OpenAI:     gpt-*, o3, o4-mini, gpt-5-codex (uses OpenRouterClient
                  pointed at api.openai.com — separate URL but shared client)
    - MLX:        mlx/*  (local mlx_lm.server)

    NOT native (use OpenRouter for both kernel and kernel-cpu):
    - grok-*, kimi-*, qwen*, deepseek-*, llama-*
    """
    prefixes = (
        "claude-",
        "gemini-",
        "mistral-", "codestral-", "devstral-", "ministral-", "magistral-",
        "gpt-", "o3", "o4-mini",
        "mlx/",
    )
    return any(model.startswith(p) for p in prefixes)


def roster(frontier: bool = False) -> list[ModelSpec]:
    """Build the model × arm matrix, dropping kernel-cpu for models without a
    native CpuDriver (where kernel-cpu would be a redundant duplicate of
    kernel via OpenRouter).

    When `frontier` is True, return EXACTLY the FRONTIER_2026 roster with each
    model's arms taken verbatim — no EXISTING/NEW category logic, no
    has_native_cpu_driver gating. This is the approach-(a) Control + B sweep."""
    if frontier:
        return [ModelSpec(m, list(arms), "new") for m, arms in FRONTIER_2026.items()]
    r = []
    def arms_for(default_arms: list[str], model: str) -> list[str]:
        if "kernel-cpu" in default_arms and not has_native_cpu_driver(model):
            return [a for a in default_arms if a != "kernel-cpu"]
        return default_arms

    for m in EXISTING_3ARM:
        r.append(ModelSpec(m, arms_for(["kernel", "kernel-cpu"], m), "existing"))
    for m in EXISTING_2ARM:
        r.append(ModelSpec(m, arms_for(["kernel", "kernel-cpu"], m), "existing"))
    for m in NEW_3ARM:
        r.append(ModelSpec(m, arms_for(["native", "kernel", "kernel-cpu"], m), "new"))
    for m in NEW_2ARM:
        r.append(ModelSpec(m, arms_for(["kernel", "kernel-cpu"], m), "new"))
    for m in NEW_MLX_KERNEL:
        r.append(ModelSpec(m, ["kernel", "kernel-cpu"], "new"))  # mlx/ has Mlx provider
    return r

# ---------- benchmarks ----------
# Benchmarks retired from the active suite: legacy tests predating the ostk
# rename / the kernel's real existence — their oracles are not valid for this
# comparison, so they are neither run nor published. Score files are archived
# (not deleted) under runs-archive/legacy-pre-kernel-<ts>/.
RETIRED_BENCHMARKS = {"haystack-boot", "haystack-mint"}

def load_benchmarks() -> list[str]:
    bdir = ROOT / "benchmarks"
    names = []
    for p in sorted(bdir.iterdir()):
        if p.is_dir() and not p.name.startswith("_") and p.name not in RETIRED_BENCHMARKS:
            names.append(p.name)
    return names

# ---------- state ----------
def load_state() -> dict:
    """Return dict[(model,bench,arm)] -> latest row."""
    cells = {}
    if not STATE.exists():
        return cells
    with STATE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = (row["model"], row["bench"], row["arm"])
            cells[k] = row
    return cells

def append_state(row: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("a") as f:
        f.write(json.dumps(row) + "\n")

QUARANTINE = RUNS / ".quarantine.jsonl"

def append_quarantine(model: str, bench: str, arm: str,
                      stop_reason: str, reason: str) -> None:
    """Log a quarantined (infra-budget-exhausted) cell. Append-only; one JSON
    row per quarantine event. A quarantined cell is NOT counted as success and
    is NOT retried again."""
    QUARANTINE.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "model": model, "bench": bench, "arm": arm,
        "stop_reason": stop_reason, "reason": reason,
        "ts": now_iso(),
    }
    with QUARANTINE.open("a") as f:
        f.write(json.dumps(row) + "\n")

def score_exists(model: str, bench: str, arm: str) -> bool:
    return (RUNS / f"{model}-{arm}" / f"{bench}.score.json").exists()

def score_path(model: str, bench: str, arm: str) -> Path:
    return RUNS / f"{model}-{arm}" / f"{bench}.score.json"

def samples_path(model: str, bench: str, arm: str) -> Path:
    return RUNS / f"{model}-{arm}" / f"{bench}.samples.json"

def read_score_resolved(model: str, bench: str, arm: str) -> bool | None:
    p = score_path(model, bench, arm)
    if not p.exists():
        return None
    try:
        return bool(json.loads(p.read_text()).get("resolved", False))
    except Exception:
        return None

def append_sample(model: str, bench: str, arm: str, attempt: int) -> None:
    """F7: copy the current .score.json into the samples sidecar so we keep
    every attempt's record for variance / majority-vote analysis."""
    sp = score_path(model, bench, arm)
    if not sp.exists():
        return
    sample = {}
    try:
        sample = json.loads(sp.read_text())
    except Exception:
        return
    sample["sample_attempt"] = attempt
    sample["sample_ts"] = now_iso()
    sxp = samples_path(model, bench, arm)
    samples = []
    if sxp.exists():
        try:
            samples = json.loads(sxp.read_text())
            if not isinstance(samples, list):
                samples = []
        except Exception:
            samples = []
    samples.append(sample)
    sxp.parent.mkdir(parents=True, exist_ok=True)
    sxp.write_text(json.dumps(samples, indent=2))

def majority_resolved(model: str, bench: str, arm: str) -> bool | None:
    """Return majority verdict from samples sidecar, or None if no samples."""
    sxp = samples_path(model, bench, arm)
    if not sxp.exists():
        return None
    try:
        samples = json.loads(sxp.read_text())
    except Exception:
        return None
    if not samples:
        return None
    yes = sum(1 for s in samples if s.get("resolved"))
    return yes * 2 > len(samples)  # strict majority

# ---------- runner ----------
ARM_FLAGS = {
    "native":      ["--arm", "native"],
    # --local: docker-cp the local musl binary into the container instead
    # of curl-downloading from ostk.ai. The download path flakes under
    # high parallelism (15-way sweep saw ~15-30% kernel-cpu cells fail
    # with `ostk: command not found`). The local binary path is a single
    # docker cp per container — reliable, idempotent, no network.
    # Requires haystack's musl bench binary to be fresh (make install).
    "kernel":      ["--arm", "kernel", "--local"],
    "kernel-cpu":  ["--arm", "kernel",  "--driver", "cpu", "--local"],
    "kernel-mlx":  ["--arm", "kernel",  "--driver", "mlx", "--local"],
}

# ---------- host bench harness (explicit binary, never bare PATH ostk) ----------
# The HOST-side ostk orchestrates every cell and writes score.json — it is a
# second binary identity, separate from the musl pin that ships INTO the
# container. Invoking bare `ostk` from PATH silently runs whatever was
# installed last (the Jul-9 smoke ran a stale Jul-2 dev build that predated
# the requested_arm/executed_arm/bench_binary receipts and the --profile
# oneshot dispatch fix). The matrix therefore resolves the harness explicitly:
#   1. $OSTK_BENCH_BIN — deliberate operator override (hashed + logged, may
#      differ from the pin for dev runs);
#   2. the single frozen-bin/ostk-host-<sha256> content-hash pin (the filename
#      IS the pin record; content hash re-verified before any cell runs).
# There is NO PATH fallback — missing/corrupt/ambiguous pins fail closed.
FROZEN_BIN = ROOT / "frozen-bin"

_HOST_OSTK: tuple[Path, str] | None = None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_host_ostk() -> tuple[Path, str]:
    """(path, sha256) of the host ostk harness, fail-closed (see block above).
    Cached: the hash is verified once per process, before the first cell."""
    global _HOST_OSTK
    if _HOST_OSTK is not None:
        return _HOST_OSTK
    override = os.environ.get("OSTK_BENCH_BIN")
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise SystemExit(f"ERROR: OSTK_BENCH_BIN={override!r} is not a file")
        _HOST_OSTK = (p, _sha256_file(p))
        return _HOST_OSTK
    pins = sorted(FROZEN_BIN.glob("ostk-host-*"))
    if not pins:
        raise SystemExit(
            "ERROR: no frozen-bin/ostk-host-<sha256> pin and no $OSTK_BENCH_BIN "
            "override — refusing to fall back to PATH `ostk` (a stale harness "
            "writes receipt-less scores). Build in haystack, then pin: "
            "cp target/release/ostk frozen-bin/ostk-host-<its sha256>")
    if len(pins) > 1:
        raise SystemExit(
            f"ERROR: {len(pins)} ostk-host-* pins in frozen-bin/ "
            f"({', '.join(p.name for p in pins)}) — exactly one must glob; "
            f"archive the others (see frozen-bin/archive-*/)")
    pin = pins[0]
    claimed = pin.name.removeprefix("ostk-host-")
    actual = _sha256_file(pin)
    if actual != claimed:
        raise SystemExit(
            f"ERROR: HOST PIN CORRUPT: {pin.name} content hash is {actual} "
            f"(filename claims {claimed}) — re-pin from a trusted build")
    _HOST_OSTK = (pin, actual)
    return _HOST_OSTK


MAX_RETRIES = 2
BACKOFF_SEC = [10, 60]  # 2 retries with escalating backoff
WALL_TIMEOUT_SEC = 1800  # legacy flat cap; superseded by cell_deadline_s below
                         # (kept for external scripts that import it)

# ---------- per-cell hard deadline (stall_watch) ----------
# The deadline comes from the BENCHMARK'S OWN wall_clock budget (difficulty.json
# tier, or the SLOW_SCENARIOS override below) plus fixed overhead for container
# build/boot/score/teardown — capped, so no cell can silently ride a flat
# 30-minute timeout that dwarfs its real 10-minute budget.
DEADLINE_OVERHEAD_SEC = 600
DEADLINE_CAP_SEC = 3600

_DIFFICULTY_WALL: dict[str, int] = {}


def _load_difficulty_walls() -> dict[str, int]:
    """bench -> wall_clock seconds from difficulty.json (empty on trouble)."""
    if _DIFFICULTY_WALL:
        return _DIFFICULTY_WALL
    try:
        data = json.loads((ROOT / "difficulty.json").read_text())
        tiers = data.get("tiers", {})
        for bench, tier in data.get("benchmarks", {}).items():
            wall = (tiers.get(tier) or {}).get("wall_clock")
            if wall:
                _DIFFICULTY_WALL[bench] = int(wall)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return _DIFFICULTY_WALL


def bench_wall_clock_s(bench: str) -> int:
    """The benchmark's own wall_clock budget (difficulty tier; SLOW_SCENARIOS
    override wins when larger; 600s default)."""
    wall = _load_difficulty_walls().get(bench, 600)
    return max(wall, SLOW_SCENARIOS.get(bench, 0))


def cell_deadline_s(bench: str) -> int:
    """Per-cell hard deadline: bench budget + overhead, sanely capped."""
    return min(bench_wall_clock_s(bench) + DEADLINE_OVERHEAD_SEC, DEADLINE_CAP_SEC)


def bench_difficulty_tier(bench: str) -> str:
    try:
        data = json.loads((ROOT / "difficulty.json").read_text())
        return data.get("benchmarks", {}).get(bench, "medium")
    except (OSError, json.JSONDecodeError):
        return "medium"

# ---------- completeness gate (→2062 / WS2) ----------
# INFRA_RETRIES is a budget SEPARATE from the transient MAX_RETRIES above.
# MAX_RETRIES covers in-attempt transient network noise (rate-limit / 5xx).
# INFRA_RETRIES covers a *completed* run that wrote a score.json which is
# nonetheless INVALID — deadline_exceeded, zero-work spawn race (turns==0 &&
# tokens==0), or api_error-with-no-work. Such a phantom score must not be
# trusted as "done": it pollutes the per-model /38 denominator. We re-run the
# cell up to INFRA_RETRIES times (deleting the stale score so score_exists no
# longer short-circuits); if still invalid, the cell is QUARANTINED — logged,
# not counted as success, never retried again.
INFRA_RETRIES = 2

# SLOW_SCENARIOS: per-scenario wall-clock override map (seconds). Some
# benchmarks legitimately need a longer wall_clock budget than the default
# baked into their Agentfile (e.g. sql-injection-search at 1800s instead of
# 600s). Extend this dict to add more.
#
# DISCOVERY (→2062, Task A.7): `ostk bench` has NO per-invocation wall-clock /
# deadline / budget flag (only --list/--cargo-only/--model/--verbose/--docker/
# --score/--arm/--all/--local/--driver/--keep). The wall_clock budget is set
# PER-BENCHMARK in benchmarks/<scenario>/Agentfile via `LIMIT wall_clock N`.
# So run_matrix cannot push a bumped wall-clock through the CLI today.
# We plumb the override as far as run_matrix allows: (a) we raise the python
# subprocess WALL_TIMEOUT_SEC so we don't kill a slow cell early, and (b) we
# pass BENCH_WALL_CLOCK_OVERRIDE in the child env as a hook for a future
# bench.rs flag.  TODO(lead): add a `--wall-clock <SEC>` flag to `ostk bench`
# (haystack bench.rs) that overrides the Agentfile `LIMIT wall_clock`, then
# wire it into ARM_FLAGS / run_cell below where BENCH_WALL_CLOCK_OVERRIDE is
# set. Until then the in-Agentfile LIMIT governs the actual model deadline.
SLOW_SCENARIOS = {"sql-injection-search": 1800}


def is_valid_score(score: dict) -> bool:
    """Promote the consolidator's post-hoc validity predicate to RUN TIME.

    Returns False (INVALID / infra-failed) when the score.json is a phantom
    that must NOT be trusted as a completed cell:
      - deadline:  stop_reason contains 'deadline' (deadline_exceeded — the
                   model never finished; re-running at the same wall_clock is
                   waste, but the cell still must not be marked done).
      - zero-work: turns_to_fix==0 AND input+output tokens==0 AND stop_reason
                   is not 'pass' (a spawn race / api.error before turn 1 — the
                   agent never made a real API call). stop_reason=='pass'
                   (passive-verify, legitimately 0 turns) is NOT zero-work.
      - api_error: stop_reason=='api_error' AND turns_to_fix==0 (auth / credit-
                   cap / 4xx-5xx upstream before any work).

    Returns True for a genuine capability fail (resolved=false WITH turns>0):
    that IS a valid, comparable outcome and counts as a loss, not infra.
    """
    stop = (score.get("stop_reason", "") or "").lower()
    turns = score.get("turns_to_fix", 0) or 0
    in_tok = score.get("input_tokens", 0) or 0
    out_tok = score.get("output_tokens", 0) or 0
    if "deadline" in stop:
        return False
    if "stall" in stop:
        # stall_watch killed a no-progress run — infra, not a model verdict.
        return False
    if turns == 0 and (in_tok + out_tok) == 0 and stop != "pass":
        return False
    if stop == "api_error" and turns == 0:
        return False
    return True


def read_score(model: str, bench: str, arm: str) -> dict | None:
    """Load the score.json for a cell (or None if absent / unparseable)."""
    p = score_path(model, bench, arm)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def score_valid(model: str, bench: str, arm: str) -> bool:
    """True iff a score.json exists AND passes is_valid_score."""
    sc = read_score(model, bench, arm)
    return sc is not None and is_valid_score(sc)

# F7: cheap-retry-on-fail sampling. First attempt always runs. If resolved=false,
# run up to MAX_SAMPLES total (cost-aware: a clean-on-first-try cell incurs 1
# run, only the noisy ones pay for triplication).
MAX_SAMPLES = 3

TRANSIENT_MARKERS = [
    "rate limit", "rate-limit", " 429 ", " 503 ", " 504 ", " 502 ",
    "connection reset", "timed out",
    "upstream error", "temporarily unavailable",
]

def classify_failure(output: str, returncode: int) -> str:
    """Return 'transient_fail' or 'failed'.

    Deadline-exceeded cells are NOT transient — the model just didn't finish.
    Re-running them at the same wall_clock_s is pure waste; classify as failed
    so the inner retry loop doesn't burn 3× the cost on a cell that needs
    longer wall_clock or a different model, not a retry.

    F1 (haystack bench.rs): credit-cap / auth / rate-limit / 4xx-5xx upstream
    failures now surface as `stop=api_error` in the bench summary line. These
    are NOT transient — retrying with the same key hits the same wall. Mark
    failed; only transient network noise (rate-limit bursts, 5xx, EOF) gets
    retried via TRANSIENT_MARKERS below.
    """
    lo = output.lower()
    # Exit 3 = model refusal (API safety classifier) with no valid score
    # landed. Deterministic and terminal — retrying at the same key/prompt
    # hits the same wall — so it is never transient, and it gets its own
    # status so a refusal-without-score is legible in the punch list.
    if returncode == 3:
        return "refused"
    if "poll deadline exceeded" in lo or "deadline_exceeded" in lo:
        return "failed"
    if "stop=api_error" in lo:
        return "failed"
    if any(m in lo for m in TRANSIENT_MARKERS):
        return "transient_fail"
    return "failed"

_HOST_BENCH_BINARY: dict | None = None


def host_bench_binary() -> dict:
    """Binary-identity receipt for stall_watch-synthesized scores, matching
    the shape ostk bench stamps on clean exits. Without it a watchdog-scored
    cell publishes receipt-less and cannot be attributed in a multi-binary
    date window (see consolidate_scores.snapshot_of)."""
    global _HOST_BENCH_BINARY
    if _HOST_BENCH_BINARY is None:
        path, _sha = resolve_host_ostk()
        try:
            out = subprocess.run([str(path), "--version"], capture_output=True,
                                 text=True, timeout=30).stdout.strip()
            ver = out.split()[-1] if out else "unknown"
        except (subprocess.TimeoutExpired, OSError):
            ver = "unknown"
        _HOST_BENCH_BINARY = {"version": ver, "build_sha": "unknown",
                              "path": str(path)}
    return _HOST_BENCH_BINARY


def run_cell(model: str, bench: str, arm: str, dry_run: bool) -> tuple[str, str]:
    """Invoke ostk bench under stall_watch (merciless stall detection for
    EVERY arm — journal-completion detector on kernel arms, no-progress
    watchdog + hard deadline on all arms). Return (status, tail_output).

    A run counts as "success" ONLY if the child completed AND a score.json
    exists AND that score passes is_valid_score. A completed run that writes
    an INVALID score (deadline / stall / zero-work / api_error-no-work) is
    classified as 'infra_fail' so the cell_action retry/quarantine path can
    handle it rather than silently locking in a phantom.

    stall_watch outcomes map as:
      exit           — child exited by itself: gate on returncode + validity.
      completed_kill — terminal evidence (score.json landed, or the kernel
                       journal showed a terminal row) but teardown hung past
                       grace: the process tree + container were SIGKILLed and
                       the cell is scored (bench's own score stamped
                       teardown_masked, or synthesized from the salvaged
                       journal). Gated on validity like a clean exit.
      stall_kill /   — no progress past the escalation ladder / hard deadline:
      deadline_kill    the cell was written as an INVALID score with
                       stop_reason 'stall' and the matrix CONTINUES
                       ('infra_fail' → retry within budget, then quarantine).

    BENCH_WALL_CLOCK_OVERRIDE stays exported for SLOW_SCENARIOS (hook for a
    future `ostk bench --wall-clock` flag; the Agentfile LIMIT still governs
    the in-container model deadline until that lands)."""
    cmd = [str(resolve_host_ostk()[0]), "bench", bench, "--model", model,
           *ARM_FLAGS[arm], "--docker"]
    if dry_run:
        print(f"    DRY-RUN: {' '.join(cmd)}")
        return "success", ""

    child_env = os.environ.copy()
    if bench in SLOW_SCENARIOS:
        child_env["BENCH_WALL_CLOCK_OVERRIDE"] = str(SLOW_SCENARIOS[bench])

    start = time.time()
    try:
        result = stall_watch.watch_cell(
            cmd, model=model, bench=bench, arm=arm,
            score_path=score_path(model, bench, arm),
            runs_root=RUNS,
            deadline_s=cell_deadline_s(bench),
            cwd=ROOT, env=child_env,
            difficulty_tier=bench_difficulty_tier(bench),
            bench_binary=host_bench_binary(),
        )
    except Exception as e:
        return "failed", f"exception: {e}"
    elapsed = time.time() - start

    # Native-arm billed-usage capture: enrich the freshly-written score with
    # the vendor CLI's own telemetry (opencode/kimi/vibe) BEFORE the validity
    # gate, so zero-billed cells become real-accounting cells at run time.
    if arm == "native" and score_exists(model, bench, arm):
        try:
            did, note = native_usage.enrich_score_file(score_path(model, bench, arm))
            if did:
                print(f"    [native-usage] {note}")
        except Exception as e:  # capture must never fail a run
            print(f"    [native-usage] WARN: capture failed: {e}", file=sys.stderr)

    if result.status in ("stall_kill", "deadline_kill"):
        # Cell already written as INVALID reason 'stall'; matrix continues.
        return "infra_fail", f"stall_watch: {result.detail} ({elapsed:.0f}s)"

    # A self-exit is 'completed' regardless of returncode: ostk bench exits
    # nonzero for a completed-but-unsolved scenario ("error: N scenario(s)
    # failed"), and the validity gate below is what decides success. Crash
    # exits that left no fresh valid score still fall through to
    # classify_failure; invalid scores route to infra_fail as before.
    # (Pre-v7.7.2 this was masked: unsolved kernel cells always hung at
    # teardown and took the completed_kill path instead of clean exit 1.)
    completed = result.status in ("completed_kill", "exit")
    masked_note = " [teardown_masked]" if result.teardown_masked else ""
    # Exit 3 = the model's safety classifier refused (stop_reason=refusal,
    # ostk bench >= 7.7.3). A refusal still lands a VALID score
    # (resolved=false, real accounting), so the cell stays a comparable
    # outcome ('success'); the marker just makes the state row and run log
    # distinguish it from a plain capability loss. The score fallback covers
    # the completed_kill path, where returncode is the kill signal.
    refused = result.returncode == 3 or (
        (read_score(model, bench, arm) or {}).get("stop_reason", "") == "refusal")
    refusal_note = " [refusal]" if refused else ""
    if completed and score_exists(model, bench, arm):
        if score_valid(model, bench, arm):
            return "success", f"({elapsed:.0f}s){masked_note}{refusal_note}"
        sc = read_score(model, bench, arm) or {}
        reason = (sc.get("stop_reason", "") or "").lower() or "invalid"
        return "infra_fail", f"invalid score (stop={reason}) ({elapsed:.0f}s){masked_note}"
    if result.status == "completed_kill":
        # killed after terminal evidence but no trustworthy score landed
        return "infra_fail", f"stall_watch: {result.detail} ({elapsed:.0f}s)"
    return classify_failure(result.tail, result.returncode or 1), result.tail

def cell_action(model: str, bench: str, arm: str, state: dict, retry_failed: bool,
                dry_run: bool = False) -> str:
    """Return 'skip', 'run', 'retry', or 'quarantine'.

    Completeness gate (→2062 / WS2): an EXISTING score.json no longer
    short-circuits to 'skip' unconditionally. We read the score and run it
    through is_valid_score:
      - VALID score   -> 'skip' (genuinely done — pass or a real capability
                          fail with turns>0).
      - INVALID score -> phantom (deadline / zero-work / api_error-no-work).
                         If infra_attempts < INFRA_RETRIES: delete the stale
                         score (so score_exists no longer short-circuits) and
                         return 'retry'. If the infra budget is exhausted:
                         return 'quarantine'.

    dry_run=True makes this read-only: we never delete a stale score during
    planning, so a --dry-run can be re-invoked without mutating runs/.
    """
    prior = state.get((model, bench, arm))
    infra_attempts = (prior or {}).get("infra_attempts", 0)

    if score_exists(model, bench, arm):
        if score_valid(model, bench, arm):
            return "skip"
        # Phantom / infra-failed score sitting on disk. Decide retry vs quarantine.
        if infra_attempts < INFRA_RETRIES:
            # Force a real re-run: score_exists short-circuits, so the stale
            # score MUST be removed or run_cell would never fire. (The run loop
            # also removes it just before re-running, so dry-run can skip it.)
            if not dry_run:
                try:
                    score_path(model, bench, arm).unlink()
                except FileNotFoundError:
                    pass
            return "retry"
        return "quarantine"

    if prior is None:
        return "run"
    status = prior.get("status")
    if status == "success":
        # State says success but the score file was cleared (e.g., re-run
        # after a binary update). Trust the filesystem: re-run.
        return "run"
    if status == "infra_fail":
        # A prior attempt produced an invalid score we already deleted (no file
        # on disk now). Retry until the infra budget is spent, then quarantine.
        if infra_attempts < INFRA_RETRIES:
            return "retry"
        return "quarantine"
    if status == "transient_fail":
        attempts = prior.get("attempts", 1)
        if attempts < MAX_RETRIES:
            return "retry"
        if retry_failed:
            return "retry"
        return "skip"
    if status == "failed":
        if retry_failed:
            return "retry"
        return "skip"
    if status == "refused":
        # A refusal is deterministic at the same key/prompt — sticky like a
        # hard fail; --retry-failed reopens it deliberately.
        if retry_failed:
            return "retry"
        return "skip"
    return "run"

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------- main ----------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontier", action="store_true",
                    help="use the curated FRONTIER_2026 roster (10 models, Control + B) verbatim, "
                         "bypassing existing/new category + native-driver gating")
    ap.add_argument("--only-new", action="store_true", help="skip existing models, run only new additions")
    ap.add_argument("--model", action="append", default=[], help="restrict to model(s); repeatable")
    ap.add_argument("--arm", action="append", default=[], help="restrict to arm(s); repeatable")
    ap.add_argument("--bench", action="append", default=[], help="restrict to bench(es); repeatable")
    ap.add_argument("--retry-failed", action="store_true", help="retry hard-failed cells too")
    ap.add_argument("--dry-run", action="store_true", help="print cells that would run; don't execute")
    ap.add_argument("--ostk-version-gate", default=None, help="require ostk --version contains this (e.g. 4.1.0)")
    ap.add_argument("--samples", type=int, default=MAX_SAMPLES,
                    help=f"F7 cheap-retry-on-fail: max samples per cell (default {MAX_SAMPLES}). "
                         "First attempt always runs; subsequent attempts only fire if the previous one "
                         "produced resolved=false. Pass --samples 1 to disable.")
    ap.add_argument("--no-resample", action="store_true",
                    help="alias for --samples 1 (single attempt, no F7 re-sampling)")
    args = ap.parse_args()
    samples_max = 1 if args.no_resample else max(1, args.samples)

    # host harness gate: resolve + hash-verify the explicit host ostk (never
    # bare PATH) BEFORE planning, so a missing/corrupt pin fails here and not
    # mid-matrix. resolve_host_ostk raises SystemExit fail-closed.
    host_bin, host_sha = resolve_host_ostk()
    print(f"[gate] host ostk: {host_bin} (sha256={host_sha[:16]}…)")

    # version gate (runs the RESOLVED harness, not PATH `ostk` — a version
    # string alone cannot discriminate same-version stale builds, hence the
    # content-hash gate above; this remains a cheap sanity label)
    if args.ostk_version_gate:
        v = subprocess.run([str(host_bin), "--version"], capture_output=True, text=True).stdout.strip()
        if args.ostk_version_gate not in v:
            print(f"ERROR: ostk version '{v}' does not contain '{args.ostk_version_gate}'", file=sys.stderr)
            return 1
        print(f"[gate] ostk version OK: {v}")

    bench_list = load_benchmarks()
    if args.bench:
        bench_list = [b for b in bench_list if b in args.bench]
    if not bench_list:
        print("no benchmarks selected", file=sys.stderr)
        return 1

    models = roster(frontier=args.frontier)
    if args.only_new:
        models = [m for m in models if m.category == "new"]
    if args.model:
        models = [m for m in models if m.name in args.model]
    if not models:
        print("no models selected", file=sys.stderr)
        return 1

    state = load_state()

    # per-model success / quarantine tallies for the final completeness summary.
    per_model_ok: dict[str, int] = {m.name: 0 for m in models}
    per_model_quarantined: dict[str, int] = {m.name: 0 for m in models}

    total = pending = skipped = succeeded = failed_hard = failed_trans = quarantined = 0
    refused_total = 0
    plan_quarantine = 0
    for m in models:
        arms = [a for a in m.arms if not args.arm or a in args.arm]
        for bench in bench_list:
            for arm in arms:
                total += 1
                # Planning pass is read-only: never delete a stale score here.
                action = cell_action(m.name, bench, arm, state, args.retry_failed,
                                     dry_run=True)
                if action == "skip":
                    skipped += 1
                    continue
                if action == "quarantine":
                    plan_quarantine += 1
                    continue
                pending += 1

    print(f"[plan] models={len(models)} benches={len(bench_list)}")
    print(f"[plan] total cells={total} pending={pending} skipped={skipped} quarantine={plan_quarantine}")
    if args.dry_run and pending == 0:
        print("[plan] nothing to run")
        return 0

    print(f"[run] starting at {now_iso()}")
    for m in models:
        arms = [a for a in m.arms if not args.arm or a in args.arm]
        print(f"\n========== {m.name} ({m.category}, arms={arms}) ==========")
        for bench in bench_list:
            for arm in arms:
                action = cell_action(m.name, bench, arm, state, args.retry_failed,
                                     dry_run=args.dry_run)
                if action == "skip":
                    continue
                if action == "quarantine":
                    # Infra budget exhausted before this run even started —
                    # log + skip. NOT counted as success, NOT retried.
                    prior = state.get((m.name, bench, arm), {})
                    sc = read_score(m.name, bench, arm) or {}
                    stop = (sc.get("stop_reason", "") or "").lower() or \
                           (prior.get("status", "") or "unknown")
                    if not args.dry_run:
                        append_quarantine(
                            m.name, bench, arm, stop,
                            f"infra budget exhausted ({INFRA_RETRIES} retries)")
                    quarantined += 1
                    per_model_quarantined[m.name] = per_model_quarantined.get(m.name, 0) + 1
                    print(f"  [QUARANTINE] {m.name} / {bench} / {arm}  (stop={stop})")
                    continue

                # Carry forward the per-cell infra_attempts counter across runs.
                prior = state.get((m.name, bench, arm), {})
                infra_attempts = prior.get("infra_attempts", 0)
                attempt_num = 1
                if action == "retry":
                    attempt_num = prior.get("attempts", 1) + 1

                # F7 outer loop: up to samples_max attempts driven by
                # resolved=false (in addition to the inner transient retry).
                # First sample always runs; later samples only fire if the
                # previous score.json reported resolved=false.
                final_status = "skipped"
                quarantine_now = False
                for sample_idx in range(1, samples_max + 1):
                    cell_succeeded = False
                    for backoff_idx in range(attempt_num - 1, MAX_RETRIES + 1):
                        if backoff_idx > 0:
                            sleep_s = BACKOFF_SEC[min(backoff_idx - 1, len(BACKOFF_SEC)-1)]
                            print(f"  [retry] sleeping {sleep_s}s before attempt {backoff_idx+1}")
                            time.sleep(sleep_s)
                        print(f"  [{now_iso()}] {m.name} / {bench} / {arm}  (sample {sample_idx}/{samples_max}, attempt {backoff_idx+1})")
                        status, tail = run_cell(m.name, bench, arm, args.dry_run)
                        # An invalid score (infra_fail) consumes the infra
                        # budget, not the transient budget.
                        if status == "infra_fail":
                            infra_attempts += 1
                        row = {
                            "ts": now_iso(),
                            "model": m.name, "bench": bench, "arm": arm,
                            "status": status,
                            "attempts": backoff_idx + 1,
                            "infra_attempts": infra_attempts,
                            "sample": sample_idx,
                            "tail": tail,
                        }
                        if not args.dry_run:
                            append_state(row)
                        state[(m.name, bench, arm)] = row
                        final_status = status
                        if status == "success":
                            cell_succeeded = True
                            print(f"    OK {tail}")
                            break
                        if status in ("failed", "refused"):
                            label = "REFUSED" if status == "refused" else "HARD FAIL"
                            print(f"    {label}: {tail[:200]}")
                            break
                        if status == "infra_fail":
                            # Phantom/invalid score. run_cell already left no
                            # valid score on disk; cell_action deleted any stale
                            # one. Retry within the infra budget, else quarantine.
                            if infra_attempts < INFRA_RETRIES:
                                print(f"    INFRA (invalid score, will retry "
                                      f"{infra_attempts}/{INFRA_RETRIES}): {tail[:200]}")
                                # ensure no stale score short-circuits the re-run
                                try:
                                    score_path(m.name, bench, arm).unlink()
                                except FileNotFoundError:
                                    pass
                                continue
                            print(f"    INFRA (budget exhausted "
                                  f"{infra_attempts}/{INFRA_RETRIES}): {tail[:200]}")
                            quarantine_now = True
                            break
                        if status == "transient_fail":
                            if backoff_idx + 1 >= MAX_RETRIES + 1:
                                print(f"    TRANSIENT (gave up after {backoff_idx+1}): {tail[:200]}")
                                break
                            else:
                                print(f"    TRANSIENT (will retry): {tail[:200]}")
                                continue
                    if quarantine_now:
                        break
                    # F7: snapshot this sample, decide whether to resample
                    if cell_succeeded and not args.dry_run:
                        append_sample(m.name, bench, arm, sample_idx)
                        resolved = read_score_resolved(m.name, bench, arm)
                        # A sample-1 solve is trusted at n=1 (uniform across
                        # all eras). But once resampling has begun, the cell
                        # is scored by strict majority over ALL samples taken
                        # (consolidate_scores folds the sidecar), so a mid-
                        # stream solve must NOT stop early: [F,T] would lock
                        # in 1/2 = miss without ever running the deciding
                        # third sample. Run the full budget.
                        if resolved and sample_idx == 1:
                            print(f"    [F7] sample 1 resolved=true; canonical score preserved")
                            break
                        if sample_idx < samples_max:
                            print(f"    [F7] sample {sample_idx} resolved={str(bool(resolved)).lower()}; resampling ({sample_idx+1}/{samples_max})")
                            attempt_num = 1  # reset transient-retry budget for the next sample
                            time.sleep(5)
                            continue
                        else:
                            print(f"    [F7] {samples_max} samples taken; strict majority decides")
                            break
                    else:
                        # Hard/transient/infra failure — don't burn extra samples
                        break
                # tally the cell once after F7 outer loop concludes
                if quarantine_now:
                    sc = read_score(m.name, bench, arm) or {}
                    stop = (sc.get("stop_reason", "") or "").lower() or "invalid"
                    if not args.dry_run:
                        append_quarantine(
                            m.name, bench, arm, stop,
                            f"infra budget exhausted ({INFRA_RETRIES} retries)")
                    quarantined += 1
                    per_model_quarantined[m.name] = per_model_quarantined.get(m.name, 0) + 1
                    print(f"    [QUARANTINE] {m.name} / {bench} / {arm}  (stop={stop})")
                elif final_status == "success":
                    succeeded += 1
                    per_model_ok[m.name] = per_model_ok.get(m.name, 0) + 1
                elif final_status == "failed":
                    failed_hard += 1
                elif final_status == "refused":
                    # Model verdict, not infra failure: counted in its own
                    # bucket and kept OUT of the nonzero-exit condition.
                    refused_total += 1
                elif final_status == "transient_fail":
                    failed_trans += 1
                elif final_status == "infra_fail":
                    # ran out of samples while still infra-invalid but budget not
                    # yet spent — count as transient give-up (will retry on next
                    # invocation since the cell is not quarantined).
                    failed_trans += 1

    print(f"\n========== summary @ {now_iso()} ==========")
    print(f"  succeeded:   {succeeded}")
    print(f"  hard fail:   {failed_hard}")
    print(f"  refused:     {refused_total}")
    print(f"  gave up:     {failed_trans}")
    print(f"  quarantined: {quarantined}")
    print(f"  skipped:     {skipped}")
    print(f"  total:       {total}")

    # Per-model completeness: cells succeeded (valid score) vs quarantined
    # (infra-budget exhausted, NOT counted toward the /38 denominator).
    print(f"\n========== per-model completeness ==========")
    for m in models:
        ok = per_model_ok.get(m.name, 0)
        q = per_model_quarantined.get(m.name, 0)
        flag = "  <-- QUARANTINED CELLS" if q else ""
        print(f"  {m.name:<26} succeeded={ok:<4} quarantined={q}{flag}")
    if quarantined and QUARANTINE.exists():
        print(f"  (quarantine detail: {QUARANTINE})")

    # punch list
    if failed_hard or failed_trans:
        print(f"\n========== punch list ==========")
        for k, v in state.items():
            if v.get("status") in ("failed", "transient_fail"):
                if v.get("status") == "transient_fail" and v.get("attempts", 0) <= MAX_RETRIES:
                    continue  # still retryable
                print(f"  {v['status']:16s} {k[0]} / {k[1]} / {k[2]}")

    # auto-consolidate on full success (skip in dry-run)
    if not args.dry_run and failed_hard == 0 and failed_trans == 0 and (ROOT / "consolidate_scores.py").exists():
        print("\n[consolidate] running consolidate_scores.py")
        subprocess.run(["python3", "consolidate_scores.py"], cwd=ROOT)

    return 0 if failed_hard == 0 else 2

if __name__ == "__main__":
    sys.exit(main())
