#!/usr/bin/env python3
"""Unit tests for the offline-testable parts of scripts/preflight.py's live
probe machinery: routing plans, OpenRouter id formatting, closest-match
suggestions, and .env key resolution. NO network calls."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import preflight
from preflight import closest_models, format_openrouter_model, probe_plan


class TestOpenRouterFormatting(unittest.TestCase):
    def test_mirrors_model_registry(self):
        cases = {
            "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
            "claude-opus-4-8": "anthropic/claude-opus-4.8",
            "claude-fable-5": "anthropic/claude-fable-5",
            "gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview",
            "gpt-5.5": "openai/gpt-5.5",
            "grok-4.3": "x-ai/grok-4.3",
            "devstral-2512": "mistralai/devstral-2512",
            "devstral-small-latest": "mistralai/devstral-small",
            "kimi-k2.6": "moonshotai/kimi-k2.6",
            "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
            "qwen3-coder": "qwen/qwen3-coder",
            "llama-4-maverick": "meta-llama/llama-4-maverick",
            "already/prefixed": "already/prefixed",
        }
        for model, expected in cases.items():
            self.assertEqual(format_openrouter_model(model), expected, model)


class TestProbePlan(unittest.TestCase):
    def test_native_routes_to_vendor_endpoints(self):
        self.assertEqual(probe_plan("claude-fable-5", "native"),
                         [("anthropic", "claude-fable-5")])
        self.assertEqual(probe_plan("gemini-3.1-pro-preview", "native"),
                         [("google", "gemini-3.1-pro-preview")])
        self.assertEqual(probe_plan("gpt-5.5", "native"), [("openai", "gpt-5.5")])
        self.assertEqual(probe_plan("devstral-2512", "native"),
                         [("mistral", "devstral-2512")])
        self.assertEqual(probe_plan("kimi-k2.6", "native"),
                         [("moonshot", "kimi-k2.6")])
        self.assertEqual(probe_plan("grok-4.3", "native"), [("xai", "grok-4.3")])
        self.assertEqual(probe_plan("deepseek-v4-pro", "native"),
                         [("deepseek", "deepseek-v4-pro")])

    def test_kernel_routes_via_openrouter_with_formatted_id(self):
        self.assertEqual(probe_plan("kimi-k2.6", "kernel"),
                         [("openrouter", "moonshotai/kimi-k2.6")])
        self.assertEqual(probe_plan("grok-4.3", "kernel"),
                         [("openrouter", "x-ai/grok-4.3")])

    def test_kernel_cpu_native_drivers(self):
        self.assertEqual(probe_plan("claude-opus-4-8", "kernel-cpu"),
                         [("anthropic", "claude-opus-4-8")])
        self.assertEqual(probe_plan("gemini-3.1-pro-preview", "kernel-cpu"),
                         [("google", "gemini-3.1-pro-preview")])
        self.assertEqual(probe_plan("devstral-2512", "kernel-cpu"),
                         [("mistral", "devstral-2512")])

    def test_kernel_cpu_gpt_prefers_openrouter_then_openai(self):
        plan = probe_plan("gpt-5.5", "kernel-cpu")
        self.assertEqual(plan, [("openrouter", "openai/gpt-5.5"),
                                ("openai", "gpt-5.5")])

    def test_mlx_has_no_probe(self):
        self.assertEqual(probe_plan("mlx/ternary-bonsai-8b", "kernel-mlx"), [])

    def test_all_plan_providers_are_probeable(self):
        # every provider a plan can name must exist in PROBE_PROVIDERS
        for model in ("claude-fable-5", "gemini-3.1-pro-preview", "gpt-5.5",
                      "devstral-2512", "kimi-k2.6", "grok-4.3",
                      "deepseek-v4-pro", "qwen3-coder"):
            for arm in ("native", "kernel", "kernel-cpu"):
                for provider, _ in probe_plan(model, arm):
                    self.assertIn(provider, preflight.PROBE_PROVIDERS,
                                  f"{model}/{arm} -> {provider}")


class TestClosestMatches(unittest.TestCase):
    def test_wrong_name_gets_suggestions(self):
        ids = {"claude-opus-4-8", "claude-sonnet-4-6", "claude-fable-5",
               "claude-haiku-4-5"}
        hits = closest_models("claude-fable-5-pro", ids)
        self.assertIn("claude-fable-5", hits)

    def test_bare_suffix_matches_openrouter_paths(self):
        ids = {"moonshotai/kimi-k2.6", "x-ai/grok-4.3", "deepseek/deepseek-v4-pro"}
        hits = closest_models("kimi-k2.6", ids)
        self.assertEqual(hits[0], "moonshotai/kimi-k2.6")

    def test_empty_catalog_no_suggestions(self):
        self.assertEqual(closest_models("anything", set()), [])


class TestKeyResolution(unittest.TestCase):
    def test_env_wins_and_value_never_needed_in_message(self):
        import os
        var = "NEEDLE_TEST_FAKE_KEY"
        preflight._KEY_CACHE.pop(var, None)
        os.environ[var] = "sk-secret-value"
        try:
            got = preflight.resolve_key(var)
            self.assertIsNotNone(got)
            value, source = got
            self.assertEqual(source, "env")
        finally:
            del os.environ[var]
            preflight._KEY_CACHE.pop(var, None)

    def test_dotenv_parsing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env_fp = Path(tmp) / ".env"
            env_fp.write_text(
                "# comment\nexport NEEDLE_TEST_DOTENV_KEY='abc123'\nOTHER=x\n")
            old_files = preflight.ENV_FILES
            preflight.ENV_FILES = [env_fp]
            preflight._KEY_CACHE.pop("NEEDLE_TEST_DOTENV_KEY", None)
            try:
                got = preflight.resolve_key("NEEDLE_TEST_DOTENV_KEY")
                self.assertIsNotNone(got)
                value, source = got
                self.assertEqual(value, "abc123")
                self.assertEqual(source, str(env_fp))
            finally:
                preflight.ENV_FILES = old_files
                preflight._KEY_CACHE.pop("NEEDLE_TEST_DOTENV_KEY", None)


if __name__ == "__main__":
    unittest.main()
