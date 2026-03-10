#!/usr/bin/env python3
"""needle-bench agent runner.

Parses an Agentfile, builds the Docker image, runs a mini-swe-agent DefaultAgent
against the benchmark, and saves the trajectory.

Usage:
    python run_needle_bench.py --model anthropic/claude-haiku-3-5-20241022 --benchmark off-by-one-pagination
    python run_needle_bench.py --model anthropic/claude-haiku-3-5-20241022 --all
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Suppress mini-swe-agent startup banner
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["MSWEA_COST_TRACKING"] = "ignore_errors"

from minisweagent.agents.default import DefaultAgent  # noqa: E402
from minisweagent.environments.docker import DockerEnvironment  # noqa: E402
from minisweagent.models.litellm_model import LitellmModel  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("needle-bench")

BENCH_DIR = Path(__file__).resolve().parent / "benchmarks"
RUNS_DIR = Path(__file__).resolve().parent / "runs"

DEFAULT_SYSTEM_TEMPLATE = (
    "You are debugging a broken application. "
    "Run test.sh to see what's failing. Find and fix the bug."
)

DEFAULT_INSTANCE_TEMPLATE = (
    "Run ./test.sh to see the failing test. "
    "Diagnose the root cause and fix it. "
    "The test should pass when you're done."
)

# Rough cost per token (USD) for deriving cost_limit from token limit.
# Conservative high estimate so the agent isn't cut short by cost before tokens.
COST_PER_TOKEN_USD = 20.0 / 1_000_000  # $20 per 1M tokens (generous ceiling)


@dataclass
class AgentfileConfig:
    """Parsed representation of a benchmark Agentfile."""

    image_name: str = ""
    tools: list[str] = field(default_factory=list)
    turn_limit: int = 30
    token_limit: int = 0
    wall_clock_limit: int = 0
    prompt: str = ""

    @property
    def cost_limit(self) -> float:
        """Derive a dollar cost limit from the token limit."""
        if self.token_limit > 0:
            return self.token_limit * COST_PER_TOKEN_USD
        return 10.0  # sensible default


def parse_agentfile(path: Path) -> AgentfileConfig:
    """Parse an Agentfile into an AgentfileConfig."""
    config = AgentfileConfig()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        directive = parts[0].upper()
        value = parts[1] if len(parts) > 1 else ""

        if directive == "FROM":
            # Strip optional :tag suffix for image naming
            config.image_name = value.split(":")[0]
        elif directive == "TOOL":
            config.tools.append(value)
        elif directive == "LIMIT":
            limit_parts = value.split(None, 1)
            if len(limit_parts) == 2:
                limit_type, limit_val = limit_parts
                limit_type = limit_type.lower()
                if limit_type == "turns":
                    config.turn_limit = int(limit_val)
                elif limit_type == "tokens":
                    config.token_limit = int(limit_val)
                elif limit_type == "wall_clock":
                    config.wall_clock_limit = int(limit_val)
        elif directive == "PROMPT":
            config.prompt = value
    return config


def build_docker_image(benchmark_name: str) -> str:
    """Build the Docker image for a benchmark. Returns the image tag."""
    benchmark_dir = BENCH_DIR / benchmark_name
    image_tag = f"needle-bench-{benchmark_name}"
    logger.info(f"Building Docker image: {image_tag} from {benchmark_dir}")
    result = subprocess.run(
        ["docker", "build", "-t", image_tag, str(benchmark_dir)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        logger.error(f"Docker build failed:\n{result.stderr}")
        raise RuntimeError(f"Docker build failed for {benchmark_name}: {result.stderr}")
    logger.info(f"Docker image built: {image_tag}")
    return image_tag


def run_benchmark(model_name: str, benchmark_name: str) -> Path:
    """Run a single benchmark and return the path to the saved trajectory."""
    benchmark_dir = BENCH_DIR / benchmark_name
    agentfile_path = benchmark_dir / "Agentfile"

    if not agentfile_path.exists():
        raise FileNotFoundError(f"No Agentfile at {agentfile_path}")

    # Parse Agentfile
    af = parse_agentfile(agentfile_path)
    logger.info(
        f"Benchmark: {benchmark_name} | turns={af.turn_limit} "
        f"tokens={af.token_limit} wall_clock={af.wall_clock_limit}s"
    )

    # Build Docker image
    image_tag = build_docker_image(benchmark_name)

    # Determine output path
    # Sanitize model name for filesystem (anthropic/claude-haiku -> anthropic_claude-haiku)
    model_slug = model_name.replace("/", "_")
    output_dir = RUNS_DIR / model_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{benchmark_name}.json"

    # Choose templates
    system_template = DEFAULT_SYSTEM_TEMPLATE
    instance_template = af.prompt if af.prompt else DEFAULT_INSTANCE_TEMPLATE

    # Create model
    model = LitellmModel(model_name=model_name)

    # Create environment
    env = DockerEnvironment(
        image=image_tag,
        cwd="/app",
        timeout=60,
    )

    try:
        # Create and run agent
        agent = DefaultAgent(
            model=model,
            env=env,
            system_template=system_template,
            instance_template=instance_template,
            step_limit=af.turn_limit,
            cost_limit=af.cost_limit,
            token_limit=af.token_limit,
            output_path=output_path,
        )

        logger.info(f"Starting agent run: {model_name} on {benchmark_name}")
        start_time = time.time()
        result = agent.run()
        elapsed = time.time() - start_time

        # Save additional metadata into the trajectory
        trajectory = json.loads(output_path.read_text())
        trajectory["needle_bench"] = {
            "benchmark": benchmark_name,
            "model": model_name,
            "agentfile": {
                "turn_limit": af.turn_limit,
                "token_limit": af.token_limit,
                "wall_clock_limit": af.wall_clock_limit,
                "has_prompt": bool(af.prompt),
                "tools": af.tools,
            },
            "wall_clock": elapsed,
            "exit_status": result.get("exit_status", "unknown"),
        }
        output_path.write_text(json.dumps(trajectory, indent=2))

        logger.info(
            f"Run complete: {benchmark_name} | "
            f"exit={result.get('exit_status', 'unknown')} | "
            f"wall_clock={elapsed:.1f}s | "
            f"trajectory={output_path}"
        )
    finally:
        env.cleanup()

    return output_path


def list_benchmarks() -> list[str]:
    """List all available benchmark names (directories with an Agentfile)."""
    benchmarks = []
    for d in sorted(BENCH_DIR.iterdir()):
        if d.is_dir() and (d / "Agentfile").exists() and not d.name.startswith("_"):
            benchmarks.append(d.name)
    return benchmarks


def main():
    parser = argparse.ArgumentParser(description="needle-bench agent runner")
    parser.add_argument(
        "--model",
        required=True,
        help="LiteLLM model name, e.g. anthropic/claude-haiku-3-5-20241022",
    )
    parser.add_argument(
        "--benchmark",
        help="Benchmark name (directory under benchmarks/)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all benchmarks",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available benchmarks and exit",
    )
    args = parser.parse_args()

    if args.list:
        for name in list_benchmarks():
            print(name)
        return

    if not args.all and not args.benchmark:
        parser.error("Provide --benchmark or --all")

    benchmarks = list_benchmarks() if args.all else [args.benchmark]

    for bm in benchmarks:
        if not (BENCH_DIR / bm).is_dir():
            logger.error(f"Benchmark not found: {bm}")
            sys.exit(1)

    results = []
    for bm in benchmarks:
        try:
            path = run_benchmark(args.model, bm)
            results.append({"benchmark": bm, "trajectory": str(path), "status": "ok"})
        except Exception as e:
            logger.error(f"Failed: {bm}: {e}", exc_info=True)
            results.append({"benchmark": bm, "status": "error", "error": str(e)})

    # Summary
    print("\n--- needle-bench results ---")
    for r in results:
        status = r["status"]
        marker = "PASS" if status == "ok" else "FAIL"
        print(f"  [{marker}] {r['benchmark']}", end="")
        if status == "ok":
            print(f"  -> {r['trajectory']}")
        else:
            print(f"  ({r['error']})")


if __name__ == "__main__":
    main()
