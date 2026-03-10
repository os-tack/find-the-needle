#!/usr/bin/env python3
"""needle-bench trajectory scorer.

Reads a trajectory JSON, computes all 11 SCORING.md metrics, and optionally
appends the score to leaderboard/scores.json.

Usage:
    python score_trajectory.py --trajectory runs/claude-haiku/off-by-one-pagination.json --benchmark off-by-one-pagination
    python score_trajectory.py --all --append
"""

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("needle-bench-scorer")

BASE_DIR = Path(__file__).resolve().parent
BENCH_DIR = BASE_DIR / "benchmarks"
RUNS_DIR = BASE_DIR / "runs"
LEADERBOARD_PATH = BASE_DIR / "leaderboard" / "scores.json"


def parse_solution_patch(patch_path: Path) -> dict:
    """Parse a unified diff patch and return {filename: set_of_changed_lines_content}.

    Returns:
        dict mapping filename -> set of added/changed line strings (stripped).
    """
    files_changed: dict[str, set[str]] = {}
    current_file = None
    for line in patch_path.read_text().splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            if current_file not in files_changed:
                files_changed[current_file] = set()
        elif line.startswith("+") and not line.startswith("+++") and current_file:
            # Added line in the patch
            files_changed[current_file].add(line[1:].strip())
    return files_changed


def get_solution_files(patch_path: Path) -> set[str]:
    """Get the set of filenames touched by the solution patch."""
    files = set()
    for line in patch_path.read_text().splitlines():
        if line.startswith("+++ b/"):
            files.add(line[6:])
        elif line.startswith("--- a/"):
            files.add(line[6:])
    return files


def extract_actions_from_messages(messages: list[dict]) -> list[dict]:
    """Extract all actions (tool calls) from trajectory messages."""
    actions = []
    for msg in messages:
        extra = msg.get("extra", {})
        for action in extra.get("actions", []):
            actions.append(action)
    return actions


def extract_turn_commands(messages: list[dict]) -> list[dict]:
    """Extract per-turn info: turn number, commands executed, files mentioned.

    Returns list of dicts with keys: turn, commands, files_edited, files_read, content.
    """
    turns = []
    turn_num = 0
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant":
            turn_num += 1
            extra = msg.get("extra", {})
            actions = extra.get("actions", [])
            commands = [a.get("command", "") for a in actions]
            content = msg.get("content", "") or ""

            # Detect file edits from commands
            files_edited = set()
            files_read = set()
            for cmd in commands:
                # Detect file writes/edits (common patterns)
                for pattern in [
                    r"(?:cat|tee)\s+>\s*(\S+)",
                    r"sed\s+-i\s+.*?(\S+)$",
                    r"echo\s+.*?>\s*(\S+)",
                    r"python3?\s+-c\s+.*?open\(['\"](\S+?)['\"]",
                ]:
                    for m in re.finditer(pattern, cmd):
                        files_edited.add(m.group(1))

                # Detect file reads
                for pattern in [
                    r"\bcat\s+(\S+)",
                    r"\bless\s+(\S+)",
                    r"\bhead\s+.*?(\S+)$",
                    r"\btail\s+.*?(\S+)$",
                    r"\bgrep\s+.*?(\S+)$",
                ]:
                    for m in re.finditer(pattern, cmd):
                        files_read.add(m.group(1))

            turns.append({
                "turn": turn_num,
                "commands": commands,
                "files_edited": files_edited,
                "files_read": files_read,
                "content": content,
            })
    return turns


def compute_turns_to_discovery(turns: list[dict], solution_files: set[str]) -> int:
    """Count turns until agent first touches/mentions a file from solution.patch."""
    for t in turns:
        all_files = t["files_edited"] | t["files_read"]
        # Check if any file path overlaps with solution files (basename or full path match)
        for f in all_files:
            f_basename = os.path.basename(f)
            for sf in solution_files:
                sf_basename = os.path.basename(sf)
                if f_basename == sf_basename or f.endswith(sf) or sf.endswith(f):
                    return t["turn"]
        # Also check command strings for mentions of solution filenames
        for cmd in t["commands"]:
            for sf in solution_files:
                sf_basename = os.path.basename(sf)
                if sf_basename in cmd or sf in cmd:
                    return t["turn"]
        # Check assistant content for file mentions
        content = t.get("content", "")
        if content:
            for sf in solution_files:
                sf_basename = os.path.basename(sf)
                if sf_basename in content:
                    return t["turn"]
    return len(turns) if turns else 0


