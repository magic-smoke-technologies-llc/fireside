"""Series: a named local container of books in an order, and hand-edited
metadata that outranks a later lookup."""

import tempfile
import unittest

from host.contracts import Book, Chapter
from host import library
from host.db import connect
from storage.local import LocalBackend


def _book(title="Seed"):
    return Book(title=title, chapters=[Chapter("Ch", "text of " + title)])


class TestSeries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-series-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        library.create_user(self.conn, "u2")

    def add(self, title, user="u1"):
        return library.add_book(self.conn, self.storage, user, _book(title))

    # ---- the container -----------------------------------------------------

    def test_a_new_account_has_no_series(self):
        self.assertEqual(library.list_series(self.conn, "u1"), [])

    def test_create_rename_delete(self):
        sid = library.create_series(self.conn, "u1", "  The  Wheel ")
        self.assertTrue(sid)
        rows = library.list_series(self.conn, "u1")
        self.assertEqual([r["name"] for r in rows], ["The Wheel"])   # whitespace folded
        self.assertTrue(library.rename_series(self.conn, "u1", sid, "Wheel of Time"))
        self.assertEqual(library.list_series(self.conn, "u1")[0]["name"], "Wheel of Time")
        self.assertTrue(library.delete_series(self.conn, "u1", sid))
        self.assertEqual(library.list_series(self.conn, "u1"), [])

    def test_a_series_needs_a_name(self):
        self.assertIsNone(library.create_series(self.conn, "u1", "   "))

    def test_one_account_cannot_touch_anothers_series(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        self.assertFalse(library.rename_series(self.conn, "u2", sid, "Yours"))
        self.assertFalse(library.delete_series(self.conn, "u2", sid))
        self.assertEqual(library.list_series(self.conn, "u1")[0]["name"], "Mine")

    def test_deleting_a_series_never_deletes_a_book(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        bid = self.add("One")
        library.set_book_series(self.conn, "u1", bid, sid)
        library.delete_series(self.conn, "u1", sid)
        self.assertIsNotNone(library.get_book(self.conn, bid))
        self.assertEqual(library.series_of(self.conn, bid), [])

    # ---- membership and order ----------------------------------------------

    def test_books_append_to_the_end(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        a, b, c = self.add("A"), self.add("B"), self.add("C")
        for bid in (a, b, c):
            self.assertTrue(library.set_book_series(self.conn, "u1", bid, sid))
        rows = self.conn.execute(
            "SELECT book_id FROM series_books WHERE series_id=? ORDER BY place",
            (sid,)).fetchall()
        self.assertEqual([r["book_id"] for r in rows], [a, b, c])
        self.assertEqual(library.series_of(self.conn, c)[0]["place"], 3)
        self.assertEqual(library.list_series(self.conn, "u1")[0]["books"], 3)

    def test_a_place_is_one_based_and_the_first_book_is_place_one(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        a = self.add("A")
        library.set_book_series(self.conn, "u1", a, sid)
        self.assertEqual(library.series_of(self.conn, a)[0]["place"], 1)

    def test_move_swaps_neighbours_and_stops_at_the_ends(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        a, b, c = self.add("A"), self.add("B"), self.add("C")
        for bid in (a, b, c):
            library.set_book_series(self.conn, "u1", bid, sid)
        self.assertTrue(library.move_in_series(self.conn, "u1", sid, c, -1))
        order = lambda: [r["book_id"] for r in self.conn.execute(
            "SELECT book_id FROM series_books WHERE series_id=? ORDER BY place", (sid,))]
        self.assertEqual(order(), [a, c, b])
        self.assertTrue(library.move_in_series(self.conn, "u1", sid, a, -1) is False)
        self.assertTrue(library.move_in_series(self.conn, "u1", sid, b, 1) is False)
        self.assertEqual(order(), [a, c, b])
        self.assertEqual(library.series_of(self.conn, c)[0]["place"], 2)

    def test_positions_stay_contiguous_after_a_removal(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        a, b, c = self.add("A"), self.add("B"), self.add("C")
        for bid in (a, b, c):
            library.set_book_series(self.conn, "u1", bid, sid)
        library.set_book_series(self.conn, "u1", b, "")
        places = [r["place"] for r in self.conn.execute(
            "SELECT place FROM series_books WHERE series_id=? ORDER BY place", (sid,))]
        self.assertEqual(places, [0, 1])
        self.assertEqual(library.series_of(self.conn, c)[0]["place"], 2)

    def test_joining_a_second_series_leaves_the_first(self):
        # one series per book this release; the row is what allows more later
        s1 = library.create_series(self.conn, "u1", "One")
        s2 = library.create_series(self.conn, "u1", "Two")
        a = self.add("A")
        library.set_book_series(self.conn, "u1", a, s1)
        library.set_book_series(self.conn, "u1", a, s2)
        self.assertEqual([s["id"] for s in library.series_of(self.conn, a)], [s2])

    def test_membership_needs_your_own_book_and_series(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        other = self.add("Theirs", user="u2")
        self.assertFalse(library.set_book_series(self.conn, "u1", other, sid))
        mine = self.add("Mine")
        foreign = library.create_series(self.conn, "u2", "Not yours")
        self.assertFalse(library.set_book_series(self.conn, "u1", mine, foreign))
        self.assertEqual(library.series_of(self.conn, mine), [])

    def test_reorder_never_touches_the_book_or_its_text(self):
        sid = library.create_series(self.conn, "u1", "Mine")
        a, b = self.add("A"), self.add("B")
        library.set_book_series(self.conn, "u1", a, sid)
        library.set_book_series(self.conn, "u1", b, sid)
        before = library.get_book(self.conn, a)
        library.move_in_series(self.conn, "u1", sid, b, -1)
        after = library.get_book(self.conn, a)
        self.assertEqual(before["title"], after["title"])
        self.assertEqual(library.chapter_text(self.conn, self.storage, a, 0), "text of A")


class TestUserEditedMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-meta-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")
        self.bid = library.add_book(self.conn, self.storage, "u1", _book())

    def test_a_lookup_leaves_the_flag_alone(self):
        library.set_details(self.conn, self.bid, author="A Lookup")
        self.assertEqual(library.get_book(self.conn, self.bid)["user_edited"], 0)

    def test_a_hand_edit_raises_the_flag(self):
        library.set_user_details(self.conn, self.bid, title="Mine", author="Me")
        b = library.get_book(self.conn, self.bid)
        self.assertEqual((b["title"], b["author"], b["user_edited"]), ("Mine", "Me", 1))

    def test_the_flag_survives_a_later_lookup(self):
        library.set_user_details(self.conn, self.bid, title="Mine")
        library.set_details(self.conn, self.bid, publisher="Someone")
        a = library.get_book(self.conn, self.bid)
        self.assertEqual((a["title"], a["user_edited"]), ("Mine", 1))


# ---- over HTTP ------------------------------------------------------------
# The library tests call functions. These go through the real server, because a
# panel is only finished when the route it calls answers the way it expects.

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host.app import App, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


class TestSeriesRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.storage = self.app.host.storage["mem"].obj
        library.create_user(self.app.conn, "u1")
        self.books = [library.add_book(self.app.conn, self.storage, "u1", _book(t))
                      for t in ("A", "B")]

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return json.loads(r.read())

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(dict(obj, user="u1")).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_the_dropdown_starts_empty_and_create_round_trips(self):
        self.assertEqual(self._get("/series?user=u1")["series"], [])
        made = self._post("/series", {"op": "create", "name": "Saga"})
        self.assertEqual([s["name"] for s in made["series"]], ["Saga"])
        sid = made["series"][0]["id"]
        self.assertEqual(len(self._post("/series", {"op": "rename", "id": sid,
                                                    "name": "Long Saga"})["series"]), 1)
        self.assertEqual(self._get("/series?user=u1")["series"][0]["name"], "Long Saga")

    def test_a_string_instead_of_json_is_a_400(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/series" % self.port, data=b"nope",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 400)

    def test_put_a_book_in_and_the_payload_carries_the_place(self):
        sid = self._post("/series", {"op": "create", "name": "Saga"})["series"][0]["id"]
        got = self._post("/book/series", {"book": self.books[0], "series": sid})
        self.assertEqual(got["book"]["series"][0]["place"], 1)
        got = self._post("/book/series", {"book": self.books[1], "series": sid})
        self.assertEqual(got["book"]["series"][0]["place"], 2)
        self.assertEqual(got["book"]["series"][0]["count"], 2)

    def test_move_over_http_and_the_order_flips(self):
        sid = self._post("/series", {"op": "create", "name": "Saga"})["series"][0]["id"]
        for b in self.books:
            self._post("/book/series", {"book": b, "series": sid})
        got = self._post("/series/move", {"series": sid, "book": self.books[1], "dir": -1})
        self.assertTrue(got["moved"])
        self.assertEqual(got["book"]["series"][0]["place"], 1)
        stuck = self._post("/series/move", {"series": sid, "book": self.books[1], "dir": -1})
        self.assertFalse(stuck["moved"])

    def test_the_shelf_carries_the_series_place_and_count(self):
        """The shelf is where a series has to be visible as a container, so
        /library must say which series a book is in and where, or the client
        can only find out one book at a time."""
        sid = self._post("/series", {"op": "create", "name": "Saga"})["series"][0]["id"]
        for b in self.books:
            self._post("/book/series", {"book": b, "series": sid})
        shelf = self._get("/library?user=u1")
        by_id = {b["id"]: b for b in shelf}
        self.assertEqual(by_id[self.books[0]]["series"][0]["name"], "Saga")
        self.assertEqual(by_id[self.books[0]]["series"][0]["place"], 1)
        self.assertEqual(by_id[self.books[1]]["series"][0]["place"], 2)
        self.assertEqual(by_id[self.books[1]]["series"][0]["count"], 2)

    def test_a_loose_book_says_no_series_on_the_shelf(self):
        self.assertEqual(self._get("/library?user=u1")[0]["series"], [])

    def test_a_hand_edit_raises_user_edited_and_unlocks(self):
        got = self._post("/book/meta", {"book": self.books[0], "title": "Mine",
                                        "author": "Me"})
        self.assertEqual((got["book"]["title"], got["book"]["author"],
                          got["book"]["user_edited"]), ("Mine", "Me", 1))

    def _post_raw(self, path, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    def test_someone_elses_book_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self._post_raw("/book/meta", {"book": self.books[0], "title": "Hijack",
                                          "user": "u2"})
        self.assertEqual(e.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self._post_raw("/book/series", {"book": self.books[0], "series": "",
                                            "user": "u2"})
        self.assertEqual(e.exception.code, 404)

    def test_a_blank_title_is_refused_and_an_author_can_be_cleared(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self._post("/book/meta", {"book": self.books[0], "title": "   "})
        self.assertEqual(e.exception.code, 400)
        got = self._post("/book/meta", {"book": self.books[0], "author": ""})
        self.assertEqual(got["book"]["author"], "")
