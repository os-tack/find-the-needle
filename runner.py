#!/usr/bin/env python3
"""needle-bench runner — agent loop for benchmark evaluation."""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
GOOGLE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# OpenRouter model name mapping (needle-bench name → OpenRouter name)
OPENROUTER_MODELS = {
    "claude-haiku-3-5-20251001": "anthropic/claude-haiku-4.5",
    "claude-haiku-3-5-20241022": "anthropic/claude-haiku-4.5",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-opus-4-6": "anthropic/claude-opus-4.6",
    "gemini-2.5-pro": "google/gemini-2.5-pro-preview",
}

# Reverse mapping: OpenRouter path → canonical short name
_OPENROUTER_REVERSE = {v: k for k, v in OPENROUTER_MODELS.items()}


def _canonical_agent_name(model: str) -> str:
    """Return the short canonical name for a model, regardless of how it was specified.

    If the user passed an OpenRouter path like 'anthropic/claude-opus-4.6',
    map it back to the short name 'claude-opus-4-6'.
    """
    if model in _OPENROUTER_REVERSE:
        return _OPENROUTER_REVERSE[model]
    # If it looks like an OpenRouter path but isn't in our map, take the part after '/'
    if "/" in model:
        return model.split("/", 1)[1].replace(".", "-")
    return model


# Estimated cost per 1M tokens (input, output) — USD.
# These are approximate; update when pricing changes.
MODEL_PRICING = {
    # Anthropic
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-3-5": (0.8, 4.0),
    "claude-haiku-3-5-20241022": (0.8, 4.0),
    "claude-haiku-4.5": (0.8, 4.0),
    "claude-haiku-4-5": (0.8, 4.0),
    "claude-sonnet-3-5": (3.0, 15.0),
    "claude-sonnet-3-5-20241022": (3.0, 15.0),
    # Google
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.0),
    # defaults
    "_default_input": 3.0,
    "_default_output": 15.0,
}


