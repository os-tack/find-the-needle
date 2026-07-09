#!/usr/bin/env python3
"""
stall_watch.py — merciless stall detection for every needle-bench cell.

THE DEFECT THIS KILLS: the operator waited 20+ minutes, several times, for a
cell that was ALREADY DONE — the kernel journaled a terminal end_turn, then
teardown blocked on an orphaned pipe and `ostk bench` never exited (bench.rs
has an internal watchdog for this, but it "took over only sometimes").
Install/arg failures were equally silent: a dead child with no score and no
message. Silent waiting is a defect: anything that can block gets a deadline,
a detector, and a loud record.

Three detectors run concurrently around every `ostk bench` child process
(kernel AND native arms — this module wraps the run path for ALL arms):

  1. COMPLETION detector — the cell's score.json landing on disk, or (kernel
     arms) a terminal row in the container's .ostk/journal.jsonl
     (agent.completed / died_early / panic / stalled / api.error, or a
     cpu.response.decoded row with stop_reason=end_turn and no pending
     tool_calls — the signed proof the agent decided it was done). Terminal
     evidence + a live child after GRACE_S means teardown is hung: SIGKILL the
     process tree + container, salvage the journal, score the cell, and
     record teardown_masked=true — ONLY because the kill was needed.

  2. NO-PROGRESS watchdog — progress is any of: child stdout/stderr growth,
     journal growth (kernel), container filesystem activity (native — vendor
     CLIs write session files under /root and edit under the workdir), or the
     score file appearing. No progress for WARN_S -> loud WARN row; PROBE_S ->
     probe (child alive? container state? docker stats) recorded loudly;
     KILL_S -> SIGKILL + the cell is written as an INVALID score with
     stop_reason 'stall' (visible, fail-closed, retried/quarantined by the
     matrix runner) and the matrix CONTINUES.

  3. HARD DEADLINE — from the benchmark's own wall_clock budget plus fixed
     overhead, capped (never a 30-minute default hiding a 10-minute bench).

Every state transition is appended, timestamped, to runs/.cell_status.jsonl;
`./bench.sh --status` (or `python3 scripts/stall_watch.py --status`) renders
the live cell table so the operator never wonders what the bench is doing.

Stdlib only. Docker access is isolated behind DockerCellProbe so unit tests
inject fakes (no docker, no network).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Tunables — the lane-mandated escalation ladder. Env-overridable so tests
# (and desperate operators) can compress the timeline without editing code.
# ---------------------------------------------------------------------------
def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Thresholds:
    warn_s: float = _env_f("OSTK_STALL_WARN_S", 90.0)
    probe_s: float = _env_f("OSTK_STALL_PROBE_S", 180.0)
    kill_s: float = _env_f("OSTK_STALL_KILL_S", 300.0)
    grace_s: float = _env_f("OSTK_STALL_GRACE_S", 10.0)
    tick_s: float = _env_f("OSTK_STALL_TICK_S", 2.0)
    heartbeat_s: float = _env_f("OSTK_STALL_HEARTBEAT_S", 30.0)
    # container-side probes (docker exec) are heavier than a stat(); run them
    # every N ticks, not every tick.
    container_probe_every_ticks: int = 5


TERMINAL_LIFECYCLE_EVENTS = {
    "agent.completed", "agent.died_early", "agent.panic", "agent.stalled",
    "api.error",
}

# Candidate in-container journal locations ({workdir}/.ostk/journal.jsonl —
# scenario workdirs are /app or /workspace across the suite).
JOURNAL_CANDIDATES = ("/app/.ostk/journal.jsonl", "/workspace/.ostk/journal.jsonl")

STATUS_BASENAME = ".cell_status.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# journal parsing (python mirror of haystack bench.rs parse_journal_to_metrics
# / journaled_end_turn_complete — keep semantics in lockstep)
# ---------------------------------------------------------------------------
def journaled_end_turn_complete(row: dict) -> bool:
    """True iff this cpu.response.decoded row is a TERMINAL completion:
    stop_reason == end_turn with no pending tool_calls (absent/null/0/[] all
    count as none, mirroring bench.rs journaled_end_turn_complete)."""
    if row.get("event") != "cpu.response.decoded":
        return False
    if row.get("stop_reason") != "end_turn":
        return False
    tc = row.get("tool_calls")
    if tc is None:
        return True
    if isinstance(tc, (int, float)):
        return tc == 0
    if isinstance(tc, list):
        return len(tc) == 0
    return False


def iter_journal_rows(journal_text: str):
    for line in journal_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def journal_terminal_row(journal_text: str) -> dict | None:
    """Return the terminal journal row, if any: a lifecycle terminal event
    (agent.completed/died_early/panic/stalled, api.error) or an end_turn
    cpu.response.decoded with no pending tool_calls."""
    terminal = None
    for row in iter_journal_rows(journal_text):
        ev = row.get("event", "")
        if ev in TERMINAL_LIFECYCLE_EVENTS or journaled_end_turn_complete(row):
            terminal = row
    return terminal


def parse_journal_metrics(journal_text: str) -> dict:
    """Sum api.call / tool.dispatch rows into bench metrics — the python
    mirror of bench.rs parse_journal_to_metrics."""
    m = {
        "turns": 0, "tool_uses": 0,
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        "billed_tokens": 0,
        "stop_reason": "", "summary": "", "end_turn_reached": False,
        "last_test_exit": None,
    }
    for row in iter_journal_rows(journal_text):
        ev = row.get("event", "")
        if journaled_end_turn_complete(row):
            m["end_turn_reached"] = True
        if ev == "api.call":
            m["turns"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_create_tokens", "cache_create_5m_tokens",
                      "cache_create_1h_tokens", "billed_tokens"):
                m[k] += int(row.get(k) or 0)
            m["cost_usd"] += float(row.get("cost_usd") or 0.0)
        elif ev == "tool.dispatch":
            m["tool_uses"] += 1
        elif ev == "tool.bash":
            cmd = str(row.get("cmd", ""))
            if "test.sh" in cmd and row.get("exit_code") is not None:
                m["last_test_exit"] = int(row.get("exit_code"))
        elif ev in TERMINAL_LIFECYCLE_EVENTS:
            if ev == "api.error":
                m["stop_reason"] = "api_error"
                m["summary"] = str(row.get("error", ""))
            else:
                exit_code = row.get("exit_code", -1)
                if ev == "agent.completed" and exit_code == 0:
                    m["stop_reason"] = "end_turn"
                elif ev == "agent.completed":
                    m["stop_reason"] = f"exit_code: {exit_code}"
                else:
                    m["stop_reason"] = ev
    if not m["stop_reason"]:
        m["stop_reason"] = "end_turn" if m["end_turn_reached"] else "watchdog_no_terminal"
    return m


def score_from_journal(journal_text: str, *, model: str, bench: str, arm: str,
                       resolved: bool | None, wall_clock_s: float,
                       difficulty_tier: str = "medium",
                       bench_binary: dict | None = None) -> dict:
    """Synthesize a score payload from a salvaged kernel journal — used when
    `ostk bench` journaled terminal completion but hung before writing
    score.json. resolved=None means the oracle re-run was impossible; we fall
    back to the last journaled `test.sh` exit code and record the source."""
    m = parse_journal_metrics(journal_text)
    if resolved is None:
        resolution_source = "journal:last_test_exit"
        resolved_final = m["last_test_exit"] == 0
    else:
        resolution_source = "container:test.sh"
        resolved_final = bool(resolved)
    return {
        "benchmark": bench,
        "agent": f"{model}-{arm}",
        "arm": arm,
        "control_type": arm,
        "timestamp": now_iso(),
        "difficulty_tier": difficulty_tier,
        "resolved": resolved_final,
        "resolution_source": resolution_source,
        "turns_to_fix": m["turns"],
        "tool_uses": m["tool_uses"],
        "token_cost": m["input_tokens"] + m["output_tokens"],
        "input_tokens": m["input_tokens"],
        "output_tokens": m["output_tokens"],
        "fresh_input_tokens": m["input_tokens"],
        "cache_read_tokens": m["cache_read_tokens"],
        "cache_create_tokens": m["cache_create_tokens"],
        "cache_create_5m_tokens": m["cache_create_5m_tokens"],
        "cache_create_1h_tokens": m["cache_create_1h_tokens"],
        "billed_tokens": m["billed_tokens"],
        "estimated_cost_usd": round(m["cost_usd"], 6),
        "stop_reason": m["stop_reason"],
        "summary": m["summary"] or (
            f"{m['stop_reason']}: {m['turns']}turns, "
            f"{m['input_tokens']}+{m['output_tokens']} tokens, "
            f"${m['cost_usd']:.4f} (scored by stall_watch from journal)"),
        "wall_clock": round(wall_clock_s, 1),
        "teardown_masked": True,
        "scored_by": "stall_watch:journal",
        # Binary-identity receipt: ostk bench stamps its own bench_binary on
        # clean exits; a synthesized score must carry the same receipt or the
        # cell is unattributable in a multi-binary date window (snapshot_of).
        **({"bench_binary": bench_binary} if bench_binary else {}),
    }


def stall_score(*, model: str, bench: str, arm: str, wall_clock_s: float,
                detail: str, timeline: list[dict],
                difficulty_tier: str = "medium",
                bench_binary: dict | None = None) -> dict:
    """The INVALID cell record for a stall kill: stop_reason 'stall' (a
    first-class cell_validity reason), zero work claimed, full escalation
    timeline embedded for forensics. Fails closed by construction — the cell
    is excluded from aggregates and visibly counted."""
    return {
        "benchmark": bench,
        "agent": f"{model}-{arm}",
        "arm": arm,
        "control_type": arm,
        "timestamp": now_iso(),
        "difficulty_tier": difficulty_tier,
        "resolved": False,
        "turns_to_fix": 0,
        "token_cost": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_tokens": 0,
        "estimated_cost_usd": 0,
        "stop_reason": "stall",
        "summary": f"stall_watch kill: {detail}",
        "wall_clock": round(wall_clock_s, 1),
        "stall": {"detail": detail, "timeline": timeline},
        "scored_by": "stall_watch:stall_kill",
        **({"bench_binary": bench_binary} if bench_binary else {}),
    }


# ---------------------------------------------------------------------------
# docker probe — all container interaction lives here so tests can fake it
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


class DockerCellProbe:
    """Find + interrogate the docker container that `ostk bench` runs this
    cell in. Container names follow bench.rs: nb-{bench}{-native|-kernel}-
    {model[:20], / -> -}-{millis}. Every method degrades to None/False on
    docker trouble — the watcher must never crash on a probe."""

    def __init__(self, model: str, bench: str, arm: str, start_ts: float):
        arm_suffix = "-native" if arm == "native" else "-kernel"
        model_short = model.replace("/", "-")[:20]
        self.prefix = f"nb-{bench}{arm_suffix}-{model_short}-"
        self.start_ms = int(start_ts * 1000)
        self.container: str | None = None
        self._journal_path: str | None = None

    # -- discovery ----------------------------------------------------------
    def find_container(self) -> str | None:
        if self.container:
            return self.container
        proc = _run(["docker", "ps", "--format", "{{.Names}}"])
        if proc is None or proc.returncode != 0:
            return None
        best = None
        for name in proc.stdout.splitlines():
            name = name.strip()
            if not name.startswith(self.prefix):
                continue
            try:
                millis = int(name.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                continue
            # only containers launched for THIS attempt (5s clock slack)
            if millis >= self.start_ms - 5000 and (best is None or millis > best[0]):
                best = (millis, name)
        if best:
            self.container = best[1]
        return self.container

    def _exec(self, shell_cmd: str, timeout: float = 10.0) -> subprocess.CompletedProcess | None:
        c = self.find_container()
        if not c:
            return None
        return _run(["docker", "exec", c, "sh", "-c", shell_cmd], timeout=timeout)

    # -- journal ------------------------------------------------------------
    def journal_size(self) -> int | None:
        cands = [self._journal_path] if self._journal_path else list(JOURNAL_CANDIDATES)
        for path in cands:
            proc = self._exec(f"wc -c < {path} 2>/dev/null")
            if proc is not None and proc.returncode == 0 and proc.stdout.strip():
                try:
                    size = int(proc.stdout.strip())
                except ValueError:
                    continue
                self._journal_path = path
                return size
        return None

    def journal_text(self) -> str | None:
        cands = [self._journal_path] if self._journal_path else list(JOURNAL_CANDIDATES)
        for path in cands:
            proc = self._exec(f"cat {path} 2>/dev/null", timeout=30.0)
            if proc is not None and proc.returncode == 0 and proc.stdout:
                self._journal_path = path
                return proc.stdout
        return None

    # -- progress / state ---------------------------------------------------
    def activity_since(self, epoch: float) -> bool | None:
        """Any file under /root (vendor CLI session state) or the workdirs
        modified since `epoch`? The native-arm progress signal: vendor CLIs
        stream session/transcript writes while working."""
        proc = self._exec(
            "find /root /app /workspace -xdev -type f "
            f"-newermt @{int(epoch)} -print 2>/dev/null | head -1")
        if proc is None:
            return None
        return bool(proc.stdout.strip())

    def container_state(self) -> str | None:
        c = self.find_container()
        if not c:
            return None
        proc = _run(["docker", "inspect", "-f", "{{.State.Status}}", c])
        if proc is None or proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def stats_line(self) -> str | None:
        c = self.find_container()
        if not c:
            return None
        proc = _run(["docker", "stats", "--no-stream", "--format",
                     "cpu={{.CPUPerc}} mem={{.MemUsage}}", c], timeout=15.0)
        if proc is None or proc.returncode != 0:
            return None
        return proc.stdout.strip()

    # -- endgame ------------------------------------------------------------
    def run_resolution_test(self) -> bool | None:
        """Re-run the scenario oracle inside the still-alive container —
        mirrors bench.rs resolution_test_cmd. None if it can't be run."""
        proc = self._exec(
            "git config --global --add safe.directory '*' 2>/dev/null; "
            "export GOFLAGS=-buildvcs=false; "
            "cd /app 2>/dev/null || cd /workspace; bash test.sh >/dev/null 2>&1; echo rc=$?",
            timeout=180.0)
        if proc is None or "rc=" not in (proc.stdout or ""):
            return None
        try:
            return int(proc.stdout.rsplit("rc=", 1)[1].strip().split()[0]) == 0
        except (ValueError, IndexError):
            return None

    def salvage_journal(self, dest: Path) -> bool:
        text = self.journal_text()
        if not text:
            return False
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            return True
        except OSError:
            return False

    def remove(self) -> None:
        c = self.container or self.find_container()
        if c:
            _run(["docker", "rm", "-f", c], timeout=30.0)