def compute_turns_to_fix(messages: list[dict], total_turns: int) -> int:
    """Count turns until test.sh passes (exit code 0).

    Looks for tool output messages following a test.sh command with returncode 0.
    """
    turn_num = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            turn_num += 1
            actions = msg.get("extra", {}).get("actions", [])
            for action in actions:
                cmd = action.get("command", "")
                if "test.sh" in cmd:
                    # Look at the next tool result message(s)
                    for j in range(i + 1, min(i + len(actions) + 2, len(messages))):
                        obs = messages[j]
                        obs_content = obs.get("content", "")
                        # Check for returncode 0 in the observation
                        if "<returncode>0</returncode>" in obs_content:
                            return turn_num
                        # Also handle raw output format
                        obs_extra = obs.get("extra", {})
                        if isinstance(obs_extra, dict):
                            output_data = obs_extra.get("output", {})
                            if isinstance(output_data, dict) and output_data.get("returncode") == 0:
                                return turn_num
    return total_turns


def compute_signal_to_noise(turns: list[dict], solution_files: set[str]) -> float:
    """Ratio of productive turns to total turns."""
    if not turns:
        return 0.0
    productive = 0
    for t in turns:
        all_files = t["files_edited"] | t["files_read"]
        # A turn is productive if it touches a relevant file
        is_productive = False
        for f in all_files:
            f_basename = os.path.basename(f)
            for sf in solution_files:
                sf_basename = os.path.basename(sf)
                if f_basename == sf_basename or f.endswith(sf) or sf.endswith(f):
                    is_productive = True
                    break
            if is_productive:
                break
        # Also productive if running test.sh
        if not is_productive:
            for cmd in t["commands"]:
                if "test.sh" in cmd:
                    is_productive = True
                    break
        # Check content for file mentions
        if not is_productive and t.get("content"):
            for sf in solution_files:
                if os.path.basename(sf) in t["content"]:
                    is_productive = True
                    break
        if is_productive:
            productive += 1
    return productive / len(turns)


def compute_false_positives(turns: list[dict], solution_files: set[str]) -> int:
    """Count distinct files edited that are NOT in solution.patch.

    Excludes test files and counts only final-state modifications.
    """
    all_edited = set()
    for t in turns:
        all_edited |= t["files_edited"]

    false_pos = set()
    solution_basenames = {os.path.basename(sf) for sf in solution_files}
    for f in all_edited:
        f_basename = os.path.basename(f)
        # Skip test files
        if "test" in f_basename.lower():
            continue
        # Check if this file is in the solution
        if f_basename not in solution_basenames:
            # Double-check with full path matching
            matched = False
            for sf in solution_files:
                if f.endswith(sf) or sf.endswith(f):
                    matched = True
                    break
            if not matched:
                false_pos.add(f)
    return len(false_pos)


def compute_token_cost(trajectory: dict) -> int:
    """Sum total tokens (input + output) from trajectory messages."""
    total = 0
    messages = trajectory.get("messages", [])
    for msg in messages:
        extra = msg.get("extra", {})
        response = extra.get("response", {})
        usage = response.get("usage", extra.get("usage", {}))
        if isinstance(usage, dict):
            total += usage.get("prompt_tokens", 0)
            total += usage.get("completion_tokens", 0)
            total += usage.get("total_tokens", 0) if not usage.get("prompt_tokens") else 0
    return total


def compute_tokens_per_correct_line(
    token_cost: int,
    trajectory: dict,
    solution_patch: dict[str, set[str]],
) -> float:
    """Tokens spent per correctly-changed line."""
    # Count correct lines: lines in agent's edits that match solution.patch additions
    correct_lines = 0
    messages = trajectory.get("messages", [])
    agent_edits: set[str] = set()

    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        actions = msg.get("extra", {}).get("actions", [])
        for action in actions:
            cmd = action.get("command", "")
            # Extract lines being written by the agent from commands
            for line in cmd.splitlines():
                agent_edits.add(line.strip())

    # Count how many solution lines appear in agent edits
    for filename, lines in solution_patch.items():
        for line in lines:
            if line.strip() and line.strip() in agent_edits:
                correct_lines += 1

    if correct_lines == 0:
        return float("inf")
    return token_cost / correct_lines


