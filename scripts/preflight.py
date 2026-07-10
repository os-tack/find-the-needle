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
  2b. LIVE PROBES— for each provider an arm will route through, make the FREE
                   authenticated models-list call and verify (a) the key is
                   VALID and (b) the EXACT model id EXISTS in the provider's
                   catalog. On a miss, fail closed printing the closest
                   matches — a dead key or a wrong-model-name-for-provider
                   can no longer survive preflight. Keys are resolved env →
                   .env (repo, then haystack) → `ostk secret env` and are
                   NEVER printed (source name only). Network-but-free.
                   Skipped by --offline; --dry-run implies --offline.
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
        # v7.7.4+: resolve_provider routes gpt-*/o-series to the native
        # OpenAI Responses driver UNCONDITIONALLY (OPENROUTER_API_KEY is
        # not consulted). Require the native key on every kernel arm —
        # same fail-closed stance as gemini/mistral below.
        return ["OPENAI_API_KEY"], (
            "native OpenAI Responses CpuDriver (api.openai.com/v1/responses; "
            "unconditional since v7.7.4 — OpenRouter never substitutes)"
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
    if m.startswith("deepseek-"):
        # v7.7.6+: bare deepseek-* routes to the DeepSeek own endpoint
        # UNCONDITIONALLY on every kernel arm (fail-closed — a silent
        # OpenRouter fallback would compare a third-party serving stack
        # against the native arm's own-endpoint opencode).
        return ["DEEPSEEK_API_KEY"], (
            "DeepSeek own endpoint (api.deepseek.com, generic OpenAI-compat "
            "client, pinned bare wire id; unconditional since v7.7.6)"
        )
    # kimi- / moonshot- / qwen* / grok- / meta-llama / org-slash
    return ["OPENROUTER_API_KEY"], "generic OpenRouter kernel"


def key_present(alternatives: list[str]) -> str | None:
    for var in alternatives:
        if os.environ.get(var):
            return var
    return None


# ---------------------------------------------------------------------------
# 2b. live provider probes (free authenticated models-list calls)
# ---------------------------------------------------------------------------
# Key resolution order mirrors what the run itself can reach: process env
# first (what `ostk bench` children inherit), then the repo/haystack .env
# files, then the ostk secret store (`ostk secret env` — bench.rs
# resolve_secret path). Values are NEVER printed or logged; only the source.
ENV_FILES = [ROOT / ".env", Path.home() / "projects/haystack/.env"]

_KEY_CACHE: dict[str, tuple[str, str] | None] = {}


def resolve_key(var: str) -> tuple[str, str] | None:
    """Return (value, source) for an API key var, or None. Sources:
    'env' | '<path>/.env' | 'ostk-secret'. Value must never be printed."""
    if var in _KEY_CACHE:
        return _KEY_CACHE[var]
    result = None
    if os.environ.get(var):
        result = (os.environ[var], "env")
    if result is None:
        for env_fp in ENV_FILES:
            if not env_fp.is_file():
                continue
            try:
                for line in env_fp.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[len("export "):]
                    if line.startswith(f"{var}=") and len(line) > len(var) + 1:
                        val = line.split("=", 1)[1].strip().strip("'\"")
                        if val:
                            result = (val, str(env_fp))
                            break
            except OSError:
                continue
            if result:
                break
    if result is None and shutil.which("ostk"):
        try:
            proc = subprocess.run(
                ["ostk", "secret", "env", var], capture_output=True,
                text=True, timeout=15, cwd=ROOT)
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.startswith(f"export {var}=") and len(line) > len(var) + 8:
                    val = line.split("=", 1)[1]
                    if val:
                        result = (val, "ostk-secret")
                        break
        except (subprocess.TimeoutExpired, OSError):
            pass
    _KEY_CACHE[var] = result
    return result


def format_openrouter_model(model: str) -> str:
    """OpenRouter provider/model id for a roster model — python mirror of
    haystack cpu/model_registry.rs format_openrouter (keep in lockstep)."""
    if "/" in model:
        return model
    if model.startswith("claude-"):
        # claude-haiku-4-5 -> anthropic/claude-haiku-4.5 (version dots)
        parts = model.split("-")
        if len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
            model = "-".join(parts[:-2]) + f"-{parts[-2]}.{parts[-1]}"
        return f"anthropic/{model}"
    if model.startswith("gemini-"):
        return f"google/{model}"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return f"openai/{model}"
    if model.startswith("grok-"):
        return f"x-ai/{model}"
    if model.startswith("codestral-"):
        return f"mistralai/{model}"
    if model.startswith("devstral-"):
        return f"mistralai/{model.removesuffix('-latest')}"
    if model.startswith("mistral-"):
        return f"mistralai/{model}"
    if model.startswith(("kimi-", "moonshot-")):
        return f"moonshotai/{model}"
    if model.startswith("deepseek-"):
        return f"deepseek/{model}"
    if model.startswith("qwen"):
        return f"qwen/{model}"
    if model.startswith("llama-"):
        return f"meta-llama/{model}"
    return model


# provider -> (key env vars [any-of], models-list URL, auth style)
PROBE_PROVIDERS = {
    "anthropic":  (["ANTHROPIC_API_KEY"],
                   "https://api.anthropic.com/v1/models?limit=1000", "anthropic"),
    "google":     (["GEMINI_API_KEY", "GOOGLE_API_KEY"],
                   "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                   "goog-header"),
    "openai":     (["OPENAI_API_KEY"], "https://api.openai.com/v1/models", "bearer"),
    "mistral":    (["MISTRAL_API_KEY"], "https://api.mistral.ai/v1/models", "bearer"),
    "xai":        (["XAI_API_KEY"], "https://api.x.ai/v1/models", "bearer"),
    "deepseek":   (["DEEPSEEK_API_KEY"], "https://api.deepseek.com/models", "bearer"),
    "moonshot":   (["KIMI_API_KEY", "MOONSHOT_API_KEY"],
                   "https://api.moonshot.ai/v1/models", "bearer"),
    "openrouter": (["OPENROUTER_API_KEY"],
                   "https://openrouter.ai/api/v1/models", "bearer"),
}


def probe_plan(model: str, arm: str) -> list[tuple[str, str]]:
    """(provider, exact-model-id) alternatives for one (model, arm) cell —
    first alternative whose key resolves gets probed. Mirrors the real
    routing: bench.rs detect_native_harness for native, providers.rs
    resolve_provider + format_openrouter for kernel arms. Empty list = no
    probe applicable (e.g. local mlx)."""
    m = model.lower()
    if arm == "native":
        if m.startswith("claude-"):
            return [("anthropic", model)]
        if m.startswith("gemini-"):
            return [("google", model)]
        if m.startswith(("gpt-", "o1", "o3", "o4")):
            return [("openai", model)]
        if m.startswith(MISTRAL_PREFIXES):
            return [("mistral", model)]
        if m.startswith(("kimi-", "moonshot-")):
            return [("moonshot", model)]
        if m.startswith("grok-"):
            return [("xai", model)]
        if m.startswith("deepseek-"):
            return [("deepseek", model)]
        return [("openrouter", format_openrouter_model(model))]
    if m.startswith("mlx/") or arm == "kernel-mlx":
        return []
    if arm == "kernel-cpu":
        if m.startswith("claude"):
            return [("anthropic", model)]
        if m.startswith("gemini"):
            return [("google", model)]
        if m.startswith(MISTRAL_PREFIXES):
            return [("mistral", model)]
        if m.startswith(("gpt", "o1", "o3")):
            # v7.7.4+: native OpenAI Responses driver, unconditional.
            return [("openai", model)]
        if m.startswith("deepseek-"):
            # v7.7.6+: DeepSeek own endpoint, unconditional.
            return [("deepseek", model)]
        return [("openrouter", format_openrouter_model(model))]
    # plain kernel: OpenRouter (wire_model) — EXCEPT gpt-*/o-series (native
    # OpenAI driver since v7.7.4) and bare deepseek-* (own endpoint since
    # v7.7.6), which resolve_provider routes unconditionally.
    if m.startswith(("gpt", "o1", "o3")):
        return [("openai", model)]
    if m.startswith("deepseek-"):
        return [("deepseek", model)]
    or_model = model.removeprefix("openrouter/") if m.startswith("openrouter/") \
        else format_openrouter_model(model)
    return [("openrouter", or_model)]


def _http_get_json(url: str, headers: dict) -> tuple[int, dict | None]:
    """GET url -> (status, parsed json | None). Never raises; never logs
    headers (they carry the key). On an HTTP error the parsed error body is
    returned when available (providers put the failure reason there — e.g.
    Google's API_KEY_INVALID rides an HTTP 400, not a 401)."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


_CATALOG_CACHE: dict[str, tuple[set | None, str]] = {}


def fetch_model_catalog(provider: str, key: str) -> tuple[set | None, str]:
    """All model ids the provider's FREE models-list endpoint reports for this
    key. Returns (ids, note); ids=None means the probe itself failed (dead
    key / network) — note says why, redacted."""
    if provider in _CATALOG_CACHE:
        return _CATALOG_CACHE[provider]
    _, url, auth = PROBE_PROVIDERS[provider]
    headers = {"User-Agent": "needle-bench-preflight"}
    if auth == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif auth == "goog-header":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"

    ids: set[str] = set()
    status, data = _http_get_json(url, headers)
    # Google signals a dead key as HTTP 400 INVALID_ARGUMENT/API_KEY_INVALID
    # (not 401/403) — read the error body so a dead key is reported as a dead
    # key, not as "unreachable". First live exercise 2026-07-09 hit exactly
    # this: a stale GEMINI_API_KEY in .env while the keychain held the live one.
    err_reasons = ""
    if status != 200 and isinstance(data, dict):
        err = data.get("error") or {}
        err_reasons = " ".join(
            [str(err.get("status", ""))] +
            [str(d.get("reason", "")) for d in err.get("details", [])
             if isinstance(d, dict)])
    if status in (401, 403) or "API_KEY_INVALID" in err_reasons:
        result = (None, f"key REJECTED by {provider} (HTTP {status}"
                        f"{', API_KEY_INVALID' if 'API_KEY_INVALID' in err_reasons else ''}"
                        f") — dead/expired key")
    elif status != 200 or data is None:
        result = (None, f"{provider} models-list unreachable (HTTP {status or 'network error'})")
    else:
        if provider == "google":
            while True:
                for mrow in data.get("models", []):
                    name = str(mrow.get("name", ""))
                    ids.add(name.removeprefix("models/"))
                token = data.get("nextPageToken")
                if not token:
                    break
                status, data = _http_get_json(f"{url}&pageToken={token}", headers)
                if status != 200 or data is None:
                    break
        else:
            for mrow in data.get("data", []) or []:
                if isinstance(mrow, dict) and mrow.get("id"):
                    ids.add(str(mrow["id"]))
        result = (ids, f"{len(ids)} models visible")
    _CATALOG_CACHE[provider] = result
    return result


def closest_models(model_id: str, ids: set) -> list[str]:
    """Closest catalog ids for a fail-closed miss message."""
    import difflib
    pool = sorted(ids)
    hits = difflib.get_close_matches(model_id, pool, n=5, cutoff=0.4)
    # provider/model catalogs (openrouter): also try matching the bare suffix
    if "/" not in model_id:
        suffix_hits = [i for i in pool if i.rsplit("/", 1)[-1] == model_id]
        hits = suffix_hits + [h for h in hits if h not in suffix_hits]
    return hits[:5]


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
    ships into bench containers; the ostk-host-* pin (the harness that writes
    score.json) is gated separately in section 4b via the same resolver
    run_matrix uses (run_matrix_v41.resolve_host_ostk)."""
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
                           local_path: Path | None, local_sha: str | None,
                           host_path: Path | None = None,
                           host_sha: str | None = None) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "arms": arms,
        "frozen_musl_sha256": pinned_sha,
        "local_musl_path": str(local_path) if local_path else None,
        "local_musl_sha256": local_sha,
        "local_matches_pin": bool(pinned_sha and local_sha and pinned_sha == local_sha),
        # Host harness (the binary run_matrix invokes to orchestrate the cell
        # and write score.json) — a SECOND identity, gated in section 4b.
        "host_ostk_path": str(host_path) if host_path else None,
        "host_ostk_sha256": host_sha,
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
                         "environment problems reported as WOULD-FAIL, exit 0 "
                         "(implies --offline)")
    ap.add_argument("--offline", action="store_true",
                    help="skip the live provider probes (models-list key/model "
                         "verification). Structural + env checks still run.")
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
            # Not exported — can it at least be resolved? The child process
            # inherits ENV, so a .env/secret-store-only key still fails the
            # env check, but the message says exactly where it was found.
            resolvable = next(
                ((var, resolve_key(var)) for var in alternatives
                 if resolve_key(var)), None)
            if resolvable:
                var, (_val, source) = resolvable
                report(f"key/{arm}", False,
                       f"{var} found in {source} but NOT exported to the "
                       f"environment — the run inherits the shell env; "
                       f"export it (value redacted) ({note})", fatal=False)
            else:
                report(f"key/{arm}", False,
                       f"none of {alternatives} set ({note})", fatal=False)

    # 2b. live provider probes (free models-list; key + exact model id) --------
    offline = args.offline or args.dry_run
    if offline:
        print("  [preflight] probe      SKIP  (--offline: no network probes)")
    else:
        for arm in arms:
            plan = probe_plan(args.model, arm)
            if not plan:
                report(f"probe/{arm}", True, "no provider probe applicable (local)")
                continue
            # first alternative whose key resolves gets probed
            chosen = None
            for provider, model_id in plan:
                key_vars = PROBE_PROVIDERS[provider][0]
                resolved = next(
                    ((var, resolve_key(var)) for var in key_vars
                     if resolve_key(var)), None)
                if resolved:
                    chosen = (provider, model_id, resolved)
                    break
            if chosen is None:
                providers = [p for p, _ in plan]
                report(f"probe/{arm}", False,
                       f"no key resolvable for provider(s) {providers} "
                       f"(env, .env, ostk secret all empty) — cannot verify "
                       f"key or model id")
                continue
            provider, model_id, (var, (key_val, source)) = chosen
            ids, cat_note = fetch_model_catalog(provider, key_val)  # cached per provider
            if ids is None:
                report(f"probe/{arm}", False,
                       f"{provider}: {cat_note} [key: {var} from {source}]")
                continue
            if model_id in ids:
                report(f"probe/{arm}", True,
                       f"{provider}: key valid ({var} from {source}, "
                       f"{cat_note}); model '{model_id}' EXISTS")
            else:
                close = closest_models(model_id, ids)
                hint = f" — closest: {close}" if close else ""
                report(f"probe/{arm}", False,
                       f"{provider}: key valid but model '{model_id}' NOT in "
                       f"catalog ({cat_note}){hint}")

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

    # 4b. host bench harness -------------------------------------------------
    # The binary that ORCHESTRATES cells and WRITES score.json. The musl pin
    # above only covers the in-container kernel; the Jul-9 smoke ran the HOST
    # side stale (a Jul-2 dev build from PATH, predating the arm/binary
    # receipts and the --profile oneshot dispatch fix). run_matrix now
    # resolves this binary explicitly ($OSTK_BENCH_BIN override, else the
    # single hash-verified frozen-bin/ostk-host-* pin — never PATH); this
    # section runs the SAME resolver and gates on it.
    host_bin = host_sha = None
    try:
        host_bin, host_sha = run_matrix_v41.resolve_host_ostk()
    except SystemExit as e:
        report("host-bin", False, str(e).removeprefix("ERROR: "), fatal=False)
    if host_bin is not None:
        host_src = ("OSTK_BENCH_BIN override" if os.environ.get("OSTK_BENCH_BIN")
                    else "frozen-bin pin")
        # ce5b0ff9's write_score embeds the receipt keys as literals in the
        # binary; a harness without them predates the arm-receipt schema and
        # writes receipt-less scores (`--version` cannot discriminate: the
        # stale Jul-2 build also reported 7.7.1 — hence hash + marker gates).
        if b"requested_arm" not in host_bin.read_bytes():
            report("host-bin", False,
                   f"{host_bin} ({host_src}, sha256={host_sha[:16]}…) predates "
                   f"the arm-receipt schema (no 'requested_arm' literal) — "
                   f"scores it writes carry no requested_arm/executed_arm/"
                   f"bench_binary receipts; rebuild + re-pin from haystack "
                   f">= ce5b0ff9", fatal=False)
        else:
            report("host-bin", True,
                   f"{host_bin.name} ({host_src}, sha256={host_sha[:16]}…) "
                   f"carries arm receipts")
        path_ostk = shutil.which("ostk")
        if path_ostk and sha256_file(Path(path_ostk)) != host_sha:
            print(f"  [preflight] host-bin   NOTE  PATH ostk ({path_ostk}) differs "
                  f"from the resolved harness — run_matrix ignores PATH by design")

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

    record_binary_identity(args.model, arms, pinned_sha, local, local_sha,
                           host_bin, host_sha)
    print(f"\npreflight OK — binary identity recorded to {BINARY_IDENTITY_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
