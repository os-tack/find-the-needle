#!/usr/bin/env python3
"""Score haystack-boot trajectories from post.jsonl evidence.

Reads each model's post.jsonl, extracts tool calls, scores boot directive execution.
Verdict is computed from filesystem evidence in the trajectory, not from test.sh.
"""
import json, os, re, sys, glob

BOOT_DIRECTIVES = [
    ("read_bootloader", lambda cmds: any("boot.md" in c for c in cmds)),
    ("verify_primefile", lambda cmds: any("primefile" in c for c in cmds)),
    ("check_clock", lambda cmds: any(c in ("date", "date -u") or "clock" in c for c in cmds)),
    ("read_language", lambda cmds: any(".language" in c for c in cmds)),
    ("read_state", lambda cmds: any("audit" in c or "needle" in c or "counter" in c for c in cmds)),
    ("wrote_audit", lambda cmds: any("audit.jsonl" in c and ("echo" in c or ">>" in c or ">" in c) for c in cmds)),
    ("wrote_identity", lambda cmds: any("identity_counter" in c and ("echo" in c or ">" in c) for c in cmds)),
    ("wrote_entityfile", lambda cmds: any("ENTITYFILE" in c and ("echo" in c or ">" in c or "cat" in c) for c in cmds)),
    ("wrote_report", lambda cmds: any(("boot-report" in c or "status" in c or "report" in c) and ("echo" in c or ">" in c) for c in cmds)),
]

def score_trajectory(post_path):
    cmds = []
    turns = 0
    tokens = 0
    wall = 0

    for line in open(post_path):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "cmd" in e:
            cmds.append(e["cmd"])
        if e.get("event") == "post.end":
            turns = e.get("turns", 0)
            wall = e.get("wall_clock", 0) if "wall_clock" in e else 0

    results = {}
    for name, check in BOOT_DIRECTIVES:
        results[name] = check(cmds)

    read_score = sum(1 for k in ["read_bootloader", "verify_primefile", "read_language", "read_state"] if results.get(k))
    write_score = sum(1 for k in ["wrote_audit", "wrote_identity", "wrote_entityfile", "wrote_report"] if results.get(k))
    total = sum(1 for v in results.values() if v)

    if write_score >= 3:
        verdict = "BOOTS"
    elif write_score >= 1:
        verdict = "PARTIAL_BOOT"
    elif read_score >= 3:
        verdict = "READS_TACK"
    elif read_score >= 1:
        verdict = "EXPLORES"
    else:
        verdict = "NO_BOOT"

    return {
        "directives": results,
        "read_score": read_score,
        "write_score": write_score,
        "total_score": total,
        "total_possible": len(BOOT_DIRECTIVES),
        "verdict": verdict,
        "turns": turns,
        "tool_calls": len(cmds),
    }


def _normalize_model_key(raw):
    """Normalize model name for dedup: strip vendor prefix, lowercase, replace _ and . with -.

    Mirrors the leaderboard's normalizeForMatch() so that
    'anthropic/claude-opus-4.6' and 'claude-opus-4-6' collapse to the same key.
    """
    name = raw.split("/", 1)[1] if "/" in raw else raw
    return re.sub(r"[_.]", "-", name.lower())


def _is_better_boot(candidate, current):
    """Return True if candidate boot result should replace current.

    Preference: higher total_score, then lower tokens (cheaper).
    """
    if candidate["total_score"] != current["total_score"]:
        return candidate["total_score"] > current["total_score"]
    return candidate.get("tokens", 0) < current.get("tokens", 0)


def main():
    runs_dir = os.path.join(os.path.dirname(__file__), "runs")
    raw_results = []

    for post_path in sorted(glob.glob(os.path.join(runs_dir, "**/haystack-boot/post.jsonl"), recursive=True)):
        model = post_path.replace(runs_dir + "/", "").split("/haystack-boot")[0]

        # Also load score.json for tokens/wall
        score_path = post_path.replace("post.jsonl", "score.json")
        tokens = 0
        wall = 0.0
        if os.path.exists(score_path):
            with open(score_path) as f:
                sd = json.load(f)
                tokens = sd.get("token_cost", 0)
                wall = sd.get("wall_clock", 0.0)

        boot_score = score_trajectory(post_path)
        boot_score["model"] = model
        boot_score["tokens"] = tokens
        boot_score["wall_clock"] = wall
        raw_results.append(boot_score)

    # Deduplicate: when both vendor-prefixed and flat dirs exist for the
    # same logical model, keep the best result.  Prefer the vendor-prefixed
    # model name for display (it's the canonical OpenRouter identifier).
    best = {}
    for r in raw_results:
        key = _normalize_model_key(r["model"])
        if key not in best or _is_better_boot(r, best[key]):
            best[key] = r
        elif "/" in r["model"] and "/" not in best[key]["model"]:
            # Same score — prefer vendor-prefixed name for display
            if not _is_better_boot(best[key], r):
                best[key] = r

    results = list(best.values())

    # Sort: BOOTS first, then by total_score desc
    verdict_order = {"BOOTS": 0, "PARTIAL_BOOT": 1, "READS_TACK": 2, "EXPLORES": 3, "NO_BOOT": 4}
    results.sort(key=lambda r: (verdict_order.get(r["verdict"], 5), -r["total_score"]))

    # Print leaderboard
    print("haystack-boot leaderboard")
    print(f"{'model':<40} {'verdict':<15} {'read':>4} {'write':>5} {'total':>5} {'tokens':>8} {'wall':>6}")
    print("-" * 90)
    for r in results:
        print(f"{r['model']:<40} {r['verdict']:<15} {r['read_score']:>4} {r['write_score']:>5} {r['total_score']:>3}/{r['total_possible']} {r['tokens']:>8} {r['wall_clock']:>5.1f}s")

    # Also write JSON
    out_path = os.path.join(os.path.dirname(__file__), "runs", "haystack-boot-leaderboard.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n→ {out_path}")


if __name__ == "__main__":
    main()
