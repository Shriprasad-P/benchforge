import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
