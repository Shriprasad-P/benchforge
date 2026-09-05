import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from benchforge.patch import InvalidPatch, extract_patch, validate_patch
from benchforge.process import execute

GOOD_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,4 @@
 def average(values):
+    if not values:
+        raise ValueError('values must not be empty')
     return sum(values) / len(values)
"""


class PatchTests(unittest.TestCase):
    def test_plain_diff(self):
        validate_patch(extract_patch(GOOD_PATCH))

    def test_markdown_fence(self):
        patch = extract_patch("```diff\n" + GOOD_PATCH + "```")
        self.assertEqual(patch, GOOD_PATCH)

    def test_empty_response_is_noop(self):
        self.assertEqual(extract_patch(""), "")
        validate_patch("")

    def test_reject_forbidden_file(self):
        with self.assertRaises(InvalidPatch):
            validate_patch(GOOD_PATCH.replace("calculator.py", "tests/test_issue.py"))

    def test_reject_traversal(self):
        with self.assertRaises(InvalidPatch):
            validate_patch(GOOD_PATCH.replace("calculator.py", "../escape.py"))

    def test_reject_mode_changes(self):
        with self.assertRaises(InvalidPatch):
            validate_patch("new mode 100755\n" + GOOD_PATCH)

    def test_reject_prose_only(self):
        with self.assertRaises(InvalidPatch):
            extract_patch("I fixed the issue.")

    def test_reject_multiple_fences(self):
        with self.assertRaises(InvalidPatch):
            extract_patch("```diff\n" + GOOD_PATCH + "```\n```diff\n" + GOOD_PATCH + "```")


class ProcessTests(unittest.TestCase):
    def test_capture(self):
        result = execute([sys.executable, "-c", "print('hello')"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "hello")

    def test_timeout(self):
        result = execute(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            timeout=0.1,
        )
        self.assertTrue(result["timeout"])

    def test_bounded_output(self):
        result = execute(
            [sys.executable, "-c", "print('x' * 300000)"]
        )
        self.assertTrue(result["output_truncated"])
        self.assertLessEqual(len(result["stdout"]), 128 * 1024)

    def test_missing_executable(self):
        result = execute(["benchforge-nonexistent-command-123456"])
        self.assertTrue(result["spawn_error"])


class FixtureTests(unittest.TestCase):
    def test_fixture_includes_hidden_tests(self):
        from benchforge.engine import BENCHMARK

        self.assertIn("assertRaises", BENCHMARK["tests"]["issue"])
        self.assertIn("test_integer_values", BENCHMARK["tests"]["regression"])
        self.assertEqual(
            BENCHMARK["commands"]["task"][-1],
            "tests.test_issue",
        )

    def test_public_benchmark_omits_hidden_material(self):
        from benchforge.engine import public_benchmark

        payload = public_benchmark()
        encoded = json.dumps(payload)
        self.assertNotIn("tests", payload)
        self.assertNotIn("source", payload)
        self.assertNotIn("files", payload)
        self.assertNotIn("commands", payload)
        self.assertNotIn("mocks", payload)
        self.assertNotIn("test_empty_values", encoded)
        self.assertNotIn("from calculator import average", encoded)
        self.assertEqual(payload["expected"]["raises"], "ValueError")
        self.assertEqual(payload["allowed_files"], ["calculator.py"])

    def test_catalog_covers_platform_and_application_standards(self):
        from benchforge.adapters import configuration, generate
        from benchforge.engine import FIXTURES, public_benchmark, public_catalog
        from benchforge.patch import extract_patch, validate_patch
        from benchforge.schema import ModelContext, PublicTask

        catalog = public_catalog()
        encoded = json.dumps(catalog)
        self.assertGreaterEqual(len(catalog["benchmarks"]), 5)
        self.assertEqual(
            {item["standard"] for item in catalog["benchmarks"]},
            {"platform", "application"},
        )
        self.assertNotIn("def average", encoded)
        self.assertNotIn("from util import normalize", encoded)

        for fixture in FIXTURES.values():
            public = public_benchmark(fixture)
            self.assertNotIn("files", public)
            self.assertNotIn("tests", public)
            self.assertNotIn("mocks", public)
            snippet = fixture["files"][fixture["allowed_files"][0]][:48]
            self.assertNotIn(snippet, encoded)

            task = fixture["task"]
            output = generate(
                configuration("mock-fixed"),
                PublicTask(
                    id=task["id"],
                    title=task["title"],
                    description=task["description"],
                    constraints=task["constraints"],
                ),
                ModelContext(files=dict(fixture["files"])),
                mock_fixed=fixture["mocks"]["fixed"],
            )
            patch = extract_patch(output["text"])
            validate_patch(patch, tuple(fixture["allowed_files"]))

    def test_missing_tests_fail_fixture_load(self):
        from benchforge.engine import load_fixture

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"id": "x", "version": "1"}, handle)
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                load_fixture(path)
        finally:
            path.unlink(missing_ok=True)


class BaselineTests(unittest.TestCase):
    def test_expected_issue_failure(self):
        from benchforge.engine import baseline_issue_matches

        self.assertTrue(baseline_issue_matches({
            "exit_code": 1,
            "timeout": False,
            "stderr": "ZeroDivisionError: division by zero\nRan 1 test in 0.001s\n",
        }))

    def test_passing_issue_is_invalid_baseline(self):
        from benchforge.engine import baseline_issue_matches

        self.assertFalse(baseline_issue_matches({
            "exit_code": 0,
            "timeout": False,
            "stderr": "OK",
        }))

    def test_timeout_is_invalid_baseline(self):
        from benchforge.engine import baseline_issue_matches

        self.assertFalse(baseline_issue_matches({
            "exit_code": 1,
            "timeout": True,
            "stderr": "ZeroDivisionError\nRan 1 test\n",
        }))


class HaltMappingTests(unittest.TestCase):
    def test_cancel_overrides_docker_failure(self):
        from benchforge.engine import classify_sandbox_failure

        self.assertEqual(
            classify_sandbox_failure({"spawn_error": True, "exit_code": 125}, True),
            "CANCELLED",
        )

    def test_docker_cli_failures_are_infra(self):
        from benchforge.engine import classify_sandbox_failure

        self.assertEqual(
            classify_sandbox_failure({"spawn_error": False, "exit_code": 125}, False),
            "INFRA_ERROR",
        )
        self.assertIsNone(
            classify_sandbox_failure({"spawn_error": False, "exit_code": 1}, False)
        )


class AdapterConfigTests(unittest.TestCase):
    def _clear_provider_env(self):
        keys = (
            "OPENAI_MODEL", "PROVIDER_MODEL", "ANTHROPIC_MODEL",
            "PROVIDER_BASE_URL", "OPENAI_BASE_URL", "PROVIDER_AUTH",
        )
        previous = {key: os.environ.pop(key, None) for key in keys}
        return previous

    def _restore(self, previous):
        for key, value in previous.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_provider_requires_model(self):
        from benchforge.adapters import configuration
        from unittest.mock import patch

        previous = self._clear_provider_env()
        try:
            with patch("benchforge.adapters.load_env_file"):
                with self.assertRaises(ValueError):
                    configuration("chat-completions")
                with self.assertRaises(ValueError):
                    configuration("openai-compatible")
                with self.assertRaises(ValueError):
                    configuration("anthropic-messages")
        finally:
            self._restore(previous)

    def test_openai_alias_and_azure_query(self):
        from benchforge.adapters import configuration, _join_url
        from unittest.mock import patch

        previous = self._clear_provider_env()
        os.environ["PROVIDER_MODEL"] = "gpt-test"
        os.environ["PROVIDER_BASE_URL"] = (
            "https://example.openai.azure.com/openai/deployments/x"
            "?api-version=2024-10-21"
        )
        try:
            with patch("benchforge.adapters.load_env_file"):
                config = configuration("openai-compatible")
            self.assertEqual(config["adapter"], "chat-completions")
            self.assertIn("api-version", config["endpoint"])
            joined = _join_url(config["endpoint"], "/chat/completions")
            self.assertIn("/chat/completions?", joined)
            self.assertIn("api-version=2024-10-21", joined)
        finally:
            self._restore(previous)

    def test_rejects_embedded_credentials(self):
        from benchforge.adapters import configuration
        from unittest.mock import patch

        previous = self._clear_provider_env()
        os.environ["PROVIDER_MODEL"] = "gpt-test"
        os.environ["PROVIDER_BASE_URL"] = "https://user:pass@api.example.com/v1"
        try:
            with patch("benchforge.adapters.load_env_file"):
                with self.assertRaises(ValueError):
                    configuration("chat-completions")
        finally:
            self._restore(previous)

    def test_mock_adapter_is_demo(self):
        from benchforge.adapters import configuration

        config = configuration("mock-fixed")
        self.assertTrue(config["demo"])
        self.assertEqual(config["model"], "mock-fixed")

    def test_chat_and_anthropic_parse_provider_payloads(self):
        from unittest.mock import patch
        from benchforge.adapters import generate
        from benchforge.schema import ModelContext, PublicTask

        task = PublicTask("t", "title", "desc", [])
        context = ModelContext(files={"calculator.py": "x = 1\n"})
        chat_config = {
            "adapter": "chat-completions",
            "provider": "chat-completions",
            "model": "gpt-test",
            "endpoint": "https://api.example.com/v1",
            "auth": "bearer",
            "temperature": 0,
            "max_tokens": 32,
            "system_prompt": "sys",
        }
        anthropic_config = {
            "adapter": "anthropic-messages",
            "provider": "anthropic-messages",
            "model": "claude-test",
            "endpoint": "https://api.anthropic.com",
            "temperature": 0,
            "max_tokens": 32,
            "system_prompt": "sys",
        }

        with patch("benchforge.adapters._post_json") as post:
            post.return_value = {
                "choices": [{"message": {"content": GOOD_PATCH}}],
                "usage": {"total_tokens": 9},
                "model": "gpt-test",
            }
            result = generate(chat_config, task, context)
        self.assertEqual(result["text"], GOOD_PATCH)
        self.assertEqual(result["returned_model"], "gpt-test")

        with patch("benchforge.adapters._post_json") as post:
            post.return_value = {
                "content": [{"type": "text", "text": GOOD_PATCH}],
                "usage": {"input_tokens": 2, "output_tokens": 4},
                "model": "claude-test",
            }
            result = generate(anthropic_config, task, context)
        self.assertEqual(result["text"], GOOD_PATCH)

    def test_provider_http_error_is_runtime_error(self):
        from unittest.mock import patch
        from urllib.error import HTTPError
        from io import BytesIO
        from benchforge.adapters import _post_json

        error = HTTPError(
            "https://api.example.com/v1/chat/completions",
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(b"{}"),
        )
        with patch("benchforge.adapters.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError):
                _post_json(
                    "https://api.example.com/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x"},
                )


if __name__ == "__main__":
    unittest.main()
