"""Redaction contract: secret values and secret-shaped keys must never leak into outputs."""

import unittest

from bruno_mcp.redaction import mask_bru_args, mask_value, preview_response_data, sha_prefix


class MaskValueTests(unittest.TestCase):
    def test_short_values_are_fully_masked(self) -> None:
        self.assertEqual(mask_value("abc123"), "******")

    def test_long_values_show_only_edges(self) -> None:
        masked = mask_value("super-secret-token-value")
        self.assertTrue(masked.startswith("sup"))
        self.assertTrue(masked.endswith("lue"))
        self.assertNotIn("secret-token", masked)

    def test_empty_value(self) -> None:
        self.assertEqual(mask_value(""), "<empty>")


class MaskBruArgsTests(unittest.TestCase):
    def test_env_var_values_are_masked(self) -> None:
        args = ["bru", "run", "--env-var", "TOKEN=super-secret-value", "--reporter-json", "/tmp/out.json"]
        masked = mask_bru_args(args)
        self.assertNotIn("super-secret-value", " ".join(masked))
        self.assertIn("--env-var", masked)

    def test_secret_named_assignments_are_masked(self) -> None:
        masked = mask_bru_args(["--password=hunter2hunter2"])
        self.assertNotIn("hunter2hunter2", masked[0])

    def test_non_secret_args_pass_through(self) -> None:
        masked = mask_bru_args(["run", "--env", "dev", "collection"])
        self.assertEqual(masked, ["run", "--env", "dev", "collection"])


class PreviewRedactionTests(unittest.TestCase):
    def test_secret_keys_are_redacted_at_any_depth(self) -> None:
        payload = {"items": [{"id": 1, "accessToken": "abcdef"}], "nested": {"deep": {"api_key": "xyz"}}}
        preview = preview_response_data(payload)
        self.assertNotIn("abcdef", repr(preview))
        self.assertNotIn("xyz", repr(preview))

    def test_previews_are_bounded(self) -> None:
        payload = {"data": ["x" * 1000]}
        preview = preview_response_data(payload)
        self.assertLess(len(repr(preview)), 1000)

    def test_non_secret_scalars_pass_through(self) -> None:
        preview = preview_response_data({"total": 3, "name": "items"})
        self.assertEqual(preview["total"], 3)
        self.assertEqual(preview["name"], "items")


class ShaPrefixTests(unittest.TestCase):
    def test_prefix_is_deterministic_and_short(self) -> None:
        value = sha_prefix("some-token")
        self.assertEqual(value, sha_prefix("some-token"))
        self.assertEqual(len(value), 16)
        self.assertNotEqual(value, sha_prefix("other-token"))


if __name__ == "__main__":
    unittest.main()
