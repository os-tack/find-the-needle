#!/usr/bin/env python3
"""
preflight.py — fail-closed preflight for a needle-bench run (used by bench.sh).

Checks, in order, BEFORE any container or API activity:
  0. pipeline    — consolidate_scores.py (+ cell_validity) imports clean, so
                   a broken edit (e.g. a `(TBD, TBD)` rate-card paste) fails
                   here — not at postflight, after the paid run. Fatal even
                   in --dry-run (structural, not environmental).
  1. roster      — model id resolves in run_matrix_v41's roster (standard or
                   --frontier); requested arms are a subset of the roster arms.
  2. api keys    — the provider env key(s) that the model's routing actually
                   uses (bench.rs detect_native_harness for the native arm,
                   cpu/providers.rs resolve_provider for kernel/kernel-cpu)
                   are present. "Any of" semantics where the kernel would
                   legitimately fall back to another provider.
  3. docker      — `docker info` succeeds (skipped in --dry-run: zero docker
                   activity during planning).
  4. binary      — the content-hash-pinned musl binary in frozen-bin/ exists
                   and its sha256 matches its filename (pin integrity), AND
                   the local musl binary that `ostk bench --local` will
                   actually docker-cp exists (a missing local binary silently
                   falls back to a network download inside the container —
                   bench.rs install_and_boot_kernel). Local-vs-pin drift is
                   reported loudly and RECORDED, not fatal (dev builds move
                   between pin updates by design).

On a real run the verified hashes are appended to runs/.binary_identity.jsonl
so every launch carries binary identity. Exit 0 = safe to run; exit 1 = fail
closed. --dry-run keeps structural checks fatal (unknown model / bad arm) but
reports environment checks informationally with WOULD-FAIL markers.

Stdlib only. Roster is imported from run_matrix_v41 — never duplicated here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_matrix_v41  # roster source of truth

FROZEN_BIN = ROOT / "frozen-bin"
RUNS = ROOT / "runs"
BINARY_IDENTITY_LOG = RUNS / ".binary_identity.jsonl"
# Mirrors haystack bench.rs local_linux_binary(): prefer the haystack source
# tree, then a target/ dir under this repo (works if ostk runs from haystack).
MUSL_SUFFIX = "target/x86_64-unknown-linux-musl/release/ostk"
LOCAL_BINARY_CANDIDATES = [
    Path.home() / "projects/haystack" / MUSL_SUFFIX,
    ROOT / MUSL_SUFFIX,
]

MISTRAL_PREFIXES = ("mistral-", "devstral-", "codestral-", "ministral-", "magistral-")


# ---------------------------------------------------------------------------
# 1. roster resolution
# ---------------------------------------------------------------------------
def resolve_model(model: str) -> tuple[list[str], bool] | None:
    """Return (roster_arms, frontier) for `model`, or None if not rostered.
    The standard roster wins; FRONTIER_2026 is consulted second (bench.sh
    passes --frontier to run_matrix_v41 when frontier=True)."""
    for spec in run_matrix_v41.roster(frontier=False):
        if spec.name == model:
            return list(spec.arms), False
    for spec in run_matrix_v41.roster(frontier=True):
        if spec.name == model:
            return list(spec.arms), True
    return None


# ---------------------------------------------------------------------------
# 2. provider key routing (mirrors bench.rs / providers.rs — see docstring)
# ---------------------------------------------------------------------------
def required_keys(model: str, arm: str) -> tuple[list[str], str]:
    """Return ([env var alternatives — any one suffices], routing note)."""
    m = model.lower()
    if arm == "native":
        if m.startswith("claude-"):
            return ["ANTHROPIC_API_KEY"], "claude-code CLI"
        if m.startswith("gemini-"):
            return ["GEMINI_API_KEY"], "gemini-cli"
        if m.startswith(("gpt-", "o1", "o3", "o4")):
            return ["OPENAI_API_KEY"], "codex CLI"
        if m.startswith(MISTRAL_PREFIXES):
            return ["MISTRAL_API_KEY"], "vibe CLI"
        if m.startswith(("kimi-", "moonshot-")):
            return ["KIMI_API_KEY"], "kimi CLI"
        if m.startswith("grok-"):
            return ["XAI_API_KEY"], "opencode fallback (xAI own-key)"
        if m.startswith("deepseek-"):
            return ["DEEPSEEK_API_KEY"], "opencode fallback (DeepSeek own-key)"
        return ["OPENROUTER_API_KEY"], "opencode fallback (OpenRouter)"

    # kernel / kernel-cpu / kernel-mlx — cpu/providers.rs resolve_provider
    if m.startswith("openrouter/"):
        return ["OPENROUTER_API_KEY"], "OpenRouter (forced by prefix)"
    if m.startswith("mlx/") or arm == "kernel-mlx":
        return [], "local mlx_lm.server (no provider key)"
    if m.startswith("claude"):
        return ["ANTHROPIC_API_KEY"], "Anthropic native CpuDriver"
    if m.startswith(("gpt", "o1", "o3")):
        return ["OPENROUTER_API_KEY", "OPENAI_API_KEY"], (
            "OpenRouter if OPENROUTER_API_KEY set, else api.openai.com "
            "passthrough (generic_kernel either way — no native OpenAI driver)"
        )
    if m.startswith("gemini"):
        if arm == "kernel-cpu":
            # FAIL CLOSED: without a Google key resolve_provider silently
            # degrades to OpenRouter — a generic execution masquerading as
            # the native-driver arm. Require the native key for kernel-cpu.
            return ["GEMINI_API_KEY", "GOOGLE_API_KEY"], (
                "Google native GeminiClient (OpenRouter fallback would "
                "silently change the arm's meaning — not accepted)"
            )
        return ["GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"], (
            "Gemini native if GEMINI/GOOGLE key, else OpenRouter"
        )
    if m.startswith(MISTRAL_PREFIXES):
        if arm == "kernel-cpu":
            return ["MISTRAL_API_KEY"], (
                "Mistral native driver (OpenRouter fallback would silently "
                "change the arm's meaning — not accepted)"
            )
        return ["MISTRAL_API_KEY", "OPENROUTER_API_KEY"], (
            "Mistral native if key, else OpenRouter"
        )
    # deepseek- / kimi- / moonshot- / qwen* / grok- / meta-llama / org-slash
    return ["OPENROUTER_API_KEY"], "generic OpenRouter kernel"


def key_present(alternatives: list[str]) -> str | None:
    for var in alternatives:
        if os.environ.get(var):
            return var
    return None


# ---------------------------------------------------------------------------
# 3/4. docker + pinned binary
# ---------------------------------------------------------------------------
def docker_reachable() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker CLI not on PATH"
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        return False, "docker info timed out (daemon unreachable?)"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        return False, tail[-1] if tail else f"docker info exit {proc.returncode}"
    return True, "docker daemon reachable"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_frozen_pin() -> tuple[Path | None, str | None, list[str]]:
    """Verify the content-hash-pinned musl binary in frozen-bin/.
    Returns (path, verified_sha256, problems). The musl binary is the one that
    ships into bench containers; ostk-host-* pins are informational."""
    problems: list[str] = []
    if not FROZEN_BIN.is_dir():
        return None, None, [f"{FROZEN_BIN} does not exist"]
    pins = sorted(FROZEN_BIN.glob("ostk-musl-*"))
    if not pins:
        return None, None, ["no ostk-musl-<sha256> pin in frozen-bin/"]
    if len(pins) > 1:
        problems.append(f"multiple musl pins present ({len(pins)}); using {pins[-1].name}")
    pin = pins[-1]
    claimed = pin.name.removeprefix("ostk-musl-")
    actual = sha256_file(pin)
    if actual != claimed:
        problems.append(
            f"PIN CORRUPT: {pin.name} content hash is {actual} (filename claims {claimed})"
        )
        return pin, None, problems
    return pin, actual, problems


def find_local_binary() -> Path | None:
    for cand in LOCAL_BINARY_CANDIDATES:
        if cand.is_file():
            return cand
    return None


def record_binary_identity(model: str, arms: list[str], pinned_sha: str | None,
                           local_path: Path | None, local_sha: str | None) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "arms": arms,
        "frozen_musl_sha256": pinned_sha,
        "local_musl_path": str(local_path) if local_path else None,
        "local_musl_sha256": local_sha,
        "local_matches_pin": bool(pinned_sha and local_sha and pinned_sha == local_sha),
    }
    with BINARY_IDENTITY_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("model", help="model id as it appears in the roster")
    ap.add_argument("--arms", default=None,
                    help="comma-separated arm subset (default: all roster arms)")
    ap.add_argument("--dry-run", action="store_true",
                    help="planning pass: no docker probe, no identity record; "
                         "environment problems reported as WOULD-FAIL, exit 0")
    ap.add_argument("--plan-out", default=None,
                    help="write the resolved plan JSON {model, frontier, arms} here")
    args = ap.parse_args()

    failures: list[str] = []
    would_fail: list[str] = []

    def report(tag: str, ok: bool, msg: str, fatal: bool = True) -> None:
        if ok:
            print(f"  [preflight] {tag:<10} OK    {msg}")
        elif args.dry_run and not fatal:
            print(f"  [preflight] {tag:<10} WOULD-FAIL  {msg}")
            would_fail.append(f"{tag}: {msg}")
        else:
            print(f"  [preflight] {tag:<10} FAIL  {msg}")
            failures.append(f"{tag}: {msg}")

    # 0. pipeline import sanity ------------------------------------------------
    # A broken consolidate_scores.py (e.g. a pasted PENDING rate-card line
    # still reading `(TBD, TBD)` — a NameError at import) otherwise first
    # surfaces at POSTFLIGHT, AFTER the paid run (bench.sh --dry-run never
    # imports it; validate.py does). Fail HERE, before any spend. Fatal even
    # in --dry-run: it is structural, not environmental.
    try:
        import consolidate_scores  # noqa: F401  (also pulls in cell_validity)
        report("pipeline", True, "consolidate_scores + cell_validity import clean")
    except Exception as e:  # NameError/SyntaxError/anything — fail closed
        report("pipeline", False,
               f"consolidate_scores import failed ({type(e).__name__}: {e}) — "
               f"postflight/board would break AFTER the paid run. If this is a "
               f"pending `(TBD, TBD)` rate-card paste: OMIT the RATE_CARD entry "
               f"until the price is announced (cost falls back to "
               f"self-reported/unpriced, never $0)")
        print(f"\npreflight FAILED ({len(failures)} problem[s]) — nothing was run.")
        return 1

    # 1. roster --------------------------------------------------------------
    resolved = resolve_model(args.model)
    if resolved is None:
        report("roster", False,
               f"model '{args.model}' not in run_matrix_v41 roster "
               f"(EXISTING_*/NEW_* lists or FRONTIER_2026) — add it there first, "
               f"see RUNBOOK.md 'Add a model in 3 steps'")
        print(f"\npreflight FAILED ({len(failures)} problem[s]) — nothing was run.")
        return 1
    roster_arms, frontier = resolved
    report("roster", True,
           f"{args.model} (roster={'FRONTIER_2026' if frontier else 'standard'}, "
           f"arms={roster_arms})")

    if args.arms:
        arms = [a.strip() for a in args.arms.split(",") if a.strip()]
        bad = [a for a in arms if a not in roster_arms]
        if bad:
            report("arms", False,
                   f"requested arm(s) {bad} not applicable to {args.model} "
                   f"(roster arms: {roster_arms})")
        else:
            report("arms", True, f"requested subset {arms}")
    else:
        arms = roster_arms
        report("arms", True, f"all roster arms {arms}")

    if failures:
        print(f"\npreflight FAILED ({len(failures)} problem[s]) — nothing was run.")
        return 1

    # 2. provider keys (offline env check) ------------------------------------
    for arm in arms:
        alternatives, note = required_keys(args.model, arm)
        if not alternatives:
            report(f"key/{arm}", True, f"none required ({note})")
            continue
        found = key_present(alternatives)
        if found:
            report(f"key/{arm}", True, f"{found} set ({note})")
        else:
            report(f"key/{arm}", False,
                   f"none of {alternatives} set ({note})", fatal=False)

    # 3. docker ----------------------------------------------------------------
    if args.dry_run:
        print("  [preflight] docker     SKIP  (--dry-run: zero docker activity)")
    else:
        ok, msg = docker_reachable()
        report("docker", ok, msg)

    # 4. pinned + local binary ---------------------------------------------------
    pin, pinned_sha, pin_problems = check_frozen_pin()
    for p in pin_problems:
        if p.startswith("multiple musl pins"):
            print(f"  [preflight] frozen-bin WARN  {p}")
        else:
            report("frozen-bin", False, p, fatal=False)
    if pinned_sha:
        report("frozen-bin", True, f"{pin.name} verifies (sha256={pinned_sha[:16]}…)")

    local = find_local_binary()
    local_sha = None
    if local is None:
        report("local-bin", False,
               f"musl binary absent at {LOCAL_BINARY_CANDIDATES[0]} — "
               f"`ostk bench --local` would silently curl-download inside the "
               f"container. Build it in haystack with `cargo build --release "
               f"--target x86_64-unknown-linux-musl` (linker "
               f"x86_64-linux-musl-gcc, per .cargo/config.toml) — NOT `make "
               f"install`, which installs the HOST debug build and never "
               f"touches the musl target", fatal=False)
    else:
        local_sha = sha256_file(local)
        report("local-bin", True, f"{local} (sha256={local_sha[:16]}…)")
        if pinned_sha and local_sha != pinned_sha:
            # Loud, recorded, non-fatal: dev builds drift from the pin between
            # releases by design. The identity log keeps this visible per run.
            print(f"  [preflight] local-bin  WARN  local binary does NOT match the "
                  f"frozen-bin pin\n"
                  f"                         pinned {pinned_sha}\n"
                  f"                         local  {local_sha}\n"
                  f"                         scores from this run are NOT "
                  f"pin-reproducible (real runs record both hashes in "
                  f"runs/{BINARY_IDENTITY_LOG.name})")

    # ---------------------------------------------------------------------------
    if args.plan_out:
        Path(args.plan_out).write_text(json.dumps(
            {"model": args.model, "frontier": frontier, "arms": arms}) + "\n")

    if failures:
        print(f"\npreflight FAILED ({len(failures)} problem[s]) — nothing was run.")
        return 1
    if args.dry_run:
        if would_fail:
            print(f"\npreflight (dry-run) OK to plan; {len(would_fail)} check(s) "
                  f"WOULD FAIL a real run:")
            for w in would_fail:
                print(f"    - {w}")
        else:
            print("\npreflight (dry-run) all checks green.")
        return 0

    record_binary_identity(args.model, arms, pinned_sha, local, local_sha)
    print(f"\npreflight OK — binary identity recorded to {BINARY_IDENTITY_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
