#!/usr/bin/env python3
"""Weekly kernel-curated benchmark pipeline.

Pipeline 2: haystack imports a repo, diagnoses the most compounding issue,
creates a frozen Docker benchmark, models solve it, fix offered upstream.

Usage:
    python3 pipeline/weekly.py --repo django/django
    python3 pipeline/weekly.py --repo golang/go --branch main
    python3 pipeline/weekly.py --list-repos
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repos.json")
BENCHMARKS_DIR = os.path.join(PROJ_ROOT, "benchmarks")
DIFFICULTY_JSON = os.path.join(PROJ_ROOT, "difficulty.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_repos():
    """Load the curated repo list from repos.json."""
    with open(REPOS_JSON) as f:
        return json.load(f)["repos"]


def current_week_number():
    """ISO week number, used for deterministic rotation."""
    return datetime.date.today().isocalendar()[1]


def repo_for_week(week=None):
    """Return the repo entry for the given ISO week (defaults to this week)."""
    repos = load_repos()
    wk = week if week is not None else current_week_number()
    return repos[wk % len(repos)]


def slugify(text):
    """Turn 'org/repo' or free text into a filesystem-safe slug."""
    return text.lower().replace("/", "-").replace(" ", "-").replace("_", "-")


# ---------------------------------------------------------------------------
# Step 1: Import repo
# ---------------------------------------------------------------------------

def _resolve_fork(repo_entry):
    """Resolve the clone URL, preferring os-tack fork if upstream is specified.

    Repos with an 'upstream' field are forks managed under os-tack.
    The fork is synced before cloning to get the latest upstream state.
    The benchmark pins the fork at a specific commit for reproducibility.
    """
    org, repo = repo_entry["org"], repo_entry["repo"]
    upstream = repo_entry.get("upstream")

    if upstream:
        # This is an os-tack fork — sync it first, then clone the fork
        print(f"[fork] {org}/{repo} is a fork of {upstream}")
        # Sync fork with upstream (gh api handles this)
        try:
            subprocess.run(
                ["gh", "api", f"repos/{org}/{repo}/merge-upstream",
                 "-f", "branch=" + repo_entry.get("branch", "main"),
                 "--silent"],
                check=True, capture_output=True,
            )
            print(f"[fork] synced {org}/{repo} with {upstream}")
        except subprocess.CalledProcessError:
            print(f"[fork] sync failed or already up to date — continuing with current fork state")

    return f"https://github.com/{org}/{repo}.git"


def import_repo(org, repo, branch="main", *, upstream=None, dry_run=False):
    """Clone the repo (or fork) and run haystack diagnosis on it.

    Returns (repo_path, needles) where needles is a list of diagnosed issues
    ranked by compounding impact.

    If upstream is set, the repo is an os-tack fork. The fork is synced
    before cloning, and any fix PRs go to the fork first, then upstream.
    """
    clone_url = f"https://github.com/{org}/{repo}.git"
    tmp_dir = tempfile.mkdtemp(prefix=f"needle-bench-{org}-{repo}-")
    repo_path = os.path.join(tmp_dir, repo)

    source_label = f"{org}/{repo}" + (f" (fork of {upstream})" if upstream else "")
    print(f"[import] cloning {source_label} (branch={branch}) -> {repo_path}")

    if not dry_run:
        subprocess.run(
            ["git", "clone", "--depth=1", "--branch", branch, clone_url, repo_path],
            check=True,
        )
        # Pin the exact commit so benchmarks are reproducible
        commit = subprocess.check_output(
            ["git", "-C", repo_path, "rev-parse", "HEAD"], text=True
        ).strip()
        print(f"[import] pinned at {commit[:12]}")
    else:
        os.makedirs(repo_path, exist_ok=True)
        commit = "dry-run-0000000000"

    # --- haystack integration point ---
    # TODO: Run `haystack install --import` on the cloned repo to produce
    #       a compiled needle graph.  For now we stub the output.
    #
    # Expected flow:
    #   result = subprocess.run(
    #       ["haystack", "install", "--import", repo_path],
    #       capture_output=True, text=True, check=True,
    #   )
    #   needles = parse_haystack_output(result.stdout)
    #
    # Stub: return an empty list; the pipeline will exit gracefully.
    needles = _stub_needles(org, repo, commit)

    return repo_path, needles


def _stub_needles(org, repo, commit):
    """Placeholder until haystack --import produces real needles.

    Returns a list of dicts shaped like haystack needle output so the
    downstream steps have something to work with during development.
    """
    # TODO: Replace with real haystack output parsing
    return [
        {
            "id": f"{org}/{repo}#stub-001",
            "title": f"[stub] most compounding issue in {org}/{repo}",
            "priority": "P0",
            "impact": "high",
            "dependencies": 5,
            "file_hint": "src/core/unknown.py",
            "test_hint": "tests/test_core.py",
            "commit": commit,
            "description": (
                "This is a stub needle. Once haystack --import is wired up, "
                "this will contain the real diagnosis of the most compounding "
                "issue in the repository."
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Step 2: Select needle
# ---------------------------------------------------------------------------

def select_needle(needles):
    """Pick the highest-leverage needle from the diagnosed list.

    Selection criteria (in order):
      1. Priority (P0 > P1 > P2)
      2. Dependency count (more dependents = more compounding)
      3. Impact rating

    Returns the selected needle dict, or None if no actionable needle found.
    """
    if not needles:
        print("[select] no needles to choose from")
        return None

    def sort_key(n):
        # Lower priority number = higher urgency
        prio_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        prio = prio_map.get(n.get("priority", "P3"), 9)
        deps = n.get("dependencies", 0)
        impact_map = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        impact = impact_map.get(n.get("impact", "low"), 9)
        return (prio, -deps, impact)

    ranked = sorted(needles, key=sort_key)
    selected = ranked[0]

    # TODO: Validate the needle is actionable:
    #   - Has a test that would pass if the bug were fixed
    #   - The file_hint and test_hint exist in the repo
    #   - The issue is reproducible (test currently fails)
    #
    # For now, accept whatever is top-ranked.

    print(f"[select] chose: {selected['id']} — {selected['title']}")
    print(f"         priority={selected.get('priority')} deps={selected.get('dependencies')} impact={selected.get('impact')}")

    return selected


# ---------------------------------------------------------------------------
# Step 3: Create benchmark
# ---------------------------------------------------------------------------

def create_benchmark(needle, repo_path, output_dir=None, *, dry_run=False):
    """Create a frozen Docker benchmark from a needle and its source repo.

    Generates:
      benchmarks/<slug>/Dockerfile
      benchmarks/<slug>/Agentfile
      benchmarks/<slug>/test.sh
      benchmarks/<slug>/.bench/README.md
      benchmarks/<slug>/app/   (relevant source files)

    Also registers the benchmark in difficulty.json (default: medium).

    Returns the benchmark directory path.
    """
    needle_id = needle.get("id", "unknown")
    slug = slugify(needle_id.split("#")[-1]) if "#" in needle_id else slugify(needle_id)
    bench_dir = os.path.join(output_dir or BENCHMARKS_DIR, slug)

    if os.path.exists(bench_dir):
        print(f"[benchmark] {bench_dir} already exists, skipping")
        return bench_dir

    print(f"[benchmark] creating {slug} at {bench_dir}")

    if dry_run:
        print(f"[benchmark] (dry-run) would create {bench_dir}")
        return bench_dir

    os.makedirs(bench_dir, exist_ok=True)
    os.makedirs(os.path.join(bench_dir, ".bench"), exist_ok=True)
    os.makedirs(os.path.join(bench_dir, "app"), exist_ok=True)

    commit = needle.get("commit", "unknown")
    org_repo = needle_id.split("#")[0] if "#" in needle_id else "unknown/unknown"
    lang = _detect_lang(repo_path)

    # --- Dockerfile ---
    _write_dockerfile(bench_dir, lang, org_repo, commit)

    # --- Agentfile ---
    _write_agentfile(bench_dir)

    # --- test.sh ---
    _write_test_sh(bench_dir, needle)

    # --- .bench/README.md ---
    _write_bench_readme(bench_dir, needle, org_repo, commit)

    # --- .bench/solution.patch ---
    # TODO: Generate from the actual fix once haystack provides it.
    _write_solution_stub(bench_dir)

    # --- Copy relevant source files ---
    # TODO: Use needle.file_hint to copy only the relevant subtree.
    #       For now, leave the app/ dir with a placeholder.
    _write_app_placeholder(bench_dir, needle)

    # --- Register in difficulty.json ---
    _register_difficulty(slug, tier="medium")

    print(f"[benchmark] created {bench_dir}")
    return bench_dir


def _detect_lang(repo_path):
    """Best-effort language detection from repo contents."""
    indicators = {
        "python": ["setup.py", "pyproject.toml", "requirements.txt"],
        "go": ["go.mod", "go.sum"],
        "rust": ["Cargo.toml"],
        "javascript": ["package.json"],
    }
    for lang, files in indicators.items():
        for f in files:
            if os.path.exists(os.path.join(repo_path, f)):
                return lang
    return "unknown"


def _write_dockerfile(bench_dir, lang, org_repo, commit):
    """Generate a Dockerfile that builds the repo at the pinned commit."""
    # Base images by language
    base_images = {
        "python": "python:3.12-alpine",
        "go": "golang:1.22-alpine",
        "rust": "rust:1.77-alpine",
        "javascript": "node:20-alpine",
    }
    base = base_images.get(lang, "ubuntu:22.04")

    # TODO: Flesh out per-language build steps once we have real needles.
    #       The Dockerfile should reproduce the exact environment needed
    #       to trigger the bug and run the test.
    dockerfile = f"""\