# ---------------------------------------------------------------------------
# status ledger — every transition timestamped; `--status` renders it
# ---------------------------------------------------------------------------
class StatusLedger:
    def __init__(self, path: Path, model: str, bench: str, arm: str):
        self.path = path
        self.cell = {"model": model, "bench": bench, "arm": arm}
        self.timeline: list[dict] = []

    def emit(self, state: str, detail: str = "", **extra) -> None:
        row = {"ts": now_iso(), **self.cell, "state": state, "detail": detail}
        row.update(extra)
        self.timeline.append(row)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass
        # WARN and worse also go loudly to stderr — never a silent wait.
        if state not in ("running", "heartbeat", "done"):
            print(f"  [stall_watch] {row['ts']} {self.cell['model']}/"
                  f"{self.cell['bench']}/{self.cell['arm']} {state.upper()} {detail}",
                  file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# the watcher
# ---------------------------------------------------------------------------
@dataclass
class WatchResult:
    status: str          # exit | completed_kill | stall_kill | deadline_kill
    returncode: int | None
    detail: str
    tail: str
    teardown_masked: bool = False
    elapsed_s: float = 0.0
    timeline: list[dict] = field(default_factory=list)


def _kill_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (Popen used start_new_session),
    falling back to a plain kill."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _tail_of(path: Path, lines: int = 10) -> str:
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(content.strip().splitlines()[-lines:])


def _stamp_teardown_masked(score_path: Path) -> None:
    """Stamp teardown_masked=true INTO the payload (the writer-stamped field
    always wins downstream — consolidate_scores.load_teardown_sidecar doc)."""
    try:
        payload = json.loads(score_path.read_text())
        if isinstance(payload, dict):
            payload["teardown_masked"] = True
            payload["teardown_masked_by"] = "stall_watch"
            score_path.write_text(json.dumps(payload, indent=2))
    except (OSError, json.JSONDecodeError):
        pass


def watch_cell(cmd: list[str], *, model: str, bench: str, arm: str,
               score_path: Path, runs_root: Path, deadline_s: float,
               cwd: Path | None = None, env: dict | None = None,
               thresholds: Thresholds | None = None,
               probe: object | None = None,
               difficulty_tier: str = "medium",
               spool_path: Path | None = None,
               bench_binary: dict | None = None) -> WatchResult:
    """Run `cmd` (one ostk bench cell) under the three detectors.

    Wired into run_matrix_v41.run_cell for EVERY arm. `probe` defaults to a
    DockerCellProbe; tests inject fakes. Kernel arms get journal polling; all
    arms get output-growth + container-activity progress and the hard
    deadline. Returns a WatchResult; on completed_kill a score is guaranteed
    on disk (bench's own, stamped, or synthesized from the salvaged journal).
    """
    th = thresholds or Thresholds()
    kernel_arm = arm != "native"
    start = time.time()
    ledger = StatusLedger(runs_root / STATUS_BASENAME, model, bench, arm)

    if spool_path is None:
        spool_path = score_path.parent / f"{bench}.launcher.log"
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    spool = spool_path.open("wb")

    if probe is None:
        probe = DockerCellProbe(model, bench, arm, start)

    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=spool, stderr=subprocess.STDOUT,
        start_new_session=True)
    ledger.emit("running", f"pid={proc.pid} deadline={deadline_s:.0f}s "
                           f"spool={spool_path}")

    score_seen_at: float | None = None
    terminal_seen_at: float | None = None
    last_progress = start
    last_journal_size: int | None = None
    last_spool_size = 0
    last_heartbeat = start
    last_container_probe = 0.0
    warned = probed = False
    tick = 0

    def elapsed() -> float:
        return time.time() - start

    def finish(status: str, detail: str, masked: bool,
               rc: int | None) -> WatchResult:
        try:
            spool.close()
        except OSError:
            pass
        state = {"exit": "done", "completed_kill": "killed_teardown_masked",
                 "stall_kill": "killed_stall",
                 "deadline_kill": "killed_deadline"}[status]
        ledger.emit(state, detail, elapsed_s=round(elapsed(), 1))
        return WatchResult(status=status, returncode=rc, detail=detail,
                           tail=_tail_of(spool_path), teardown_masked=masked,
                           elapsed_s=elapsed(), timeline=ledger.timeline)

    def kill_everything() -> None:
        _kill_tree(proc)
        try:
            probe.remove()
        except Exception:
            pass

    while True:
        rc = proc.poll()
        if rc is not None:
            # Child exited on its own — the healthy path, nothing masked.
            return finish("exit", f"exit rc={rc}", False, rc)

        now = time.time()
        tick += 1

        # ---- progress signal 1: child output growth ------------------------
        try:
            spool.flush()
            size = spool_path.stat().st_size
        except OSError:
            size = last_spool_size
        if size > last_spool_size:
            last_spool_size = size
            last_progress = now

        # ---- progress signal 2: score file appearing ------------------------
        if score_seen_at is None and score_path.exists():
            try:
                fresh = score_path.stat().st_mtime >= start - 1
            except OSError:
                fresh = False
            if fresh:
                score_seen_at = now
                last_progress = now
                ledger.emit("score_landed",
                            "score.json written; waiting grace for clean exit")

        # ---- progress signal 3: journal growth (kernel arms) ---------------
        container_probe_due = (now - last_container_probe
                               >= th.tick_s * th.container_probe_every_ticks)
        if kernel_arm and container_probe_due and score_seen_at is None:
            jsize = None
            try:
                jsize = probe.journal_size()
            except Exception:
                jsize = None
            if jsize is not None and jsize != last_journal_size:
                last_journal_size = jsize
                last_progress = now
                # journal grew — check for a terminal row
                if terminal_seen_at is None:
                    try:
                        jtext = probe.journal_text()
                    except Exception:
                        jtext = None
                    if jtext and journal_terminal_row(jtext) is not None:
                        terminal_seen_at = now
                        ledger.emit(
                            "journal_terminal",
                            "journal shows terminal row (agent done); "
                            f"granting {th.grace_s:.0f}s teardown grace")

        # ---- progress signal 4: container fs activity (native arms) --------
        if (not kernel_arm) and container_probe_due \
                and now - last_progress > th.warn_s / 2:
            try:
                active = probe.activity_since(last_progress)
            except Exception:
                active = None
            if active:
                last_progress = now
        if container_probe_due:
            last_container_probe = now

        # ---- COMPLETION detector: grace then kill ---------------------------
        completion_at = score_seen_at or terminal_seen_at
        if completion_at is not None and now - completion_at >= th.grace_s:
            # Terminal evidence + child still alive after grace = hung teardown.
            detail_bits = []
            masked = True
            if score_seen_at is not None:
                detail_bits.append("score.json existed")
            if terminal_seen_at is not None and score_seen_at is None:
                # bench never wrote a score: salvage journal + oracle, then kill.
                raw_dir = score_path.parent / f"{bench}.raw"
                salvaged = False
                jtext = None
                try:
                    salvaged = probe.salvage_journal(raw_dir / "journal.jsonl")
                    jtext = (raw_dir / "journal.jsonl").read_text() if salvaged else None
                except Exception:
                    jtext = None
                resolved = None
                try:
                    resolved = probe.run_resolution_test()
                except Exception:
                    resolved = None
                if jtext:
                    payload = score_from_journal(
                        jtext, model=model, bench=bench, arm=arm,
                        resolved=resolved, wall_clock_s=elapsed(),
                        difficulty_tier=difficulty_tier,
                        bench_binary=bench_binary)
                    score_path.parent.mkdir(parents=True, exist_ok=True)
                    score_path.write_text(json.dumps(payload, indent=2))
                    detail_bits.append(
                        f"scored from salvaged journal (resolved={payload['resolved']}, "
                        f"source={payload['resolution_source']})")
                else:
                    detail_bits.append("journal salvage FAILED — no score written")
            ledger.emit("teardown_hung",
                        "terminal evidence but child alive after "
                        f"{th.grace_s:.0f}s grace — SIGKILL process tree + container")
            kill_everything()
            if score_seen_at is not None:
                _stamp_teardown_masked(score_path)
            detail = ("teardown masked: " + "; ".join(detail_bits)) if detail_bits \
                else "teardown masked"
            return finish("completed_kill", detail, masked, None)

        # ---- NO-PROGRESS watchdog ------------------------------------------
        # Completion evidence pauses the stall ladder (grace handles it).
        if completion_at is None:
            stalled_for = now - last_progress
            if stalled_for >= th.kill_s:
                detail = (f"no progress for {stalled_for:.0f}s "
                          f"(warn@{th.warn_s:.0f} probe@{th.probe_s:.0f} "
                          f"kill@{th.kill_s:.0f})")
                kill_everything()
                if not score_path.exists():
                    payload = stall_score(
                        model=model, bench=bench, arm=arm,
                        wall_clock_s=elapsed(), detail=detail,
                        timeline=ledger.timeline,
                        difficulty_tier=difficulty_tier,
                        bench_binary=bench_binary)
                    score_path.parent.mkdir(parents=True, exist_ok=True)
                    score_path.write_text(json.dumps(payload, indent=2))
                return finish("stall_kill", detail, False, None)
            if stalled_for >= th.probe_s and not probed:
                probed = True
                alive = proc.poll() is None
                cstate = stats = None
                try:
                    cstate = probe.container_state()
                    stats = probe.stats_line()
                except Exception:
                    pass
                ledger.emit("probing",
                            f"no progress {stalled_for:.0f}s; child_alive={alive} "
                            f"container={cstate or 'unknown'} {stats or ''}".strip())
            elif stalled_for >= th.warn_s and not warned:
                warned = True
                ledger.emit("warn_no_progress",
                            f"no progress for {stalled_for:.0f}s "
                            f"(kill at {th.kill_s:.0f}s)")
            elif stalled_for < th.warn_s:
                warned = probed = False

        # ---- HARD DEADLINE ---------------------------------------------------
        if elapsed() >= deadline_s:
            detail = f"hard deadline {deadline_s:.0f}s exceeded"
            kill_everything()
            if not score_path.exists():
                payload = stall_score(
                    model=model, bench=bench, arm=arm, wall_clock_s=elapsed(),
                    detail=detail, timeline=ledger.timeline,
                    difficulty_tier=difficulty_tier,
                    bench_binary=bench_binary)
                score_path.parent.mkdir(parents=True, exist_ok=True)
                score_path.write_text(json.dumps(payload, indent=2))
            return finish("deadline_kill", detail, False, None)

        # ---- heartbeat -------------------------------------------------------
        if now - last_heartbeat >= th.heartbeat_s:
            last_heartbeat = now
            ledger.emit("heartbeat",
                        f"elapsed={elapsed():.0f}s "
                        f"last_progress={now - last_progress:.0f}s ago "
                        f"output={last_spool_size}B "
                        f"journal={last_journal_size if last_journal_size is not None else '?'}B")

        time.sleep(th.tick_s)


