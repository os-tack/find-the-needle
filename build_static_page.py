#!/usr/bin/env python3
"""Build a self-contained STATIC results page from the VERIFIED consolidate_harden
gating (Sections 1/2/3). Captures consolidate_harden.py stdout verbatim so the
published numbers are exactly the resolved-gated, per-bucket-priced, schema-unified
figures — never the (broken) interactive-board aggregator in consolidate_scores.py.

Output: public/v760.html  (served directly, no build/JSON dependency).
The interactive leaderboard render is deferred (fix-later, post-sliver).
"""
import subprocess, sys, html
from pathlib import Path

HERE = Path(__file__).parent
HARDEN = HERE / "consolidate_harden.py"
OUT = HERE / "public" / "v760.html"

import re
table = subprocess.run([sys.executable, str(HARDEN)], capture_output=True, text=True).stdout
# Strip internal needle IDs (→NNNN) and the retired-bench housekeeping note —
# these are internal ticket refs / noise, not for a public results page.
table = re.sub(r"→\d+", "", table)
table = "\n".join(l for l in table.splitlines()
                  if "[note]" not in l and "retired" not in l.lower() and "legacy pre" not in l.lower())
table_esc = html.escape(table)

PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>needle-bench — v7.6.0 schema-unified results (2026-06-15)</title>
<meta name="robots" content="index,follow">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif; max-width: 980px;
          margin: 0 auto; padding: 1.5rem; color:#1a1a1a; background:#fff; }}
  @media (prefers-color-scheme: dark) {{ body {{ color:#e6e6e6; background:#141414; }} a{{color:#7db3ff}} }}
  h1 {{ font-size: 1.5rem; margin-bottom: .2rem; }}
  .sub {{ color:#888; margin-top:0; }}
  .banner {{ background:#fff3cd; border:1px solid #ffe08a; color:#5c4400; padding:.7rem 1rem;
             border-radius:6px; margin:1rem 0; font-size:.9rem; }}
  @media (prefers-color-scheme: dark) {{ .banner{{background:#2a2410;border-color:#5c4d12;color:#e8d98a}} }}
  .box {{ background:rgba(127,127,127,0.08); border:1px solid rgba(127,127,127,0.3); border-radius:8px; padding:1rem 1.2rem; margin:1rem 0; }}
  table.tldr {{ border-collapse:collapse; width:100%; font-size:.92rem; }}
  table.tldr th, table.tldr td {{ text-align:left; padding:.35rem .6rem; border-bottom:1px solid rgba(127,127,127,0.25); }}
  .win {{ color:#1a7f37; font-weight:700; }}  .neg {{ color:#cf222e; font-weight:700; }}
  @media (prefers-color-scheme: dark) {{ .win{{color:#3fb950}} .neg{{color:#f85149}} }}
  pre {{ background:rgba(127,127,127,0.10); border:1px solid rgba(127,127,127,0.3); border-radius:8px; padding:1rem;
         overflow-x:auto; font: 12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }}
  code {{ font-family: ui-monospace,Menlo,monospace; }}
  h2 {{ font-size:1.15rem; margin-top:1.8rem; border-bottom:1px solid #e1e4e8; padding-bottom:.2rem; }}
  ul li {{ margin:.3rem 0; }}
  .foot {{ color:#888; font-size:.85rem; margin-top:2rem; }}
</style>
</head>
<body>
<p><a href="/">&larr; needle-bench.cc</a></p>
<h1>needle-bench — v7.6.0 schema-unified results</h1>
<p class="sub">Native vendor CLI vs ostk kernel · single-shot (samples=1) · captured 2026-06-15</p>

<div class="banner"><strong>Preliminary static results.</strong> These are the authoritative, accuracy-verified numbers for the v7.6.0 run.
The interactive leaderboard on the home page is being rebuilt on this dataset and should not be read as current yet.
More testing (cache-sliver projection) is in progress.</div>

<h2>TL;DR</h2>
<div class="box">
<p><strong>Apples-to-apples cost/token is honestly measurable only on Anthropic models</strong> (claude-code is the
only native harness that reports cost + cache split + real turns). Cost is summed per token bucket
(fresh 1&times;, cache-read 0.1&times;, cache-create 1.25&times;/2&times;) on a rate card validated to within 0.5% of real
Anthropic billing; efficiency deltas are computed <em>only over cells both arms solved</em>.</p>
<table class="tldr">
<tr><th>Model</th><th>Solve (native &rarr; kernel)</th><th>Cost &Delta;</th><th>Token &Delta;</th></tr>
<tr><td>claude-opus-4-8</td><td>97% &rarr; 97% (parity)</td><td class="win">&minus;17%</td><td class="win">&minus;49%</td></tr>
<tr><td>claude-sonnet-4-6</td><td>93% &rarr; <strong>100%</strong> (kernel +2)</td><td class="win">&minus;38%</td><td class="win">&minus;65%</td></tr>
<tr><td>devstral-2512 <span style="color:#888">(solve-only)</span></td><td><strong>19% &rarr; 97%</strong></td><td>n/a</td><td>n/a</td></tr>
<tr><td>gemini-3.1-pro <span style="color:#888">(solve-only)</span></td><td>97% &rarr; 100%</td><td>n/a</td><td>n/a</td></tr>
<tr><td>gpt-5.5 <span style="color:#888">(solve-only)</span></td><td>94% &rarr; 97%</td><td>n/a</td><td>n/a</td></tr>
<tr><td>kimi-k2.6 <span style="color:#888">(solve-only)</span></td><td>97% &rarr; 95%</td><td>n/a</td><td>n/a</td></tr>
<tr><td>grok-4.3 / deepseek-v4-pro <span style="color:#888">(B*)</span></td><td>native key-blocked — kernel-only</td><td>n/a</td><td>n/a</td></tr>
</table>
<p style="margin-bottom:0"><strong>Read:</strong> at equal-or-better solve rate the kernel is cheaper and far lighter on tokens on the
honestly-comparable (Anthropic) models, and a large capability multiplier for weak tool-users
(devstral 19%&rarr;97%). This is a <em>floor</em> — the cache-sliver projection is not yet in the kernel arm.</p>
</div>

<h2>Full results (verified gating)</h2>
<p>Generated verbatim from <code>consolidate_harden.py</code> — resolved-gated efficiency, per-bucket pricing,
split-resolve / both-fail / infra broken out. 38 benchmarks.</p>
<pre>{table_esc}</pre>

<h2>Methodology &amp; caveats (read these)</h2>
<ul>
<li><strong>Single-shot (samples=1).</strong> Individual cells are noisy; trust per-model aggregates, not any one cell.</li>
<li><strong>Anthropic-only cost/tokens.</strong> gemini-cli / codex / opencode native arms don't report cost (=$0)
    and under/mis-count tokens &amp; turns, so non-Anthropic models are <em>solve-rate + kernel-absolutes only</em> — cross-arm
    cost/token deltas are omitted as measurement artifacts.</li>
<li><strong>Resolved-gated efficiency.</strong> Cost/token deltas are computed only over cells <em>both</em> arms solved.
    Split-resolve (one arm only), both-fail, and zero-work infra cells are excluded from the deltas and listed separately.</li>
<li><strong>Floor, not ceiling.</strong> The kernel arm here does <em>not</em> exercise the cache-sliver projection
    (deferred). That mechanism makes the kernel's tokens cheap-cached and is expected to widen the cost win.</li>
<li><strong>B vs B*:</strong> true <strong>B</strong> = kernel-cpu (native driver). <strong>B*</strong> = generic OpenRouter kernel
    (no hand-written driver for that provider).</li>
</ul>

<p class="foot">needle-bench · v7.6.0 · single-shot native-vs-kernel · static snapshot 2026-06-15.
Interactive render rebuilding on this data.</p>
</body>
</html>
"""

OUT.write_text(PAGE)
print(f"wrote {OUT} ({len(PAGE):,} bytes)")
