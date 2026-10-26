"""Pronunciation dictionaries: named, scoped, shareable.

A dictionary is one set of respellings attached at one scope. Four scopes,
resolved Book > Series > Author > Global. The point of the layer is that the
words outlive one book: a series dict means the next Arthurian book starts with
the names already right, and an exported file can be handed to someone else.

The one thing these tests are most about: a scope that cannot be established is
NOT guessed. No author means the unknown bucket; no series means the unknown
bucket. A blank field the reader can fill beats a wrong scope nobody notices.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from host import db, pron
from plugins.reader.epub.plugin import parse_epub


def conn_for(tmp):
    return db.connect(os.path.join(tmp, "d.db"))


def epub_with(meta_xml=""):
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr("META-INF/container.xml",
               '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
               '</rootfiles></container>')
    z.writestr("OEBPS/content.opf",
               '<?xml version="1.0"?><package><metadata '
               'xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<dc:title>Names</dc:title>' + meta_xml + '</metadata><manifest>'
               '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
               '</manifest><spine><itemref idref="c1"/></spine></package>')
    z.writestr("OEBPS/c1.xhtml",
               "<html><body><h1>One</h1><p>" + ("hello " * 20) + "</p></body></html>")
    z.close()
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-dict-")
        self.conn = conn_for(self.tmp)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def book(self, bid, author="", series=""):
        self.conn.execute(
            "INSERT INTO books(id,user_id,title,author,series_name,created_at)"
            " VALUES(?,?,?,?,?,?)", (bid, "u1", "T " + bid, author, series, "t"))
        self.conn.commit()


class TestScopes(Base):
    def test_four_scopes_resolve_narrowest_last(self):
        self.book("b1", author="T. H. White", series="Once and Future")
        pron.save_dict(self.conn, "u1", "G", "global", "",
                       [{"from": "Bedivere", "to": "BAD-ih-veer"},
                        {"from": "Kay", "to": "Kai"}])
        pron.save_dict(self.conn, "u1", "A", "author", "T. H. White",
                       [{"from": "Bedivere", "to": "bed-EE-veer"}])
        pron.save_dict(self.conn, "u1", "S", "series", "Once and Future",
                       [{"from": "Bedivere", "to": "BED-eh-veer"}])
        pron.save_dict(self.conn, "u1", "T", "book", "b1",
                       [{"from": "Bedivere", "to": "BED-ver"}])
        rules = pron.load(self.conn, "u1")
        got = {r["from"]: r["to"] for r in
               pron.for_book(rules, "b1", "T. H. White", ["Once and Future"])}
        self.assertEqual(got["Bedivere"], "BED-ver")     # the book's own word wins
        self.assertEqual(got["Kay"], "Kai")              # global still reaches it
        # one layer up, the series wins over the author and the global
        got = {r["from"]: r["to"] for r in
               pron.for_book(rules, "b9", "T. H. White", ["Once and Future"])}
        self.assertEqual(got["Bedivere"], "BED-eh-veer")
        # author scope alone
        got = {r["from"]: r["to"] for r in
               pron.for_book(rules, "b9", "T. H. White", [])}
        self.assertEqual(got["Bedivere"], "bed-EE-veer")
        # nobody in particular
        got = {r["from"]: r["to"] for r in
               pron.for_book(rules, "b9", "Someone Else", ["Other Shelf"])}
        self.assertEqual(got["Bedivere"], "BAD-ih-veer")

    def test_scope_without_a_key_is_refused(self):
        self.assertIsNone(pron.save_dict(self.conn, "u1", "A", "author", "",
                                        [{"from": "x", "to": "y"}]))
        self.assertEqual(pron.dicts(self.conn, "u1"), [])
        # an unknown scope falls back to global rather than inventing a fifth
        got = pron.save_dict(self.conn, "u1", "A", "nonsense", "",
                             [{"from": "x", "to": "y"}])
        self.assertEqual(got["scope"], "global")

    def test_unknown_bucket_catches_a_book_with_no_author(self):
        self.book("b1")
        pron.save_dict(self.conn, "u1", "no author", "author", pron.UNKNOWN_AUTHOR,
                       [{"from": "Qui-Gon", "to": "Kwai-Gon"}])
        rules = pron.load(self.conn, "u1")
        got = pron.for_book(rules, "b1", "", [])
        self.assertEqual([r["to"] for r in got], ["Kwai-Gon"])
        # a book that does name its author does not fall into the bucket
        self.assertEqual(pron.for_book(rules, "b2", "Mira", []), [])

    def test_unknown_bucket_catches_a_book_with_no_series(self):
        pron.save_dict(self.conn, "u1", "no series", "series", pron.UNKNOWN_SERIES,
                       [{"from": "Kay", "to": "Kai"}])
        rules = pron.load(self.conn, "u1")
        self.assertEqual(len(pron.for_book(rules, "b1", "Mira", [])), 1)
        self.assertEqual(pron.for_book(rules, "b1", "Mira", ["A Shelf"]), [])

    def test_two_dicts_on_one_scope_both_apply(self):
        pron.save_dict(self.conn, "u1", "one", "global", "",
                       [{"from": "Kay", "to": "Kai"}])
        pron.save_dict(self.conn, "u1", "two", "global", "",
                       [{"from": "Bedivere", "to": "BED-ver"}])
        got = {r["from"] for r in pron.for_book(pron.load(self.conn, "u1"), "b1", "")}
        self.assertEqual(got, {"Kay", "Bedivere"})


class TestFiles(Base):
    def test_export_then_import_is_the_words_not_the_scope(self):
        pron.save_dict(self.conn, "u1", "Arthurian", "book", "b1",
                       [{"from": "Bedivere", "to": "BED-ver"}])
        d = pron.dicts(self.conn, "u1")[0]
        blob = json.dumps(pron.export_dict(self.conn, "u1", d["id"]))
        other = conn_for(tempfile.mkdtemp(prefix="fireside-dict2-"))
        got = pron.import_dict(other, "u2", blob)
        self.assertEqual(got["rules"], [{"from": "Bedivere", "to": "BED-ver"}])
        # the file's book id means nothing on another account, so it lands global
        self.assertEqual(got["scope"], "global")
        self.assertEqual(got["scope_key"], "")
        # and can be re-attached to this reader's own series in one move
        got = pron.import_dict(other, "u2", blob, scope="series", scope_key="Mine")
        self.assertEqual(got["scope_key"], "Mine")
        other.close()

    def test_junk_import_is_refused_not_stored(self):
        self.assertIsNone(pron.import_dict(self.conn, "u1", "not json"))
        self.assertIsNone(pron.import_dict(self.conn, "u1", {"rules": "nope"}))
        self.assertEqual(pron.dicts(self.conn, "u1"), [])

    def test_duplicate_words_inside_one_dict_collapse(self):
        got = pron.save_dict(self.conn, "u1", "x", "global", "",
                             [{"from": "Kay", "to": "Kai"},
                              {"from": "kay", "to": "KAY"},
                              {"from": "", "to": "nothing"}])
        self.assertEqual(got["rules"], [{"from": "Kay", "to": "Kai"}])


class TestMigration(Base):
    def test_old_flat_list_becomes_dictionaries_and_is_cleared(self):
        old = [{"from": "Kay", "to": "Kai", "book": "", "author": ""},
               {"from": "Bedivere", "to": "BED-ver", "book": "b1", "author": ""},
               {"from": "Gawain", "to": "GOW-in", "book": "", "author": "White"}]
        self.conn.execute(
            "INSERT INTO user_prefs(user_id,pron,updated_at) VALUES(?,?,?)",
            ("u1", json.dumps(old), "t"))
        self.conn.commit()
        made = pron.migrate(self.conn, "u1")
        self.assertEqual(made, 3)
        scopes = sorted((d["scope"], d["scope_key"]) for d in pron.dicts(self.conn, "u1"))
        self.assertEqual(scopes, [("author", "White"), ("book", "b1"), ("global", "")])
        # the same words still come out, and nothing is left in the old slot
        got = {r["from"]: r["to"] for r in pron.load(self.conn, "u1")}
        self.assertEqual(got, {"Kay": "Kai", "Bedivere": "BED-ver", "Gawain": "GOW-in"})
        self.assertEqual(pron.migrate(self.conn, "u1"), 0)
        row = self.conn.execute("SELECT pron FROM user_prefs WHERE user_id=?",
                                ("u1",)).fetchone()
        self.assertEqual((row["pron"] or ""), "")

    def test_migration_does_not_run_over_existing_dicts(self):
        self.conn.execute(
            "INSERT INTO user_prefs(user_id,pron,updated_at) VALUES(?,?,?)",
            ("u1", json.dumps([{"from": "Kay", "to": "Kai"}]), "t"))
        self.conn.commit()
        pron.save_dict(self.conn, "u1", "mine", "global", "", [{"from": "A", "to": "B"}])
        pron.migrate(self.conn, "u1")
        self.assertEqual(len(pron.dicts(self.conn, "u1")), 1)


class TestFileMetadata(unittest.TestCase):
    def test_epub_author_and_calibre_series_are_read(self):
        b = parse_epub(epub_with('<dc:creator>T. H. White</dc:creator>'
                                 '<meta name="calibre:series" content="Once and Future"/>'
                                 '<meta name="calibre:series_index" content="2"/>'),
                       "t.epub")
        self.assertEqual(b["author"], "T. H. White")
        self.assertEqual(b["series"], "Once and Future")
        self.assertEqual(b["series_index"], "2")

    def test_book_without_metadata_claims_nothing(self):
        b = parse_epub(epub_with(), "t.epub")
        for key in ("author", "series", "series_index"):
            self.assertNotIn(key, b)

    def test_a_series_in_the_title_is_not_a_series(self):
        buf = epub_with("")
        b = parse_epub(buf, "Book 2 of the Once and Future.epub")
        self.assertNotIn("series", b)


if __name__ == "__main__":
    unittest.main()


class TestDictRoutes(unittest.TestCase):
    """The dictionary over the host's own routes, not just the module.

    A module that behaves and a route that never calls it is the shape that has
    bitten this project before (a sale machine built, green, and switched off by
    a deploy variable), so this one goes through the app.
    """

    def setUp(self):
        from host.app import App
        from tests.test_pluginhost import write_plugin
        tmp = tempfile.mkdtemp(prefix="fireside-dictapp-")
        root = os.path.join(tmp, "plugins")
        write_plugin(root, "storage", "mem", body=(
            "import hashlib\n"
            "class Plugin:\n"
            "    def __init__(self): self._d = {}\n"
            "    def put(self, data):\n"
            "        r = hashlib.sha256(data).hexdigest(); self._d[r] = data; return r\n"
            "    def has(self, ref): return ref in self._d\n"
            "    def get(self, ref): return self._d[ref]\n"
            "    def delete(self, ref): self._d.pop(ref, None)\n"
            "    def info(self): return {'kind': 'memory', 'durable': False}\n"))
        self.app = App(root, db_path=os.path.join(tmp, "a.db"),
                       storage_id="mem", require_auth=False)

    def test_round_trip_through_the_routes(self):
        q = {"user": "u1"}
        st, out = self.app.pron_dict_save(q, {"name": "Arthurian", "scope": "global",
                                              "rules": [{"from": "Kay", "to": "Kai"}]})
        self.assertEqual(st, 200)
        did = out["dict"]["id"]
        st, out = self.app.pron_dicts(q, {})
        self.assertEqual(st, 200)
        self.assertEqual([d["name"] for d in out["dicts"]], ["Arthurian"])
        self.assertEqual(out["unknown"]["author"], pron.UNKNOWN_AUTHOR)
        st, out = self.app.pron_dicts({"user": "u1", "id": did}, {})
        self.assertEqual(out["format"], pron.DICT_FORMAT)
        st, out = self.app.pron_get(q, {})
        self.assertEqual([r["from"] for r in out["rules"]], ["Kay"])
        st, _ = self.app.pron_dict_delete(q, {"id": did})
        self.assertEqual(st, 200)
        self.assertEqual(pron.dicts(self.app.conn, "u1"), [])

    def test_a_scope_with_no_key_is_a_400_not_a_silent_global(self):
        st, out = self.app.pron_dict_save({"user": "u1"},
                                          {"name": "S", "scope": "series", "rules": []})
        self.assertEqual(st, 400)
        self.assertEqual(pron.dicts(self.app.conn, "u1"), [])
