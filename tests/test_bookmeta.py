"""The ISBN and cover helpers, called directly now that they are plain functions."""

import unittest

from host import bookmeta


class TestBookMeta(unittest.TestCase):
    def test_isbn_forms(self):
        self.assertEqual(bookmeta.norm_isbn("978-0-306-40615-7"), "9780306406157")
        self.assertTrue(bookmeta.valid_isbn("9780306406157"))
        self.assertTrue(bookmeta.valid_isbn("080442957X"))
        self.assertFalse(bookmeta.valid_isbn("12345"))
        self.assertFalse(bookmeta.valid_isbn(""))

    def test_a_refused_url_is_never_fetched(self):
        calls = []
        self.assertIsNone(bookmeta.cover_from_url(
            "http://127.0.0.1/c.png", allowed=lambda u: calls.append(u) or False))
        self.assertEqual(calls, ["http://127.0.0.1/c.png"])

    def test_details_use_the_fetcher_they_are_given(self):
        seen = []

        def fake(url):
            seen.append(url)
            return {"title": "T", "subtitle": "S", "publishers": ["P"],
                    "publish_date": "2001", "description": {"value": " d "}}

        out = bookmeta.details_by_isbn("978-0-306-40615-7", get_json=fake)
        self.assertEqual(out["title"], "T: S")
        self.assertEqual(out["publisher"], "P")
        self.assertEqual(out["description"], "d")
        self.assertEqual(seen, ["https://openlibrary.org/isbn/9780306406157.json"])

    def test_a_bad_isbn_makes_no_request(self):
        def boom(url):
            raise AssertionError("fetched")

        self.assertIsNone(bookmeta.details_by_isbn("nope", get_json=boom))


if __name__ == "__main__":
    unittest.main()
