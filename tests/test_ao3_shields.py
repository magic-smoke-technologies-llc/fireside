"""AO3's fetch refusals, in words a reader can act on.

On 2026-11-17 AO3 raised its shields ("Shields are up!" behind a 403) and Cat's
import died. The link was fine. The old path reported `could not fetch that
link (HTTP 403)`, which reads like her link was wrong. These tests pin both
halves: the shields page is named as AO3's own, and an ordinary page still
imports (the positive control, so a rule that only refuses is not a fix).
"""
import unittest

from host.contracts import Refused
from host.egress_backends import HttpStatusError
from plugins.reader.ao3.plugin import Ao3, parse_ao3

URL = "https://archiveofourown.org/works/6928960/chapters/15804844"

SHIELDS_PAGE = """<!DOCTYPE html><html><head>
<title>Shields are up! | Archive of Our Own</title></head>
<body><h1>Shields are up!</h1>
<p>We are currently experiencing some technical difficulties.</p></body></html>"""

WORK_PAGE = """<html><body><h2 class="title">A Test Fic</h2>
<div id="workskin"><div id="chapter-1">
<h3 class="landmark heading" id="work">Chapter Text</h3>
<div class="userstuff"><p>Fire burns here.</p></div></div></div></body></html>"""


def _read(page=None, error=None):
    def fetch(url, cred=None, credential_hosts=()):
        if error:
            raise error
        return page
    return Ao3().read({"kind": "url", "url": URL}, fetch)


class ShieldsTest(unittest.TestCase):
    def test_shields_page_is_named_as_ao3s_own(self):
        err = HttpStatusError(403, URL, SHIELDS_PAGE)
        with self.assertRaises(Refused) as cm:
            _read(error=err)
        msg = (cm.exception.error + " " + cm.exception.detail).lower()
        self.assertIn("shields", msg)
        self.assertIn("not a problem with your link", msg)
        self.assertEqual(cm.exception.status, 502)

    def test_plain_403_still_asks_about_the_cookie(self):
        err = HttpStatusError(403, URL, "<html><body>Forbidden</body></html>")
        with self.assertRaises(Refused) as cm:
            _read(error=err)
        self.assertIn("session cookie", cm.exception.detail)

    def test_other_errors_are_not_swallowed(self):
        # A 500 is not the reader's link and not AO3's shields: let the host
        # classify it, rather than dressing it in the wrong cause.
        err = HttpStatusError(500, URL, "<html>oops</html>")
        with self.assertRaises(HttpStatusError):
            _read(error=err)

    def test_ordinary_page_still_imports(self):
        book = _read(page=WORK_PAGE)
        self.assertEqual(book.title, "A Test Fic")
        self.assertEqual(len(book.chapters), 1)
        self.assertTrue(book.chapters[0].text.startswith("Fire burns here."))

    def test_parse_refusals_are_refusals_not_500s(self):
        with self.assertRaises(Refused):
            parse_ao3("<html><body>nope</body></html>", URL)


if __name__ == "__main__":
    unittest.main()