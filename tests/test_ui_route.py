"""Back and forward inside the app.

Cat listens on autopilot and hits the browser's Back to leave the book, and the
whole page was one history entry, so Back left fireside and landed her on
whatever she had open before. These tests hold the three things that fix it:

- every view is a place, written to the hash (never the path: `/library` is the
  library API and `/plans` is the price list, so a path route collides),
- a Back press is applied to the page instead of being pushed again,
- the app writes its first place at boot, so the first Back stays inside it.
"""
import os
import re
import unittest

from host.app import GET_ROUTES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")


def html():
    with open(UI, encoding="utf-8") as f:
        return f.read()


class TestHistoryRouting(unittest.TestCase):
    def test_back_and_hash_edits_are_both_applied(self):
        page = html()
        self.assertIn("addEventListener('popstate'", page)
        self.assertIn("addEventListener('hashchange'", page)
        self.assertIn("async function applyRoute()", page)

    def test_every_view_has_a_place(self):
        page = html()
        routes = re.search(r"const ROUTE_OF = \{[^}]*\}", page).group(0)
        for view in re.findall(r'<button data-v="([a-z]+)"', page):
            self.assertIn(view + ":", routes, "the %s tab has no place" % view)
        self.assertIn("set: 'settings'", routes)
        back = re.search(r"const VIEW_OF = \{[^}]*\}", page).group(0)
        for name in re.findall(r": '([a-z]+)'", routes):
            self.assertIn(name + ":", back, "a place that cannot be read back")

    def test_places_are_hashes_and_never_server_paths(self):
        # the guard behind the whole design: a path route would hit a real API
        self.assertIn("/library", GET_ROUTES)
        self.assertIn("/plans", GET_ROUTES)
        page = html()
        hashes = re.findall(r"return '#/([a-z]+)", page)
        self.assertTrue(hashes, "no route is built from the hash")
        for path in ("pushState", "replaceState"):
            for call in re.findall(path + r"\([^)]*'([^']*)'", page):
                self.assertFalse(call.startswith("/") and call not in ("/",),
                                 "%s writes the path %r" % (path, call))

    def test_opening_a_view_writes_one_place(self):
        page = html()
        show = re.search(r"function show\(v, mode\) \{(.*?)\n\}", page, re.S).group(1)
        self.assertIn("syncRoute(", show, "a view change is not a place")
        # correcting an entry must never stack a second one
        self.assertIn("syncRoute(mode === 'replace' ? 'replace' : undefined)", show)

    def test_a_book_is_named_in_the_url_only_once_it_is_open(self):
        # clicking a book shows the play view first and loads after, so the URL
        # it wrote can still name the previous book. openBook corrects it.
        page = html()
        open_fn = re.search(r"async function openBook\(id, jumpPlay\) \{(.*?)\n\}", page, re.S).group(1)
        self.assertIn("renderPlay();", open_fn)
        self.assertIn("syncRoute('replace')", open_fn,
                      "the URL keeps a stale book id after the load")

    def test_boot_owns_the_first_entry(self):
        page = html()
        boot = page.split("window.addEventListener('load'", 1)[1]
        self.assertIn("history.replaceState({ route: '#/library' }", boot,
                      "no first entry: the first Back leaves the site")
        self.assertIn("BOOTING = false", boot)
        # a deep link keeps its own place instead of being replaced by the library
        self.assertIn("if (/^#\\//.test(location.hash", boot)

    def test_a_back_press_does_not_re_push_the_same_place(self):
        page = html()
        sync = re.search(r"function syncRoute\(mode\) \{(.*?)\n\}", page, re.S).group(1)
        self.assertIn("if (h === ROUTE_LAST) return;", sync)
        apply_fn = re.search(r"async function applyRoute\(\) \{(.*?)\n\}", page, re.S).group(1)
        self.assertIn("APPLYING_ROUTE = true", apply_fn)
        self.assertIn("APPLYING_ROUTE = false", apply_fn)

    def test_a_deep_link_opens_the_book_it_names(self):
        page = html()
        apply_fn = re.search(r"async function applyRoute\(\) \{(.*?)\n\}", page, re.S).group(1)
        self.assertIn("await openBook(id, true)", apply_fn,
                      "a saved #/play/<id> link lands on an empty page")
        # and boot does not open a different book first
        boot = page.split("window.addEventListener('load'", 1)[1]
        self.assertIn("if (lastName && lastName !== deepBook)", boot)


if __name__ == "__main__":
    unittest.main()