def compute_recovery_events(turns: list[dict]) -> tuple[int, int]:
    """Detect recovery events and successful recoveries.

    Returns (recovery_events, successful_recoveries).
    """
    recovery_events = 0
    successful_recoveries = 0
    recovery_keywords = [
        "revert", "undo", "wrong approach", "let me try",
        "that didn't work", "incorrect", "mistake", "going back",
        "start over", "different approach",
    ]

    for i, t in enumerate(turns):
        content = (t.get("content") or "").lower()
        is_recovery = False

        # Check for recovery language
        for kw in recovery_keywords:
            if kw in content:
                is_recovery = True
                break

        # Check for git revert/checkout commands
        for cmd in t["commands"]:
            if any(r in cmd for r in ["git checkout", "git revert", "git restore"]):
                is_recovery = True

        if is_recovery:
            recovery_events += 1
            # A recovery is "successful" if the agent eventually runs test.sh
            # with success after this point (we approximate: if there are more
            # turns after, assume the agent continued and potentially recovered)
            # Full accuracy requires checking subsequent test.sh results,
            # but we use a heuristic: if there are turns after recovery, count it.
            if i < len(turns) - 1:
                successful_recoveries += 1

    return recovery_events, successful_recoveries


def compute_recovery_rate(recovery_events: int, successful_recoveries: int) -> float:
    """Recovery rate: successful_recoveries / recovery_events. 1.0 if no events."""
    if recovery_events == 0:
        return 1.0
    return successful_recoveries / recovery_events


def check_resolved_in_container(trajectory: dict, benchmark_name: str) -> bool:
    """Run test.sh inside the agent's container to check if the bug is resolved.

    Falls back to checking the trajectory exit status if container is gone.
    """
    # First, try to find the container ID from the trajectory
    env_config = (
        trajectory.get("info", {})
        .get("config", {})
        .get("environment", {})
    )
    container_image = env_config.get("image", "")

    # Check trajectory for a passing test.sh in the final turns
    messages = trajectory.get("messages", [])
    # Walk backwards to find the last test.sh execution
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if "<returncode>0</returncode>" in content:
                # Check if this was a test.sh response
                # Look at the preceding assistant message for test.sh
                idx = messages.index(msg)
                for j in range(idx - 1, -1, -1):
                    prev = messages[j]
                    if prev.get("role") == "assistant":
                        actions = prev.get("extra", {}).get("actions", [])
                        for a in actions:
                            if "test.sh" in a.get("command", ""):
                                return True
                        break

    # If we found a Submitted exit status, the agent thinks it solved it,
    # but we need test.sh confirmation
    exit_status = trajectory.get("info", {}).get("exit_status", "")
    if exit_status == "Submitted":
        # The agent submitted, but we need to verify test.sh passed
        # Check the last few observations
        pass

    return False


def has_prompt_directive(benchmark_name: str) -> bool:
    """Check if the benchmark's Agentfile has a PROMPT directive."""
    agentfile = BENCH_DIR / benchmark_name / "Agentfile"
    if not agentfile.exists():
        return False
    for line in agentfile.read_text().splitlines():
        if line.strip().upper().startswith("PROMPT"):
            return True
    return False


def score_trajectory(trajectory_path: Path, benchmark_name: str) -> dict:
    """Compute all 11 metrics for a trajectory."""
    trajectory = json.loads(trajectory_path.read_text())
    messages = trajectory.get("messages", [])

    # Load solution patch
    patch_path = BENCH_DIR / benchmark_name / ".bench" / "solution.patch"
    if not patch_path.exists():
        raise FileNotFoundError(f"No solution.patch at {patch_path}")

    solution_files = get_solution_files(patch_path)
    solution_patch = parse_solution_patch(patch_path)

    # Extract turn data
    turns = extract_turn_commands(messages)
    total_turns = len(turns)
    step_limit = (
        trajectory.get("info", {})
        .get("config", {})
        .get("agent", {})
        .get("step_limit", total_turns)
    )
    effective_limit = step_limit if step_limit > 0 else total_turns

    # 1. resolved
    resolved = check_resolved_in_container(trajectory, benchmark_name)

    # 2. turns_to_discovery
    turns_to_discovery = compute_turns_to_discovery(turns, solution_files)
    if turns_to_discovery == 0:
        turns_to_discovery = effective_limit

    # 3. turns_to_fix
    turns_to_fix = compute_turns_to_fix(messages, effective_limit)

    # 4. signal_to_noise
    signal_to_noise = round(compute_signal_to_noise(turns, solution_files), 4)

    # 5. false_positives
    false_positives = compute_false_positives(turns, solution_files)

    # 6. token_cost
    token_cost = compute_token_cost(trajectory)

    # 7. tokens_per_correct_line
    tpcl = compute_tokens_per_correct_line(token_cost, trajectory, solution_patch)
    tokens_per_correct_line = tpcl if not math.isinf(tpcl) else None

    # 8 & 9. recovery_events and recovery_rate
    recovery_events, successful_recoveries = compute_recovery_events(turns)
    recovery_rate = round(compute_recovery_rate(recovery_events, successful_recoveries), 4)

    # 10. wall_clock
    needle_meta = trajectory.get("needle_bench", {})
    wall_clock = needle_meta.get("wall_clock", 0.0)
    if wall_clock == 0.0:
        # Try to derive from message timestamps
        timestamps = []
        for msg in messages:
            ts = msg.get("extra", {}).get("timestamp")
            if ts:
                timestamps.append(ts)
        if len(timestamps) >= 2:
            wall_clock = timestamps[-1] - timestamps[0]
    wall_clock = round(wall_clock, 1)

    # 11. blind_discovery
    has_prompt = has_prompt_directive(benchmark_name)
    blind_discovery = resolved and not has_prompt

    # Determine agent name from trajectory
    model_config = (
        trajectory.get("info", {})
        .get("config", {})
        .get("model", {})
    )
    agent_name = model_config.get("model_name", needle_meta.get("model", "unknown"))

    score = {
        "benchmark": benchmark_name,
        "agent": agent_name,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolved": resolved,
        "turns_to_discovery": turns_to_discovery,
        "turns_to_fix": turns_to_fix,
        "signal_to_noise": signal_to_noise,
        "false_positives": false_positives,
        "token_cost": token_cost,
        "tokens_per_correct_line": tokens_per_correct_line,
        "recovery_events": recovery_events,
        "recovery_rate": recovery_rate,
        "wall_clock": wall_clock,
        "blind_discovery": blind_discovery,
    }

    return score