# ---------------------------------------------------------------------------
# --status renderer
# ---------------------------------------------------------------------------
def render_status(status_path: Path, out=sys.stdout) -> int:
    """Render the last state of every cell in the ledger, newest activity
    first, with ages — so the operator NEVER wonders whether the bench is
    alive. Returns 0; a missing ledger is not an error (nothing has run)."""
    if not status_path.exists():
        print(f"no status ledger at {status_path} — nothing has run "
              f"under stall_watch yet.", file=out)
        return 0
    cells: dict[tuple, dict] = {}
    starts: dict[tuple, str] = {}
    for line in status_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (row.get("model"), row.get("bench"), row.get("arm"))
        if row.get("state") == "running":
            # a fresh attempt resets the cell's story
            starts[key] = row.get("ts", "")
        cells[key] = row
    now = datetime.now(timezone.utc)

    def age_of(ts: str) -> str:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return "?"
        s = (now - dt).total_seconds()
        if s < 90:
            return f"{s:.0f}s"
        if s < 5400:
            return f"{s / 60:.0f}m"
        return f"{s / 3600:.1f}h"

    live_states = {"running", "heartbeat", "warn_no_progress", "probing",
                   "score_landed", "journal_terminal"}
    rows = []
    for key, row in cells.items():
        model, bench, arm = key
        state = row.get("state", "?")
        rows.append((row.get("ts", ""), model or "?", bench or "?", arm or "?",
                     state, age_of(row.get("ts", "")),
                     age_of(starts.get(key, row.get("ts", ""))),
                     (row.get("detail") or "")[:60],
                     state in live_states))
    rows.sort(key=lambda r: r[0], reverse=True)
    w_cell = max([len(f"{m}/{b}/{a}") for _, m, b, a, *_ in rows] + [len("cell")])
    print(f"{'cell':<{w_cell}}  {'state':<24} {'since':>6} {'started':>8}  detail",
          file=out)
    print("-" * (w_cell + 50), file=out)
    live = 0
    for _, m, b, a, state, since, started, detail, is_live in rows:
        live += is_live
        marker = "*" if is_live else " "
        print(f"{m + '/' + b + '/' + a:<{w_cell}} {marker} {state:<24} "
              f"{since:>6} {started:>8}  {detail}", file=out)
    print(f"\n{live} live cell(s) (*), {len(rows) - live} settled. "
          f"ledger: {status_path}", file=out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="stall_watch — merciless per-cell stall detection")
    ap.add_argument("--status", action="store_true",
                    help="render the live cell-state table and exit")
    ap.add_argument("--file", default=None,
                    help=f"status ledger (default: <repo>/runs/{STATUS_BASENAME})")
    args = ap.parse_args()
    default_ledger = Path(__file__).resolve().parents[1] / "runs" / STATUS_BASENAME
    if args.status:
        return render_status(Path(args.file) if args.file else default_ledger)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