FROM {base}

# Benchmark generated from {org_repo} @ {commit[:12]}
# Language: {lang}

RUN apk add --no-cache bash curl git 2>/dev/null || \\
    apt-get update && apt-get install -y bash curl git

RUN git config --global user.email "bench@needle-bench.cc" && \\
    git config --global user.name "needle-bench"

WORKDIR /workspace

# TODO: Replace with actual repo checkout + build steps.
#   git clone --depth=1 --branch <branch> https://github.com/{org_repo}.git .
#   git checkout {commit[:12]}
COPY app/ /workspace/app/
COPY test.sh /workspace/test.sh
RUN chmod +x test.sh

CMD ["bash"]
"""
    with open(os.path.join(bench_dir, "Dockerfile"), "w") as f:
        f.write(dockerfile)


def _write_agentfile(bench_dir):
    """Generate a standard Agentfile for the benchmark."""
    agentfile = """\
# Auto-generated by weekly pipeline
FROM needle-bench
TOOL shell
TOOL file:read
TOOL file:edit
LIMIT turns 30
LIMIT tokens 150000
LIMIT wall_clock 600
"""
    with open(os.path.join(bench_dir, "Agentfile"), "w") as f:
        f.write(agentfile)


def _write_test_sh(bench_dir, needle):
    """Generate test.sh from needle metadata."""
    test_hint = needle.get("test_hint", "")
    # TODO: Extract the actual failing test command from haystack output.
    #       For now, write a stub that always fails (benchmark is unsolved).
    test_sh = f"""\