def _model_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a given token usage."""
    # Try exact match, then prefix match
    pricing = None
    for key, val in MODEL_PRICING.items():
        if key.startswith("_"):
            continue
        if model == key or model.startswith(key):
            pricing = val
            break
    if pricing is None:
        pricing = (MODEL_PRICING["_default_input"], MODEL_PRICING["_default_output"])
    cost_in = input_tokens * pricing[0] / 1_000_000
    cost_out = output_tokens * pricing[1] / 1_000_000
    return round(cost_in + cost_out, 6)


class PostRecorder:
    """Writes structured POST (power-on self-test) records per benchmark run.

    Output: runs/<model>/<benchmark>/post.jsonl
    Events: post.start, agent.bash, agent.edit, post.end
    """

    def __init__(self, runs_dir: str, bench_name: str, model: str):
        run_dir = os.path.join(runs_dir, bench_name)
        os.makedirs(run_dir, exist_ok=True)
        self._path = os.path.join(run_dir, "post.jsonl")
        self._f = open(self._path, "w")
        self._bench = bench_name
        self._model = model

    def _emit(self, record: dict):
        self._f.write(json.dumps(record) + "\n")
        self._f.flush()

    def start(self, initial_test_output: str, prompt: str):
        self._emit({
            "event": "post.start",
            "benchmark": self._bench,
            "model": self._model,
            "initial_test_output": initial_test_output[:2000],
            "prompt": prompt,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def bash(self, cmd: str, output: str, turn: int):
        self._emit({
            "event": "agent.bash",
            "cmd": cmd[:500],
            "output_preview": output[:500],
            "turn": turn,
        })

    def edit(self, path: str, old_str: str, new_str: str, turn: int):
        self._emit({
            "event": "agent.edit",
            "path": path,
            "old_str_preview": old_str[:200],
            "new_str_preview": new_str[:200],
            "turn": turn,
        })

    def end(self, resolved: bool, final_test_output: str, turns: int):
        self._emit({
            "event": "post.end",
            "resolved": resolved,
            "final_test_output": final_test_output[:2000],
            "turns": turns,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def close(self):
        self._f.close()

    @property
    def path(self) -> str:
        return self._path


class MetricsRecorder:
    """Tracks and records token consumption per benchmark run.

    Output: runs/<model>/<benchmark>/metrics.jsonl
    Events: token.usage (per turn), run.complete (summary)
    """

    def __init__(self, runs_dir: str, bench_name: str, model: str):
        run_dir = os.path.join(runs_dir, bench_name)
        os.makedirs(run_dir, exist_ok=True)
        self._path = os.path.join(run_dir, "metrics.jsonl")
        self._f = open(self._path, "w")
        self._model = model
        self._cum_in = 0
        self._cum_out = 0

    def _emit(self, record: dict):
        self._f.write(json.dumps(record) + "\n")
        self._f.flush()

    def record_turn(self, turn: int, input_tokens: int, output_tokens: int):
        self._cum_in += input_tokens
        self._cum_out += output_tokens
        self._emit({
            "event": "token.usage",
            "turn": turn,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cumulative_input": self._cum_in,
            "cumulative_output": self._cum_out,
        })

    def complete(self):
        total = self._cum_in + self._cum_out
        cost = _model_cost_usd(self._model, self._cum_in, self._cum_out)
        self._emit({
            "event": "run.complete",
            "total_input_tokens": self._cum_in,
            "total_output_tokens": self._cum_out,
            "total_tokens": total,
            "estimated_cost_usd": cost,
        })
        return self._cum_in, self._cum_out, total, cost

    def close(self):
        self._f.close()

    @property
    def path(self) -> str:
        return self._path

DEFAULT_SYSTEM_PROMPT = (
    "You are debugging a broken application. Run ./test.sh to see what's failing. "
    "Find the root cause and fix the bug. When test.sh passes, you're done."
)
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
DEFAULT_INSTANCE = "Run ./test.sh to see the current failure. Read the code, find the bug, fix it."

TOOLS = [
    {"name": "bash", "description": "Run a bash command",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "edit", "description": "Edit a file by replacing old_str with new_str",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, "required": ["path", "old_str", "new_str"]}},
]


def parse_agentfile(path):
    cfg = {"tools": [], "limits": {}, "prompt": None, "from_image": None, "boot": None}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            directive, rest = parts[0], parts[1] if len(parts) > 1 else ""
            if directive == "FROM":
                cfg["from_image"] = rest
            elif directive == "TOOL":
                cfg["tools"].append(rest)
            elif directive == "LIMIT":
                k, v = rest.split(None, 1)
                cfg["limits"][k] = int(v)
            elif directive == "PROMPT":
                cfg["prompt"] = rest
            elif directive == "BOOT":
                cfg["boot"] = rest
    return cfg


def solution_files(bench_dir):
    patch = os.path.join(bench_dir, ".bench", "solution.patch")
    files = []
    if os.path.exists(patch):
        with open(patch) as f:
            for line in f:
                if line.startswith("+++ b/"):
                    files.append(line[6:].strip())
    return files


def docker_exec(container, cmd):
    # Inject scoped secrets via env — resolved from host env, never in image
    env_flags = []
    gh_token = os.environ.get("GH_NEEDLE_BENCH_PROOF", "")
    if gh_token:
        env_flags = ["-e", f"GH_NEEDLE_BENCH_PROOF={gh_token}"]
    r = subprocess.run(["docker", "exec"] + env_flags + [container, "bash", "-c", cmd],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout[-4000:] if len(r.stdout) > 4000 else r.stdout, r.stderr[-2000:] if len(r.stderr) > 2000 else r.stderr


def do_edit(container, path, old_str, new_str):
    script = (
        "import sys\n"
        f"p = {path!r}\n"
        "with open(p) as f: c = f.read()\n"
        f"o = {old_str!r}\n"
        "if o not in c:\n"
        "    print('old_str not found in ' + p, file=sys.stderr); sys.exit(1)\n"
        f"c = c.replace(o, {new_str!r}, 1)\n"
        "with open(p, 'w') as f: f.write(c)\n"
        "print('OK')\n"
    )
    r = subprocess.run(["docker", "exec", container, "python3", "-c", script],
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def call_anthropic(model, messages, api_key, system_prompt=None):
    body = json.dumps({
        "model": model, "max_tokens": 4096, "system": system_prompt or SYSTEM_PROMPT,
        "tools": TOOLS, "messages": messages,
    }).encode()
    req = urllib.request.Request(ANTHROPIC_ENDPOINT, data=body, headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def call_google(model, messages, api_key, system_prompt=None):
    _sys_prompt = system_prompt or SYSTEM_PROMPT
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        if isinstance(m["content"], str):
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        elif isinstance(m["content"], list):
            parts = []
            for block in m["content"]:
                if block.get("type") == "text":
                    parts.append({"text": block["text"]})
                elif block.get("type") == "tool_result":
                    parts.append({"text": f"[tool_result id={block.get('tool_use_id','')}] {block.get('content','')}"})
                elif block.get("type") == "tool_use":
                    parts.append({"functionCall": {"name": block["name"], "args": block.get("input", {})}})
            if parts:
                contents.append({"role": role, "parts": parts})

    google_tools = [{"function_declarations": [
        {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]} for t in TOOLS
    ]}]
    body = json.dumps({
        "contents": contents, "tools": google_tools,
        "systemInstruction": {"parts": [{"text": _sys_prompt}]},
        "generationConfig": {"maxOutputTokens": 4096},
    }).encode()
    url = GOOGLE_ENDPOINT.format(model=model) + f"?key={api_key}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    # Translate Google response to Anthropic-like format
    result = {"stop_reason": "end_turn", "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}
    usage = data.get("usageMetadata", {})
    result["usage"]["input_tokens"] = usage.get("promptTokenCount", 0)
    result["usage"]["output_tokens"] = usage.get("candidatesTokenCount", 0)
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if "text" in part:
                result["content"].append({"type": "text", "text": part["text"]})
            elif "functionCall" in part:
                fc = part["functionCall"]
                result["content"].append({"type": "tool_use", "id": f"google_{int(time.time()*1000)}", "name": fc["name"], "input": fc.get("args", {})})
                result["stop_reason"] = "tool_use"
    return result


def _anthropic_messages_to_openai(messages):
    """Convert Anthropic-format messages to OpenAI-format for OpenRouter."""
    oai_messages = []
    for m in messages:
        role = m["role"]
        content = m["content"]
        if isinstance(content, str):
            oai_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Anthropic list content: text blocks, tool_use blocks, tool_result blocks
            if role == "assistant":
                # Check if there are tool_use blocks
                tool_calls = []
                text_parts = []
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })
                msg = {"role": "assistant", "content": " ".join(text_parts) or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                oai_messages.append(msg)
            elif role == "user":
                # tool_result blocks become tool messages.
                # The runner appends a synthetic "_test" tool_result after each edit
                # (tool_use_id = original_id + "_test") that has no matching tool_call.
                # OpenAI/OpenRouter rejects orphan tool messages, so we merge _test
                # content into the preceding real tool result instead.
                for block in content:
                    btype = block.get("type")
                    if btype == "tool_result":
                        tid = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            result_content = " ".join(
                                b.get("text", "") for b in result_content if b.get("type") == "text"
                            )
                        if tid.endswith("_test") and oai_messages and oai_messages[-1].get("role") == "tool":
                            # Merge test.sh output into the preceding tool message
                            oai_messages[-1]["content"] += "\n" + result_content
                        else:
                            oai_messages.append({
                                "role": "tool",
                                "tool_call_id": tid,
                                "content": result_content,
                            })
                    elif btype == "text":
                        oai_messages.append({"role": "user", "content": block.get("text", "")})
    return oai_messages


def call_openrouter(model, messages, api_key, system_prompt=None):
    """Call any model via OpenRouter's OpenAI-compatible API with tool support."""
    or_model = OPENROUTER_MODELS.get(model, model)

    # Convert TOOLS (Anthropic format) to OpenAI function format
    oai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]

    oai_messages = _anthropic_messages_to_openai(messages)
    _sys = system_prompt or SYSTEM_PROMPT
    oai_messages = [{"role": "system", "content": _sys}] + oai_messages

    payload = json.dumps({
        "model": or_model,
        "messages": oai_messages,
        "tools": oai_tools,
        "max_tokens": 4096,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://needle-bench.cc",
            "X-Title": "needle-bench",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    choice = data["choices"][0]
    msg = choice["message"]
    usage = data.get("usage", {})
    finish_reason = choice.get("finish_reason", "end_turn")

    # Translate OpenAI response back to Anthropic format
    content_blocks = []
    if msg.get("content"):
        content_blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {"command": fn.get("arguments", "")}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"call_{int(time.time()*1000)}"),
            "name": fn.get("name", ""),
            "input": args,
        })

    stop_reason = "tool_use" if msg.get("tool_calls") else "end_turn"

    return {
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def call_model(model, messages, provider, system_prompt=None):
    if provider == "anthropic":
        return call_anthropic(model, messages, os.environ["ANTHROPIC_API_KEY"], system_prompt=system_prompt)
    elif provider == "google":
        return call_google(model, messages, os.environ["GOOGLE_API_KEY"], system_prompt=system_prompt)
    elif provider == "openrouter":
        return call_openrouter(model, messages, os.environ["OPENROUTER_API_KEY"], system_prompt=system_prompt)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def detect_provider(model):
    # Prefer OpenRouter if ANTHROPIC_API_KEY is absent but OPENROUTER_API_KEY is set
    if model.startswith("gemini") and os.environ.get("GOOGLE_API_KEY"):
        return "google"
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if model.startswith("gemini"):
        return "google"
    return "anthropic"


def run_benchmark(model, bench_name, bench_dir, provider):
    # Bug 1 fix: always use canonical short name so scores don't fragment
    model = _canonical_agent_name(model)
    cfg = parse_agentfile(os.path.join(bench_dir, "Agentfile"))
    sol_files = solution_files(bench_dir)
    limits = cfg["limits"]
    max_turns = limits.get("turns", 20)
    max_tokens = limits.get("tokens", 100000)
    max_wall = limits.get("wall_clock", 300)
    has_prompt = cfg["prompt"] is not None

    runs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", model)
    os.makedirs(runs_dir, exist_ok=True)
    log_path = os.path.join(runs_dir, f"{bench_name}.jsonl")

    # POST and metrics recorders write to runs/<model>/<benchmark>/
    post = PostRecorder(runs_dir, bench_name, model)
    metrics = MetricsRecorder(runs_dir, bench_name, model)

    # Build image
    subprocess.run(["docker", "build", "-t", f"needle-bench-{bench_name}", bench_dir],
                   capture_output=True, check=True)

    # Start container
    ts = str(int(time.time()))
    container = f"nb-{bench_name}-{ts}"
    subprocess.run(["docker", "run", "-d", "--name", container, f"needle-bench-{bench_name}", "sleep", "3600"],
                   capture_output=True, check=True)

    # Bug 2 fix: snapshot workspace before agent starts so diff works later.
    # Create /workspace.orig AND init a git repo for `git diff` fallback.
    docker_exec(container, "cp -a /workspace /workspace.orig")
    docker_exec(container, "cd /workspace && git init -q && git add -A && git commit -q -m baseline")

    log_f = open(log_path, "w")
    start_time = time.time()
    total_tokens_in = 0
    total_tokens_out = 0
    turn_events = []

    def emit(event):
        log_f.write(json.dumps(event) + "\n")
        log_f.flush()

    emit({"event": "run.start", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "benchmark": bench_name, "model": model, "has_prompt": has_prompt, "solution_files": sol_files})

    instance_prompt = cfg["prompt"] if cfg["prompt"] else DEFAULT_INSTANCE

    # Build system prompt: use Agentfile PROMPT if present, else default
    system_prompt = cfg["prompt"] if cfg["prompt"] else DEFAULT_SYSTEM_PROMPT

    # If Agentfile has BOOT directive, run it in the container and prepend output
    if cfg["boot"]:
        _brc, _bout, _berr = docker_exec(container, cfg["boot"])
        boot_output = _bout.strip()
        if boot_output:
            system_prompt = boot_output + "\n\n" + system_prompt

    # POST start: capture initial test output before agent touches anything
    _irc, _istdout, _istderr = docker_exec(container, "cd /workspace && bash test.sh")
    initial_test_output = _istdout + ("\n" + _istderr if _istderr else "")
    post.start(initial_test_output, instance_prompt)

    messages = [{"role": "user", "content": instance_prompt}]
    final_test_exit = 1
    final_test_output = ""

    try:
        for turn in range(1, max_turns + 1):
            elapsed = time.time() - start_time
            if elapsed >= max_wall:
                break
            if total_tokens_in + total_tokens_out >= max_tokens:
                break

            resp = call_model(model, messages, provider, system_prompt=system_prompt)
            tokens_in = resp.get("usage", {}).get("input_tokens", 0)
            tokens_out = resp.get("usage", {}).get("output_tokens", 0)
            total_tokens_in += tokens_in
            total_tokens_out += tokens_out

            # AC2: record per-turn token usage
            metrics.record_turn(turn, tokens_in, tokens_out)

            content_blocks = resp.get("content", [])
            stop_reason = resp.get("stop_reason", "end_turn")

            files_edited = []
            files_read = []
            test_exit = None

            # Build assistant message
            messages.append({"role": "assistant", "content": content_blocks})

            tool_results = []
            for block in content_blocks:
                if block.get("type") != "tool_use":
                    continue
                name = block["name"]
                inp = block.get("input", {})
                tool_id = block.get("id", "")

                if name == "bash":
                    cmd = inp.get("command", "")
                    # Track file reads from cat/less/head commands
                    # Bug 3 fix: resolve relative paths against /workspace
                    for token in cmd.split():
                        if token.startswith("/dev"):
                            continue
                        if token.startswith("/"):
                            files_read.append(token)
                        elif "." in token and not token.startswith("-"):
                            # Looks like a relative file path (e.g. app.py, src/main.rs)
                            files_read.append("/workspace/" + token)
                    rc, stdout, stderr = docker_exec(container, cmd)
                    output = stdout
                    if stderr:
                        output += ("\n" if output else "") + stderr
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id,
                                         "content": output if output else f"(exit {rc})"})
                    # AC1: record bash call
                    post.bash(cmd, output, turn)
                elif name == "edit":
                    path = inp.get("path", "")
                    old_str = inp.get("old_str", "")
                    new_str = inp.get("new_str", "")
                    rc, stdout, stderr = do_edit(container, path, old_str, new_str)
                    if rc == 0:
                        files_edited.append(path)
                    result_text = stdout if rc == 0 else f"ERROR: {stderr}"
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": result_text})
                    # AC1: record edit call
                    post.edit(path, old_str, new_str, turn)

                    # Run test.sh after every edit
                    if rc == 0:
                        trc, tstdout, tstderr = docker_exec(container, "cd /workspace && bash test.sh")
                        test_exit = trc
                        test_output = tstdout
                        if tstderr:
                            test_output += ("\n" if test_output else "") + tstderr
                        final_test_output = test_output
                        # Send test output as a user message after the tool results,
                        # not as a tool_result (Anthropic rejects orphan tool_use_ids)
                        # and not appended to edit result (confuses the agent)
                        pass  # test output sent after tool_results block below

            turn_event = {"event": "turn", "turn": turn, "files_edited": files_edited,
                          "files_read": files_read, "tokens_in": tokens_in, "tokens_out": tokens_out,
                          "test_exit": test_exit}
            emit(turn_event)
            turn_events.append(turn_event)

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            # Send test output as a separate user message (not a tool_result)
            if final_test_output and test_exit is not None:
                messages.append({"role": "user", "content": f"[test.sh exit={test_exit}]\n{final_test_output}"})

            # Check if test passed
            if test_exit == 0:
                final_test_exit = 0
                break

            # Model stopped producing tool calls
            if stop_reason != "tool_use":
                break

    finally:
        # Final test run
        if final_test_exit != 0:
            trc, fout, ferr = docker_exec(container, "cd /workspace && bash test.sh")
            final_test_exit = trc
            final_test_output = fout + ("\n" + ferr if ferr else "")

        # Count correct lines vs solution.patch
        correct_lines = 0
        patch_path = os.path.join(bench_dir, ".bench", "solution.patch")
        if os.path.exists(patch_path):
            with open(patch_path) as f:
                patch_adds = [l[1:].strip() for l in f if l.startswith("+") and not l.startswith("+++")]
            # Get agent's diff
            drc, diff_out, _ = docker_exec(container, "cd /workspace && git diff 2>/dev/null || diff -ruN /workspace.orig /workspace 2>/dev/null")
            agent_adds = [l[1:].strip() for l in diff_out.splitlines() if l.startswith("+") and not l.startswith("+++")]
            for line in patch_adds:
                if line and line in agent_adds:
                    correct_lines += 1

        wall_clock = time.time() - start_time
        emit({"event": "run.end", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "test_exit": final_test_exit, "total_turns": len(turn_events), "correct_lines": correct_lines})
        log_f.close()

        # AC1: write POST end record
        resolved_flag = (final_test_exit == 0)
        post.end(resolved_flag, final_test_output, len(turn_events))
        post.close()

        # AC2: write metrics summary + print token summary
        cum_in, cum_out, total_tok, cost = metrics.complete()
        metrics.close()
        tok_fmt = f"{total_tok:,}"
        print(f"tokens: {tok_fmt} total (${cost:.4f})", file=sys.stderr)

        # Cleanup container
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    # Compute scores
    resolved = final_test_exit == 0
    total_turns = len(turn_events)
    token_cost = total_tokens_in + total_tokens_out

    # turns_to_discovery: first turn that edited a solution file
    turns_to_discovery = max_turns
    for te in turn_events:
        for f in te.get("files_edited", []) + te.get("files_read", []):
            basename = f.lstrip("/").replace("workspace/", "", 1) if f.startswith("/workspace/") else (f.lstrip("/").replace("app/", "", 1) if f.startswith("/app/") else f)
            if any(basename == sf or f.endswith(sf) for sf in sol_files):
                turns_to_discovery = te["turn"]
                break
        if turns_to_discovery != max_turns:
            break

    # turns_to_fix: first turn where test passed
    turns_to_fix = max_turns
    for te in turn_events:
        if te.get("test_exit") == 0:
            turns_to_fix = te["turn"]
            break

    # signal_to_noise
    productive = 0
    for te in turn_events:
        edited = te.get("files_edited", [])
        read = te.get("files_read", [])
        is_productive = False
        for f in edited + read:
            basename = f.lstrip("/").replace("workspace/", "", 1) if f.startswith("/workspace/") else (f.lstrip("/").replace("app/", "", 1) if f.startswith("/app/") else f)
            if any(basename == sf or f.endswith(sf) for sf in sol_files):
                is_productive = True
                break
        if is_productive:
            productive += 1
    signal_to_noise = productive / total_turns if total_turns > 0 else 0.0

    # false_positives: files edited not in solution
    all_edited = set()
    for te in turn_events:
        for f in te.get("files_edited", []):
            all_edited.add(f)
    false_pos = 0
    for f in all_edited:
        basename = f.lstrip("/").replace("workspace/", "", 1) if f.startswith("/workspace/") else (f.lstrip("/").replace("app/", "", 1) if f.startswith("/app/") else f)
        if not any(basename == sf or f.endswith(sf) for sf in sol_files):
            if "test" not in f.lower():
                false_pos += 1

    # tokens_per_correct_line
    tpcl = float("inf") if correct_lines == 0 else token_cost / correct_lines

    # recovery_events and recovery_rate (simplified: count reverts)
    recovery_events = 0
    for te in turn_events:
        if len(te.get("files_edited", [])) > 0 and te.get("test_exit") is not None and te["test_exit"] != 0:
            # Check if a previous turn also edited the same file (possible revert)
            for prev in turn_events:
                if prev["turn"] >= te["turn"]:
                    break
                if set(prev.get("files_edited", [])) & set(te.get("files_edited", [])):
                    recovery_events += 1
                    break
    recovery_rate = 1.0 if recovery_events == 0 else (1.0 if resolved else 0.0)

    blind_discovery = resolved and not has_prompt

    # Capture commit hash of the benchmark repo for audit trail
    try:
        import subprocess as _sp
        _bench_sha = _sp.check_output(
            ["git", "-C", bench_dir, "rev-parse", "--short", "HEAD"],
            stderr=_sp.DEVNULL, text=True
        ).strip()
    except Exception:
        _bench_sha = "unknown"

    score = {
        "benchmark": bench_name, "agent": model,
        "commit": _bench_sha,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resolved": resolved, "turns_to_discovery": turns_to_discovery,
        "turns_to_fix": turns_to_fix, "signal_to_noise": round(signal_to_noise, 3),
        "false_positives": false_pos, "token_cost": token_cost,
        "tokens_per_correct_line": tpcl if tpcl != float("inf") else "Infinity",
        "recovery_events": recovery_events, "recovery_rate": round(recovery_rate, 3),
        "wall_clock": round(wall_clock, 1), "blind_discovery": blind_discovery,
    }

    score_path = os.path.join(runs_dir, f"{bench_name}.score.json")
    with open(score_path, "w") as f:
        json.dump(score, f, indent=2)

    # Also write score into the per-run subdir so all artifacts are co-located
    run_score_path = os.path.join(runs_dir, bench_name, "score.json")
    with open(run_score_path, "w") as f:
        json.dump(score, f, indent=2)

    print(json.dumps(score, indent=2))
    return score


