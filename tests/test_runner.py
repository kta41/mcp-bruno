import unittest

from bruno_mcp.runner import BrunoRunner


class EmbedQueryParamsInUrlTests(unittest.TestCase):
    def test_does_not_double_encode_already_encoded_values(self) -> None:
        runner = BrunoRunner()

        lines = ["  url: https://example.test/items"]
        updated = runner._embed_query_params_in_url(
            lines,
            {"fromPublishedAt": "2021-01-01T00%3A00%3A00Z"},
        )

        self.assertEqual(
            updated,
            ["  url: https://example.test/items?fromPublishedAt=2021-01-01T00:00:00Z"],
        )

    def test_does_not_double_encode_lowercase_percent_escapes(self) -> None:
        runner = BrunoRunner()

        lines = ["  url: https://example.test/items"]
        updated = runner._embed_query_params_in_url(
            lines,
            {"fromPublishedAt": "2021-01-01T00%3a00%3a00Z"},
        )

        self.assertEqual(
            updated,
            ["  url: https://example.test/items?fromPublishedAt=2021-01-01T00:00:00Z"],
        )

    def test_encodes_raw_values_once(self) -> None:
        runner = BrunoRunner()

        lines = ["  url: https://example.test/items?sortBy=UPDATED"]
        updated = runner._embed_query_params_in_url(
            lines,
            {"fromPublishedAt": "2021-01-01T00:00:00Z"},
        )

        self.assertEqual(
            updated,
            ["  url: https://example.test/items?sortBy=UPDATED&fromPublishedAt=2021-01-01T00:00:00Z"],
        )


if __name__ == "__main__":
    unittest.main()