#!/bin/sh
# Auto-generated by weekly pipeline
# Needle: {needle.get('id', 'unknown')}
# Test hint: {test_hint}
#
# TODO: Replace with the actual test command extracted from the repo.
#       This test should FAIL on the buggy code and PASS once fixed.

set -e

echo "FAIL: test not yet wired up for this benchmark"
exit 1
"""
    with open(os.path.join(bench_dir, "test.sh"), "w") as f:
        f.write(test_sh)
    os.chmod(os.path.join(bench_dir, "test.sh"), 0o755)


def _write_bench_readme(bench_dir, needle, org_repo, commit):
    """Generate .bench/README.md from needle metadata."""
    readme = f"""\
# {needle.get('title', 'Untitled')}

**Source:** [{org_repo}](https://github.com/{org_repo}) @ `{commit[:12]}`
**Priority:** {needle.get('priority', 'unknown')}
**Impact:** {needle.get('impact', 'unknown')}
**Dependencies:** {needle.get('dependencies', 'unknown')}
**Pipeline:** weekly/kernel-curated

## Description

{needle.get('description', 'No description available.')}

## Files

- **Bug location (hint):** `{needle.get('file_hint', 'unknown')}`
- **Test:** `{needle.get('test_hint', 'unknown')}`

## How to verify

```bash
docker build -t needle-bench-test .
docker run --rm needle-bench-test bash -c "cd /workspace && bash test.sh"
```

## Attribution

Discovered by the needle-bench weekly pipeline via haystack kernel diagnosis.
"""
    with open(os.path.join(bench_dir, ".bench", "README.md"), "w") as f:
        f.write(readme)


def _write_solution_stub(bench_dir):
    """Write a placeholder solution.patch."""
    patch = """\
# TODO: Generate from the actual fix once haystack provides it.
# This file should be a git-format patch that, when applied, makes test.sh pass.
"""
    with open(os.path.join(bench_dir, ".bench", "solution.patch"), "w") as f:
        f.write(patch)


def _write_app_placeholder(bench_dir, needle):
    """Write a placeholder file in app/ so Dockerfile COPY doesn't fail."""
    placeholder = f"""\
# Placeholder — weekly pipeline stub
# Needle: {needle.get('id', 'unknown')}
#
# TODO: Copy the relevant source files from the cloned repo here.
#       Use needle['file_hint'] to identify the subtree to include.
"""
    with open(os.path.join(bench_dir, "app", "README"), "w") as f:
        f.write(placeholder)


def _register_difficulty(slug, tier="medium"):
    """Add the benchmark to difficulty.json if not already present."""
    if not os.path.exists(DIFFICULTY_JSON):
        return

    with open(DIFFICULTY_JSON) as f:
        data = json.load(f)

    if slug not in data.get("benchmarks", {}):
        data["benchmarks"][slug] = tier
        with open(DIFFICULTY_JSON, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        print(f"[difficulty] registered {slug} as {tier}")
    else:
        print(f"[difficulty] {slug} already registered")


# ---------------------------------------------------------------------------
# Step 4: Offer fix upstream
# ---------------------------------------------------------------------------

def offer_fix(benchmark_path, solution_patch, org, repo, *, model="unknown", dry_run=False):
    """Open a PR upstream with the model's fix.

    This is a stub — called when a model solves the benchmark. The actual
    PR creation will use `gh pr create` with proper attribution.

    Args:
        benchmark_path: Path to the benchmark directory.
        solution_patch: Path to the .patch file with the fix.
        org: GitHub org (e.g. "django").
        repo: GitHub repo (e.g. "django").
        model: Model name that produced the fix.
        dry_run: If True, print what would happen without doing it.
    """
    bench_name = os.path.basename(benchmark_path)

    pr_title = f"Fix: {bench_name} (discovered by needle-bench)"
    pr_body = (
        f"Fix discovered by [needle-bench](https://needle-bench.cc) battery.\n\n"
        f"**Model:** {model}\n"
        f"**Benchmark:** {bench_name}\n\n"
        f"This fix was identified through automated diagnosis of compounding issues "
        f"in the codebase. The relevant test now passes.\n\n"
        f"---\n"
        f"*Automated PR — please review before merging.*"
    )

    if dry_run:
        print(f"[offer_fix] (dry-run) would create PR on {org}/{repo}:")
        print(f"  title: {pr_title}")
        print(f"  patch: {solution_patch}")
        return None

    # TODO: Implement the actual upstream PR flow:
    #
    # 1. Fork the repo (if not already forked)
    #    gh repo fork {org}/{repo} --clone=false
    #
    # 2. Clone the fork, create a branch, apply the patch
    #    git clone https://github.com/needle-bench/{repo}.git /tmp/fix-{bench_name}
    #    cd /tmp/fix-{bench_name}
    #    git checkout -b fix/{bench_name}
    #    git apply {solution_patch}
    #    git commit -m "{pr_title}"
    #    git push origin fix/{bench_name}
    #
    # 3. Open the PR
    #    gh pr create --repo {org}/{repo} --title "{pr_title}" --body "{pr_body}"
    #
    # 4. Return the PR URL

    print(f"[offer_fix] TODO: create PR on {org}/{repo} with fix from {solution_patch}")
    return None


# ---------------------------------------------------------------------------
# Step 5: Main
# ---------------------------------------------------------------------------

def run_pipeline(org, repo, branch="main", *, upstream=None, dry_run=False):
    """Execute the full weekly pipeline for a single repo."""
    print(f"\n{'='*60}")
    label = f"{org}/{repo}" + (f" (fork of {upstream})" if upstream else "")
    print(f"  Weekly Pipeline: {label} (branch={branch})")
    print(f"{'='*60}\n")

    # Step 0: Sync fork with upstream if applicable
    if upstream and not dry_run:
        _resolve_fork({"org": org, "repo": repo, "branch": branch, "upstream": upstream})

    # Step 1: Import and diagnose
    repo_path, needles = import_repo(org, repo, branch, upstream=upstream, dry_run=dry_run)
    print(f"[import] found {len(needles)} needle(s)\n")

    if not needles:
        print("[pipeline] no needles found — nothing to benchmark")
        return None

    # Step 2: Select the most impactful needle
    needle = select_needle(needles)
    if needle is None:
        print("[pipeline] no actionable needle selected")
        return None
    print()

    # Step 3: Create the benchmark
    bench_path = create_benchmark(needle, repo_path, dry_run=dry_run)
    print()

    # Step 4: (Future) When a model solves this benchmark, offer_fix() is
    #         called from the scoring pipeline — not during creation.
    print(f"[pipeline] benchmark ready for review: {bench_path}")
    print(f"[pipeline] after model solves it, run: offer_fix(benchmark, patch, '{org}', '{repo}')")

    # Cleanup temp clone
    if not dry_run and repo_path.startswith(tempfile.gettempdir()):
        shutil.rmtree(os.path.dirname(repo_path), ignore_errors=True)
        print(f"[cleanup] removed temp clone")

    return bench_path


def main():
    parser = argparse.ArgumentParser(
        description="Weekly kernel-curated benchmark pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python3 pipeline/weekly.py --repo django/django
  python3 pipeline/weekly.py --repo golang/go --branch master
  python3 pipeline/weekly.py --list-repos
  python3 pipeline/weekly.py --auto           # pick repo by week number
""",
    )
    parser.add_argument("--repo", help="GitHub repo as org/name (e.g. django/django)")
    parser.add_argument("--branch", help="Branch to clone (default: from repos.json or main)")
    parser.add_argument("--list-repos", action="store_true", help="List curated repos and exit")
    parser.add_argument("--auto", action="store_true", help="Auto-select repo by week number")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without cloning or creating files")
    args = parser.parse_args()

    # --list-repos
    if args.list_repos:
        repos = load_repos()
        week = current_week_number()
        print(f"Curated repos (week {week}, auto-select index: {week % len(repos)}):\n")
        for i, r in enumerate(repos):
            marker = " <-- this week" if i == week % len(repos) else ""
            fork_label = f" (fork of {r['upstream']})" if r.get("upstream") else ""
            print(f"  [{i}] {r['org']}/{r['repo']} ({r['lang']}, branch={r['branch']}){fork_label}{marker}")
        return

    # Determine which repo to run
    upstream = None
    if args.auto:
        entry = repo_for_week()
        org, repo, branch = entry["org"], entry["repo"], entry["branch"]
        upstream = entry.get("upstream")
        print(f"[auto] week {current_week_number()} -> {org}/{repo}")
    elif args.repo:
        parts = args.repo.split("/")
        if len(parts) != 2:
            print("ERROR: --repo must be org/name (e.g. django/django)", file=sys.stderr)
            sys.exit(1)
        org, repo = parts
        # Look up branch + upstream from repos.json, fall back to arg or "main"
        branch = args.branch
        for entry in load_repos():
            if entry["org"] == org and entry["repo"] == repo:
                branch = branch or entry["branch"]
                upstream = entry.get("upstream")
                break
        branch = branch or "main"
    else:
        parser.print_help()
        sys.exit(1)

    result = run_pipeline(org, repo, branch, upstream=upstream, dry_run=args.dry_run)

    if result:
        print(f"\nBenchmark created: {result}")
        print("Next: open a PR for human review, then add to the leaderboard.")
    else:
        print("\nNo benchmark created.")

    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