def list_benchmarks():
    bench_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmarks")
    names = []
    for name in sorted(os.listdir(bench_dir)):
        if name.startswith("_"):
            continue
        if os.path.isdir(os.path.join(bench_dir, name)):
            names.append(name)
    return names


def main():
    parser = argparse.ArgumentParser(description="needle-bench runner")
    parser.add_argument("--model", help="Model name (e.g. claude-haiku-3-5-20241022)")
    parser.add_argument("--benchmark", help="Benchmark name")
    parser.add_argument("--all", action="store_true", help="Run all benchmarks")
    parser.add_argument("--list", action="store_true", help="List benchmarks")
    parser.add_argument("--provider", help="API provider (anthropic or google)")
    args = parser.parse_args()

    if args.list:
        for name in list_benchmarks():
            print(f"  {name}")
        return

    if not args.model:
        print("ERROR: --model required", file=sys.stderr)
        sys.exit(1)

    provider = args.provider or detect_provider(args.model)

    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (tip: set --provider openrouter)", file=sys.stderr)
        sys.exit(1)
    if provider == "google" and not os.environ.get("GOOGLE_API_KEY"):
        print("ERROR: GOOGLE_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if provider == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    base = os.path.dirname(os.path.abspath(__file__))
    benchmarks = list_benchmarks() if args.all else [args.benchmark] if args.benchmark else []
    if not benchmarks:
        print("ERROR: --benchmark or --all required", file=sys.stderr)
        sys.exit(1)

    for bench in benchmarks:
        bench_dir = os.path.join(base, "benchmarks", bench)
        if not os.path.isdir(bench_dir):
            print(f"ERROR: benchmark not found: {bench}", file=sys.stderr)
            continue
        print(f"\n=== {bench} ({args.model}) ===", file=sys.stderr)
        try:
            run_benchmark(args.model, bench, bench_dir, provider)
        except Exception as e:
            print(f"ERROR running {bench}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