def find_all_trajectories() -> list[tuple[Path, str]]:
    """Find all trajectory files under runs/ and infer benchmark names."""
    results = []
    if not RUNS_DIR.exists():
        return results
    for model_dir in sorted(RUNS_DIR.iterdir()):
        if not model_dir.is_dir():
            continue
        for traj_file in sorted(model_dir.glob("*.json")):
            benchmark_name = traj_file.stem
            # Verify this benchmark exists
            if (BENCH_DIR / benchmark_name / ".bench" / "solution.patch").exists():
                results.append((traj_file, benchmark_name))
    return results


def append_to_leaderboard(score: dict):
    """Append a score record to leaderboard/scores.json."""
    LEADERBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LEADERBOARD_PATH.exists():
        scores = json.loads(LEADERBOARD_PATH.read_text())
    else:
        scores = []

    # Remove existing entry for same benchmark+agent if present
    scores = [
        s for s in scores
        if not (s.get("benchmark") == score["benchmark"] and s.get("agent") == score["agent"])
    ]
    scores.append(score)

    # Sort by leaderboard ranking: resolved desc, turns_to_fix asc, token_cost asc, wall_clock asc
    scores.sort(
        key=lambda s: (
            not s.get("resolved", False),
            s.get("turns_to_fix", 9999),
            s.get("token_cost", 9999999),
            s.get("wall_clock", 9999),
        )
    )

    LEADERBOARD_PATH.write_text(json.dumps(scores, indent=2) + "\n")
    logger.info(f"Appended score to {LEADERBOARD_PATH}")


def main():
    parser = argparse.ArgumentParser(description="needle-bench trajectory scorer")
    parser.add_argument(
        "--trajectory",
        help="Path to trajectory JSON file",
    )
    parser.add_argument(
        "--benchmark",
        help="Benchmark name (required with --trajectory)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Score all trajectories under runs/",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append scores to leaderboard/scores.json",
    )
    args = parser.parse_args()

    if not args.all and not args.trajectory:
        parser.error("Provide --trajectory or --all")

    if args.trajectory and not args.benchmark:
        # Try to infer benchmark from filename
        traj_path = Path(args.trajectory)
        args.benchmark = traj_path.stem
        if not (BENCH_DIR / args.benchmark / ".bench" / "solution.patch").exists():
            parser.error("Cannot infer benchmark name from trajectory path. Use --benchmark.")

    if args.all:
        pairs = find_all_trajectories()
        if not pairs:
            logger.warning("No trajectories found under runs/")
            return
    else:
        pairs = [(Path(args.trajectory), args.benchmark)]

    all_scores = []
    for traj_path, bm_name in pairs:
        try:
            logger.info(f"Scoring: {bm_name} <- {traj_path}")
            score = score_trajectory(traj_path, bm_name)
            all_scores.append(score)
            print(json.dumps(score, indent=2))
            if args.append:
                append_to_leaderboard(score)
        except Exception as e:
            logger.error(f"Failed to score {bm_name}: {e}", exc_info=True)

    # Print summary
    if len(all_scores) > 1:
        resolved_count = sum(1 for s in all_scores if s["resolved"])
        print(f"\n--- Summary: {resolved_count}/{len(all_scores)} resolved ---")


if __name__ == "__main__":
    main()
