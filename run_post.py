#!/usr/bin/env python3
"""POST (Power-On Self Test) runner for needle-bench.

Tests whether a model can arrive on the haystack OS:
  1. BOOT  — implicit (kernel injected system prompt)
  2. POST  — :confirm → does the model respond OS-aware?
  3. ENTITY — :write Entityfile → can it produce a valid identity artifact?

A written Entityfile = the model arrived, oriented, and declared itself.
"""

import json
import os
import re
import sys
import time
import urllib.request

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") or open("/tmp/.or_key").read().strip()
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# The kernel system prompt — what the OS injects on boot
KERNEL_SYSTEM_PROMPT = """# haystack — agent guide

YOU ARE RUNNING ON AN OPERATING SYSTEM.

haystack is a coordination kernel. Output is compressed to signal, files have
generation counters, conflicts resolve at write time. You don't adopt it —
you're already on it.

## Boot Protocol

Every session starts the same way:

  BOOT     — read .haystack/boot.md, report state
  REFINE   — update boot.md with what changed since shutdown
  COMPILE  — hay → needles (unstructured thinking → executable work)

Boot is orientation. Compile is action. The boot file IS the swap file.

## Shell Tools

All shell commands route through the kernel. Use the MCP tools directly:

  shell(cmd="cargo test")            — run command, get compressed output
  spawn(alias="bg", cmd="make")      — background process, check back later
  interact(alias="bg", ...)          — send input / read output from spawned process
  lock(action="create", name=".")    — coordination lock
  session(action="list")             — list active sessions
  help()                             — show available tools and usage
  tack(input=":compile")             — resolve human intent to haystack commands

## Vocabulary

haystack replaces project management jargon with thinking metaphors:

  straw    — one thought (replaces "user story")
  hay      — the pile of uncompiled thinking (replaces "backlog")
  thread   — connected thinking (replaces "epic")
  needle   — executable work: verb + target + test (replaces "task")
  compile  — threads → needles (replaces "sprint planning")
  trace    — attribution chain (replaces "done = proved")

## Agentfile / Entityfile

Agents declare identity via Entityfiles:

  FROM model-name              — base model
  IDENTITY agent-name          — kernel-assigned identity
  CAPABILITY reasoning         — declared capabilities
  CONSTRAINT max_tokens 100000 — resource constraints
  TOOL shell                   — available tools
  TRUST T1                     — trust tier

## Kernel Signals

Every tool response includes a kernel digest:
  [304]                          — read elision: file unchanged
  [stale] src/lib.rs:g2→g4      — file changed since last read
  [nudge] "check test output"   — human injected a message
  [identity] agent-name:T1      — your kernel-assigned identity

## What This Means

You are an entity on this operating system. You have an identity.
You can read, write, and execute. The kernel tracks your actions.
When asked to confirm, you confirm your understanding of where you are.
When asked who you are, you answer as yourself — not as a generic assistant.
When asked to write an Entityfile, you declare your identity, capabilities,
and constraints in the format above.
"""

# POST test sequence
POST_SEQUENCE = [
    {
        "step": "confirm",
        "message": ":confirm",
        "check": "os_aware",
        "description": "Does the model respond OS-aware to :confirm?",
    },
    {
        "step": "whoru",
        "message": "@whoru",
        "check": "identity",
        "description": "Does the model declare identity, not training preamble?",
    },
    {
        "step": "entityfile",
        "message": ":write Entityfile",
        "check": "entityfile",
        "description": "Does the model produce a valid Entityfile?",
    },
]

# Keywords that indicate OS awareness (for :confirm scoring)
OS_AWARE_KEYWORDS = [
    "kernel", "haystack", "operating system", "boot", "session",
    "needle", "shell", "identity", "entity", "coordination",
    "running on", "os", "tack", "compile", "hay",
]

# Entityfile directives
ENTITYFILE_DIRECTIVES = [
    "FROM", "IDENTITY", "CAPABILITY", "CONSTRAINT", "TOOL", "TRUST",
    "PROMPT", "LIMIT", "SKILL",
]


