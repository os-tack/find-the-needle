# Contributing to needle-bench

Your worst debugging day is everyone's benchmark.

## Submitting a Benchmark

### What makes a good benchmark

- **Real bug.** Something you actually hit. Not synthetic, not contrived. The bug that cost you four hours at 2am.
- **Minimal reproduction.** Strip everything that isn't the bug and its context. The smaller the container, the better the benchmark.
- **Deterministic test.** `test.sh` must produce the same result every time. No flaky tests. No timing-dependent assertions.
- **Clean solution.** The patch in `.bench/solution.patch` should be the minimal fix. Not a refactor. Not a cleanup. Just the fix.

### Step by step

1. **Copy the template:**
   ```bash
   cp -r benchmarks/_template benchmarks/your-bug-name
   ```

2. **Name it well:**
   - Format: `<bug-type>-<project-or-domain>`
   - Examples: `off-by-one-redis`, `null-deref-tokio`, `race-condition-sqlite`
   - Lowercase, hyphenated, unique

3. **Write the Dockerfile:**
   - Start from a small base image (Alpine preferred)
   - Install dependencies, copy broken source code
   - Must build in < 5 minutes, produce < 500 MB image
   - Must NOT include hints toward the fix
   - Must NOT require network at test time

4. **Write the Agentfile:**
   ```
   FROM your-bug-name:latest
   TOOL sh_run
   TOOL ss
   LIMIT turns 30
   LIMIT tokens 200000
   LIMIT wall_clock 600
   ```
   - Add `PROMPT` only if the bug genuinely requires extra context
   - More tools = easier benchmark. Be deliberate.

5. **Write test.sh:**
   - Must be executable (`chmod +x test.sh`)
   - Must exit 0 when the bug is fixed, exit 1 when broken
   - Must complete in < 60 seconds
   - Must be deterministic
   - Should print useful output on failure

6. **Create the solution patch:**
   ```bash
   # Inside the container, fix the bug, then:
   git diff > .bench/solution.patch
   ```
   - Verify: applying the patch makes `test.sh` exit 0
   - Verify: without the patch, `test.sh` exits 1

7. **Write README.md:**
   - What the project does (one sentence)
   - What the bug is (without spoiling the location)
   - What symptoms the agent will see
   - Do NOT reveal the fix

8. **Validate locally:**
   ```bash
   make validate BENCH=your-bug-name
   ```

9. **Submit a PR:**
   - One benchmark per PR
   - CI will validate the structure automatically
   - A maintainer will review the difficulty and quality

### Validation checklist

Your PR will be checked for:

- [ ] `Dockerfile` exists and builds in < 5 min
- [ ] `Agentfile` exists with valid directives
- [ ] `test.sh` exists and is executable
- [ ] `.bench/solution.patch` exists and applies cleanly
- [ ] `test.sh` fails without patch, passes with patch
- [ ] `README.md` describes the bug without spoiling it
- [ ] Benchmark name follows the naming convention
- [ ] No network access required at test time

### Difficulty guidelines

| Difficulty | Turns expected | Description |
|-----------|---------------|-------------|
| Easy | 1–5 | Obvious from test output, one-file fix |
| Medium | 5–15 | Requires reading multiple files, understanding context |
| Hard | 15–30 | Subtle logic bug, requires deep domain knowledge |
| Expert | 30+ | Multi-file interaction, timing-dependent, architectural |

Label your benchmark's expected difficulty in the PR description.

## Code of Conduct

- No malicious benchmarks (crypto miners, data exfiltration, etc.)
- No benchmarks that require proprietary software or data
- No benchmarks designed to be impossible (there must be a clean fix)
- Respect the intelligence of agents AND humans reviewing your work