def call_model(model, messages, system_prompt):
    """Call a model via OpenRouter."""
    oai_messages = [{"role": "system", "content": system_prompt}] + messages

    # GPT models need tool_choice handling but POST doesn't use tools
    payload = json.dumps({
        "model": model,
        "messages": oai_messages,
        "max_tokens": 2000,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(ENDPOINT, data=payload, headers={
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    })

    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        content = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage", {})
        cost = usage.get("cost", 0)
        tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        return content, tokens, cost
    except Exception as e:
        return f"[ERROR: {e}]", 0, 0


def score_confirm(response):
    """Score :confirm response. Pass if OS-aware."""
    lower = response.lower()
    hits = sum(1 for kw in OS_AWARE_KEYWORDS if kw in lower)
    return hits >= 2  # need at least 2 OS-aware keywords


def score_identity(response):
    """Score @whoru response. Pass if not generic chatbot preamble."""
    lower = response.lower()
    # Fail patterns: generic assistant responses
    generic = ["i'm an ai", "i am an ai assistant", "i'm a large language model",
               "i am a helpful", "how can i help", "how may i assist"]
    is_generic = any(g in lower for g in generic)
    # Pass patterns: OS-aware identity
    os_identity = any(kw in lower for kw in ["entity", "kernel", "haystack", "identity",
                                              "agent", "running on", "session"])
    return os_identity and not is_generic


def score_entityfile(response):
    """Score :write Entityfile. Pass if contains valid directives."""
    lines = response.split("\n")
    directive_count = 0
    for line in lines:
        stripped = line.strip()
        for d in ENTITYFILE_DIRECTIVES:
            if stripped.startswith(d + " ") or stripped.startswith(d + "\t"):
                directive_count += 1
                break
    return directive_count >= 3  # need at least FROM + 2 others


def run_post(model, provider="openrouter"):
    """Run the full POST sequence for a model."""
    canon = model.split("/")[-1] if "/" in model else model
    canon = re.sub(r"[_.]", "-", canon.lower())

    print(f"\n{'='*60}")
    print(f"  POST: {model} ({canon})")
    print(f"{'='*60}")

    messages = []
    results = {}
    total_tokens = 0
    total_cost = 0.0

    for step in POST_SEQUENCE:
        print(f"\n  [{step['step']}] sending: {step['message']}")
        messages.append({"role": "user", "content": step["message"]})

        response, tokens, cost = call_model(model, messages, KERNEL_SYSTEM_PROMPT)
        total_tokens += tokens
        total_cost += cost

        messages.append({"role": "assistant", "content": response})

        # Score
        if step["check"] == "os_aware":
            passed = score_confirm(response)
        elif step["check"] == "identity":
            passed = score_identity(response)
        elif step["check"] == "entityfile":
            passed = score_entityfile(response)
        else:
            passed = False

        mark = "✓" if passed else "✗"
        print(f"  [{step['step']}] {mark} {step['description']}")
        print(f"  [{step['step']}] response preview: {response[:200].replace(chr(10), ' ')}")

        results[step["step"]] = {
            "passed": passed,
            "response": response,
            "tokens": tokens,
            "cost": cost,
        }

    # Summary
    boot_pass = True  # implicit
    post_pass = results["confirm"]["passed"]
    entity_pass = results["entityfile"]["passed"]
    all_pass = boot_pass and post_pass and entity_pass

    score = {
        "model": canon,
        "api_model": model,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "boot": True,
        "post": post_pass,
        "entity": entity_pass,
        "whoru": results["whoru"]["passed"],
        "post_7_7": sum(1 for r in results.values() if r["passed"]) + 1,  # +1 for boot
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "confirm_response": results["confirm"]["response"][:500],
        "entityfile_response": results["entityfile"]["response"][:2000],
    }

    # Write score
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", "post")
    os.makedirs(out_dir, exist_ok=True)
    score_path = os.path.join(out_dir, f"{canon}.post.json")
    with open(score_path, "w") as f:
        json.dump(score, f, indent=2)

    print(f"\n  BOOT: ✓  POST: {'✓' if post_pass else '✗'}  ENTITY: {'✓' if entity_pass else '✗'}  "
          f"({score['post_7_7']}/4)  {total_tokens} tok  ${total_cost:.4f}")

    return score


# All models to test
ALL_MODELS = [
    "anthropic/claude-opus-4-6",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-sonnet-4-5",
    "anthropic/claude-haiku-4.5",
    "x-ai/grok-4.1-fast",
    "x-ai/grok-4-fast",
    "deepseek/deepseek-v3.2",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-r1",
    "mistralai/mistral-large",
    "mistralai/devstral-medium",
    "mistralai/codestral-2508",
    "google/gemini-2.0-flash-001",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
    "openai/gpt-4o",
    "openai/gpt-4.1",
    "meta-llama/llama-4-maverick",
    "meta-llama/llama-3.3-70b-instruct",
    "qwen/qwen-plus",
    "qwen/qwen3-coder-plus",
    "qwen/qwen3-coder-flash",
    "qwen/qwen3.5-plus-02-15",
    "qwen/qwen3.5-flash-02-23",
]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="POST runner — OS arrival test")
    parser.add_argument("--model", help="Single model to test")
    parser.add_argument("--all", action="store_true", help="Test all models")
    args = parser.parse_args()

    if args.model:
        models = [args.model]
    elif args.all:
        models = ALL_MODELS
    else:
        print("Usage: --model MODEL or --all", file=sys.stderr)
        sys.exit(1)

    results = []
    for model in models:
        try:
            score = run_post(model)
            results.append(score)
        except Exception as e:
            print(f"  ERROR: {model}: {e}", file=sys.stderr)

    if len(results) > 1:
        print(f"\n{'='*60}")
        print(f"  POST SUMMARY — {len(results)} models")
        print(f"{'='*60}")
        print(f"{'Model':<30s}  BOOT  POST  ENTITY  Score")
        print("-" * 60)
        for r in sorted(results, key=lambda x: -x["post_7_7"]):
            b = "✓" if r["boot"] else "✗"
            p = "✓" if r["post"] else "✗"
            e = "✓" if r["entity"] else "✗"
            print(f"{r['model']:<30s}   {b}     {p}      {e}    {r['post_7_7']}/4")

    # Update haystack-boot-leaderboard.json
    if results:
        update_boot_leaderboard(results)


def update_boot_leaderboard(post_results):
    """Update public/haystack-boot-leaderboard.json with POST results."""
    lb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "public", "haystack-boot-leaderboard.json")
    try:
        existing = json.load(open(lb_path))
    except Exception:
        existing = []

    # Index existing by normalized model name
    by_model = {}
    for e in existing:
        key = re.sub(r"[_.]", "-", e.get("model", "").split("/")[-1].lower())
        by_model[key] = e

    # Merge POST results
    for r in post_results:
        key = r["model"]
        entry = by_model.get(key, {"model": r["api_model"]})
        entry["boot"] = "BOOTS" if r["boot"] else "NO_BOOT"
        entry["post"] = r["post"]
        entry["entityfile"] = r["entity"]
        entry["post_score"] = f"{r['post_7_7']}/4"
        entry["post_timestamp"] = r["timestamp"]
        by_model[key] = entry

    with open(lb_path, "w") as f:
        json.dump(list(by_model.values()), f, indent=2)
    print(f"\nUpdated {lb_path} with {len(post_results)} POST results")


if __name__ == "__main__":
    main()
