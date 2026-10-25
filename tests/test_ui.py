"""The browser client: the page the host serves, and the routes it depends on.

Two guards that matter:

- every fetch the UI makes must be a route the host actually serves, parsed from
  the shipped HTML against the live route table. A client that calls a phantom
  endpoint is a broken deployment, and this catches it before a browser does.
- the chapter bytes the player verifies (sha256 in JS) must match the ref the
  server reports, so verify-before-play is checking the right thing.

The whole flow runs over loopback against fake plugins; no network.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request

from host.app import App, UI_DIR, GET_ROUTES, POST_ROUTES, AUTH_ROUTES, PUBLIC_ROUTES, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


class TestBrowserUi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.httpd = urllib.request  # silence linters; server started below
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, r.read()

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def _jget(self, path):
        s, b = self._get(path)
        return s, json.loads(b)

    def _wait_job(self, jid, timeout=10):
        import time
        end = time.time() + timeout
        while time.time() < end:
            s, job = self._jget("/job?id=%s" % jid)
            if job["status"] in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail("job %s did not finish" % jid)

    def test_index_is_served_and_versioned(self):
        s, body = self._get("/")
        html = body.decode("utf-8")
        self.assertEqual(s, 200)
        self.assertIn("fireside", html)
        self.assertIn("sha256hex", html)                 # the verifier ships with it
        self.assertNotIn("__HOST_VERSION__", html)       # placeholder was filled
        self.assertTrue(re.search(r"v\d+\.\d+", html))

    def test_ui_only_calls_served_routes(self):
        html = open(os.path.join(UI_DIR, "index.html"), encoding="utf-8").read()
        served = set(GET_ROUTES) | set(POST_ROUTES) | set(AUTH_ROUTES)
        calls = set(re.findall(r"""(?:fetch|api|jpost)\(\s*['"]([^'"]+)['"]""", html))
        self.assertTrue(calls, "no fetch calls found - did the page change shape?")
        for call in calls:
            base = call.split("?")[0]
            self.assertIn(base, served,
                          "the UI calls %r, which the host does not serve" % base)

    def test_sign_in_chip_is_wired(self):
        # the whole app sits behind a session: a door is served, it talks only to
        # served routes, and the signed-in chip carries the way back out.
        html = open(os.path.join(UI_DIR, "index.html"), encoding="utf-8").read()
        self.assertIn('id="auth"', html)
        self.assertIn('id="lock"', html)
        self.assertIn("/auth/policy", html)
        self.assertIn("/auth/local/login", html)
        self.assertIn("/auth/logout", html)
        self.assertIn("Continue with ", html)
        # the door must actually be shown: body.locked hides the app, so the
        # sign-in card has to be un-hidden or the page is blank for everyone
        # who is not signed in.
        self.assertIn("$('#lock').hidden = false", html)

    def test_free_signup_is_not_a_purchase(self):
        # The free tier is an account, not a cart item: a visitor must be able
        # to create one directly, and the server starts new accounts on free.
        html = open(os.path.join(UI_DIR, "index.html"), encoding="utf-8").read()
        self.assertIn('id="pagesignup"', html)
        self.assertIn("Create a free account", html)

    def test_favicon_is_served_publicly(self):
        self.assertIn("/favicon.ico", PUBLIC_ROUTES)

    def test_plans_catalog_is_public(self):
        # The sales page must load for a visitor with no session: the catalog is
        # public, and the buy button routes them through the door to an account.
        self.assertIn("/plans", PUBLIC_ROUTES)
        s, d = self._jget("/plans")
        self.assertEqual(s, 200)
        self.assertIn("tiers", d)
        s, body = self._get("/favicon.ico")
        self.assertEqual(s, 200)
        self.assertEqual(body[:4], b"\x00\x00\x01\x00")   # ICO magic

    def test_plugins_declare_speech_settings(self):
        # the fake root above ships no settings; ask the host to load the real
        # plugin tree so we are testing the shipped contracts, not the stubs.
        real = App("plugins", db_path=os.path.join(self.tmp, "db2.sqlite"),
                   storage_id="local")
        s, d = real.plugins({}, {})
        self.assertEqual(s, 200)
        ids = {p["id"] for p in d["speech"]}
        self.assertTrue({"local", "openai", "azure", "minimax"} <= ids)
        by_id = {p["id"]: p for p in d["speech"]}
        self.assertTrue(any(f["name"] == "base_url" for f in by_id["openai"]["settings"]))
        self.assertTrue(any(f["name"] == "key" for f in by_id["azure"]["settings"]))
        self.assertTrue(any(f["name"] == "group" for f in by_id["minimax"]["settings"]))
        self.assertEqual(by_id["local"]["settings"], [])
        # and the AO3 reader declares the cookie field the page renders
        ao3 = next(r for r in d["readers"] if r["id"] == "ao3")
        self.assertTrue(any(f["name"] == "cookie" for f in ao3["settings"]))

    def test_chapter_says_which_blob_it_served(self):
        """The header is the play check the page relies on.

        The listing in memory can be older than the audio after a re-encode, so
        the player verifies the bytes against the hash the server reports for
        what it just sent, and takes the newer ref - instead of failing on a
        stale size and refusing to play a chapter that is perfectly fine.
        """
        _, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        bid = imp["book_id"]
        _, r = self._post("/render", {"user": "u1", "book": bid,
                                       "provider": "fake", "voice": "vv"})
        self._wait_job(r["job_id"])
        url = ("http://127.0.0.1:%d/chapter?book=%s&idx=0&provider=fake&voice=vv"
               % (self.port, bid))
        with urllib.request.urlopen(url) as resp:
            body = resp.read()
            self.assertEqual(resp.headers.get("X-Checksum"),
                             hashlib.sha256(body).hexdigest())
            self.assertEqual(resp.headers.get("Content-Length"), str(len(body)))

    def test_browser_flow_verify_before_play(self):
        # the exact sequence the page runs: identity -> import -> voices ->
        # render -> poll -> progress -> verified chapter bytes -> position/claim
        s, imp = self._post("/import", {"user": "u1", "kind": "upload",
                                        "filename": "x.epub"})
        bid = imp["book_id"]

        s, v = self._post("/voices", {"provider": "fake", "cred": {}})
        self.assertEqual(v["voices"][0]["id"], "vv")

        s, r = self._post("/render", {"user": "u1", "book": bid,
                                      "provider": "fake", "voice": "vv"})
        job = self._wait_job(r["job_id"])
        self.assertEqual(job["status"], "done")

        s, prog = self._jget("/progress?user=u1&book=%s" % bid)
        ch = prog["chapters"][0]
        self.assertTrue(ch["rendered"])
        self.assertTrue(ch["audio_ref"])

        s, audio = self._get("/chapter?book=%s&idx=0" % bid)
        self.assertEqual(s, 200)
        # what the JS sha256 verifier checks, checked here in Python:
        self.assertEqual(hashlib.sha256(audio).hexdigest(), ch["audio_ref"])
        self.assertEqual(len(audio), ch["audio_bytes"])

        self._post("/claim", {"user": "u1", "book": bid, "device": "web-abcd"})
        self._post("/position", {"user": "u1", "book": bid, "chapter_index": 0,
                                 "offset_seconds": 4.0, "device": "web-abcd"})
        s, pos = self._jget("/position?user=u1&book=%s" % bid)
        self.assertEqual(pos["offset_seconds"], 4.0)
        self.assertEqual(pos["owner_device_id"], "web-abcd")
class TestUiDomRefs(unittest.TestCase):
    """A dead $("#id") throws at load and blanks the whole page silently.

    The element the script reaches for must exist in the shipped HTML; #user
    was removed when the free-text "listening as" field became a session label,
    and the leftover reference killed the script for every returning browser.
    """

    def test_index_html_references_only_existing_ids(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        defined = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
        refs = set(re.findall(r"\$\('#([A-Za-z0-9_\-]+)'\)", html))
        self.assertEqual(sorted(refs - defined), [],
                         "index.html references ids that do not exist")


class TestBootSafetyNet(unittest.TestCase):
    """A load-time throw must surface, and every selector form must resolve.

    The blank page had two causes stacked: a dead $('#user') reference that
    threw at load, and a signed-out branch that hid the app but never revealed
    the door. These guards cover both classes plus the generic error net.
    """

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_page_never_blanks_on_error(self):
        html = self._html()
        self.assertIn("window.addEventListener('error'", html)
        # both auth branches set the door's visibility explicitly
        self.assertIn("$('#lock').hidden = true", html)    # signed in
        self.assertIn("$('#lock').hidden = false", html)   # signed out

    def test_library_legend_waits_for_a_book(self):
        # the five-tag legend explains states of a library; on an empty one it
        # teaches nothing and eats the top of the screen, so it starts hidden
        # and is revealed with the shelf.
        html = self._html()
        self.assertIn('id="legend" style="display:none"', html)
        self.assertIn("$('#legend').style.display = books.length ? 'flex' : 'none'", html)

    def test_first_run_names_the_product(self):
        # a first-run user must be told what fireside is on the door, not only
        # in a manifest no user reads.
        html = self._html()
        # the sentence is also the translation key, so it carries data-t
        self.assertIn('data-t="Books and web stories, voiced.">Books and web stories, voiced.</p>', html)

    def test_signin_errors_are_generic(self):
        # the brief's rule: never say which half was wrong
        html = self._html()
        self.assertIn("invalid username or password.", html)
        self.assertIn("id=\"lockuser\"", html)
        self.assertIn("id=\"lockpass\"", html)
        self.assertIn("id=\"lockpass2\"", html)  # confirm field exists

    def test_id_refs_cover_all_selector_forms(self):
        html = self._html()
        defined = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
        pats = [r"\$\('#([A-Za-z0-9_\-]+)'\)",
                r"getElementById\('([A-Za-z0-9_\-]+)'\)",
                r"querySelector(?:All)?\('#([A-Za-z0-9_\-]+)'\)"]
        refs = set()
        for pat in pats:
            refs |= set(re.findall(pat, html))
        self.assertEqual(sorted(refs - defined), [],
                         "index.html reaches for ids that do not exist")


class TestClaimEndpoints(unittest.TestCase):
    """The collision-proof door over real HTTP: describe, then confirm by password."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.app.require_auth = True
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()

    def _post(self, path, obj, cookie=None):
        h = {"Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=json.dumps(obj).encode(), headers=h)
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), r.headers.get("Set-Cookie", "")

    def _get(self, path, cookie=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        if cookie:
            req.add_header("Cookie", cookie)
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def _seed_collision(self):
        from host import auth
        uid = auth.local_register(self.app.conn, "chris", "hunter2hunter2")
        auth.link_identity(self.app.conn, "google", {
            "subject": "g1", "name": "Chris", "email": "chris@example.com",
            "email_verified": True}, uid)
        return auth.create_claim(self.app.conn, "microsoft", {
            "subject": "m1", "name": "Chris", "email": "chris@example.com",
            "email_verified": True}, uid)

    def test_describe_and_confirm(self):
        token = self._seed_collision()
        s, d = self._get("/auth/claim?token=" + token)
        self.assertEqual(s, 200)
        self.assertEqual(d["provider"], "microsoft")
        self.assertTrue(d["has_password"])
        s, d, cookie = self._post("/auth/claim/confirm",
                                  {"claim": token, "password": "hunter2hunter2"})
        self.assertEqual(s, 200)
        self.assertTrue(d["ok"])
        s, me = self._get("/me", cookie=cookie)
        self.assertTrue(me["authenticated"])

    def test_wrong_password_refuses(self):
        token = self._seed_collision()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/auth/claim/confirm" % self.port,
            data=json.dumps({"claim": token, "password": "nope"}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("wrong password should be refused")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


if __name__ == "__main__":
    unittest.main()


class TestAppShell(unittest.TestCase):
    """The chrome: a user menu (settings hidden inside it), the add flow on the
    library screen, and a persistent notices ribbon with toasts and a bell."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_settings_lives_in_the_account_menu(self):
        html = self._html()
        self.assertIn('id="avatar"', html)
        self.assertIn('id="usermenu"', html)
        self.assertIn('id="menuset"', html)
        self.assertNotIn('data-v="set"', html)     # settings is not a tab
        self.assertNotIn('data-v="add"', html)     # neither is add

    def test_add_is_on_the_library_screen(self):
        html = self._html()
        lib = html.split('id="v-lib"')[1].split("</section>")[0]
        self.assertIn('id="addbtn"', lib)
        self.assertIn('id="addpanel"', lib)
        self.assertIn('id="drop"', lib)

    def test_notices_ribbon_toasts_and_bell(self):
        html = self._html()
        for anchor in ('id="ribbon"', 'id="rprog"', 'id="toasts"', 'id="bell"',
                       "bellRaise()", "toast('bad'", "toast('ok'"):
            self.assertIn(anchor, html)

    def test_connections_settings_surface_is_present(self):
        html = self._html()
        for anchor in ('id="conns"', 'id="dconn"', 'id="addconn"', 'id="cprovider"'):
            self.assertIn(anchor, html)


class TestBookPageConvert(unittest.TestCase):
    """The book page: chapters you can pick, and a convert dialog that runs all
    of them or just the ones the reader ticked."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_book_page_has_a_chapter_list_and_a_convert_dialog(self):
        html = self._html()
        self.assertIn('id="nplist"', html)       # the pickable chapter list
        self.assertIn('id="convertbtn"', html)
        self.assertIn("Convert to voice", html)
        self.assertNotIn("Voice the whole book", html)
        for anchor in ('id="convertdlg"', 'id="cvall"', 'id="cvsel"',
                       'id="cvconn"', 'id="cvvoice"'):
            self.assertIn(anchor, html)

    def test_import_lands_on_the_book_page(self):
        html = self._html()
        # a successful parse opens the book page (both the file and url paths)
        self.assertIn("openBook(d.book_id, true)", html)
        self.assertGreaterEqual(html.count("openBook(d.book_id, true)"), 2)

    def test_convert_sends_only_the_ticked_chapters(self):
        html = self._html()
        self.assertIn("body.chapters = list", html)   # subset travels to /render

    def test_chapter_list_is_cards_not_checkboxes(self):
        # the main list shows whether a chapter is voiced (and in how many
        # voices) and a button that plays it; picking for conversion lives only
        # inside the dialog now.
        html = self._html()
        self.assertIn('id="nplist"', html)
        self.assertIn("chapplay", html)               # a play button per chapter
        self.assertIn("chapterVoiceLabel", html)      # "one voice" or "N voices"
        self.assertNotIn("Select for conversion", html)   # no per-row checkbox

    def test_convert_dialog_counts_characters(self):
        html = self._html()
        self.assertIn("Whole book: ", html)
        self.assertIn("nf(bookChars)", html)
        self.assertIn("nf(selChars)", html)

    def test_book_page_has_book_settings_panel(self):
        # one settings panel holds the metadata and the cover controls; the
        # lone "Set cover" button is gone.
        html = self._html()
        for anchor in ('id="settingsbtn"', 'id="settingsdlg"', 'id="npcover"',
                       'id="npcoverimg"', 'id="sisbn"', 'id="sfetch"',
                       'id="scovprev"', 'id="scovurl"', 'id="scovfile"',
                       'id="scovautobtn"', 'id="scovrmbtn"'):
            self.assertIn(anchor, html)
        self.assertNotIn('id="coverbtn"', html)
        self.assertIn("/cover", html)                 # the shelf/book cover route
        self.assertIn("coverURL", html)
        self.assertIn("/book/details", html)          # ISBN -> metadata lookup

    def test_book_settings_can_remove_voices_and_the_book(self):
        # removing is per shelf: a voice, every voice, or the book, all from the
        # settings panel, with the routes the host actually serves.
        html = self._html()
        for anchor in ('id="scopies"', 'id="scopiesnote"', 'id="srmallbtn"',
                       'id="sdelbookbtn"', 'function renderCopies'):
            self.assertIn(anchor, html)
        self.assertIn("/book/renditions/delete", html)
        self.assertIn("/book/delete", html)


class TestVoiceOrdering(unittest.TestCase):
    """English voices sort to the top of every voice list (US, then UK) and the
    convert dialog can filter. The client cannot run here, so these are anchor
    guards: the helpers and the filter field have to ship."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_english_first_helpers_ship(self):
        html = self._html()
        for anchor in ("function isEnVoice", "function voiceRank",
                       "function enFirst", "function fillVoices",
                       "function filterSel"):
            self.assertIn(anchor, html)

    def test_convert_dialog_can_filter_voices(self):
        html = self._html()
        self.assertIn('id="cvfilter"', html)
        self.assertIn("filterSel($('#cvvoice')", html)

    def test_convert_preselects_a_voice(self):
        html = self._html()
        self.assertIn("fs_lastvoice", html)                       # remembers the last pick
        self.assertIn("chapters.find(c => c.rendered && c.voice)", html)  # the book's voice


class TestConditionalRender(unittest.TestCase):
    """A convert of a subset renders that subset and leaves the rest alone. The
    host accepts {chapters:[...]} so a reader can voice a few chapters now."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def _jget(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return json.loads(r.read())

    def test_render_subset_leaves_the_others_unvoiced(self):
        imp = self._post("/import", {"user": "u1", "kind": "upload",
                                     "filename": "x.epub"})
        bid = imp["book_id"]
        r = self._post("/render", {"user": "u1", "book": bid, "provider": "fake",
                                   "voice": "vv", "chapters": [1]})
        import time
        for _ in range(100):
            job = self._jget("/job?id=%s" % r["job_id"])
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["total"], 1)
        prog = self._jget("/progress?user=u1&book=%s" % bid)["chapters"]
        by_idx = {c["idx"]: c for c in prog}
        self.assertFalse(by_idx[0]["rendered"])
        self.assertTrue(by_idx[1]["rendered"])


class TestShelfSize(unittest.TestCase):
    """One book must not stretch to a poster.

    The shelf columns used to be a 3-way split of the page, so a single book
    filled a third of a ~880px column (a ~420px-tall cover). The columns are now
    a fixed thumbnail width and each card is capped, so one book and ten look
    the same size.
    """

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_shelf_columns_are_thumbnail_sized(self):
        html = self._html()
        self.assertIn("repeat(auto-fill,170px)", html)
        self.assertNotIn("grid-template-columns:repeat(3,1fr)", html)

    def test_book_card_is_capped(self):
        html = self._html()
        self.assertIn(
            ".bk .cover{aspect-ratio:2/3;font-size:14px;margin-bottom:8px;max-width:170px}",
            html,
        )


class TestTransportNav(unittest.TestCase):
    """A book is heard chapter by chapter, so the transport steps chapters and
    the seek unit is 15 seconds, on both the transport and the Now Playing bar.

    A 30-second skip is the wrong unit between chapters; the transport now
    carries prev/next (to the nearest chapter voiced in the current voice), and
    both seek buttons moved from 30s to 15s.
    """

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_transport_steps_chapters(self):
        html = self._html()
        self.assertIn('id="tprev"', html)
        self.assertIn('id="tnext"', html)
        self.assertIn("chapterStep(-1)", html)
        self.assertIn("chapterStep(1)", html)

    def test_seek_unit_is_fifteen_seconds(self):
        html = self._html()
        self.assertIn('title="Back 15 seconds"', html)
        self.assertIn('title="Forward 15 seconds"', html)
        self.assertNotIn("30 seconds", html)
        self.assertNotIn("\u221230s", html)
        self.assertNotIn("+30s", html)


class TestGlobalConversionProgress(unittest.TestCase):
    """Conversion progress lives in the global bar and is character-weighted: a
    filling bar, a rising character count, and chapter x/y, so a few long
    chapters do not read as almost done."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_progress_uses_done_chars_from_the_job(self):
        html = self._html()
        self.assertIn("j.done_chars", html)
        self.assertIn("bar(doneChars, totalChars)", html)

    def test_progress_shows_chapter_and_chars(self):
        html = self._html()
        self.assertIn("ch ' + j.current + '/' + j.total", html)
        self.assertIn("nf(doneChars) + ' / ' + nf(totalChars)", html)


class TestVolumePerDevice(unittest.TestCase):
    """Volume is remembered per device; the position is not (it is global)."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_volume_is_stored_and_reapplied(self):
        html = self._html()
        self.assertIn("VOLKEY", html)
        self.assertIn("localStorage.setItem(VOLKEY", html)
        self.assertIn("function applyVolume", html)
        self.assertIn("applyVolume();", html)


class TestViewLoadPlayerSplit(unittest.TestCase):
    """One player, always. Opening a book is a look, never a load; playing a
    selection makes its book the player's book; Load book is the explicit
    resume. The page and the player are separate, so browsing cannot steal your
    place."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_viewing_never_rewrites_the_last_book(self):
        html = self._html()
        # opening a book used to write it as the last book and jump to the
        # player; that write is gone.
        self.assertNotIn("lastName = id; localStorage.setItem", html)
        self.assertIn("function loadBook", html)

    def test_play_makes_its_book_the_player_book(self):
        html = self._html()
        self.assertIn("let player = { id: '', title: '', chapters: [] }", html)
        self.assertIn("async function playSelection", html)
        self.assertIn("player = { id: bk.id, title: bk.title, chapters: bk.chapters }", html)

    def test_load_book_button_is_on_the_page(self):
        html = self._html()
        self.assertIn('id="loadbook"', html)
        self.assertIn(">Load book<", html)


class TestQueue(unittest.TestCase):
    """The queue is a drawer off the transport bar: an ordered list of books
    where finishing one loads and plays the next."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_queue_drawer_and_controls(self):
        html = self._html()
        for anchor in ('id="queuedrawer"', 'id="qlist"', 'id="qplay"',
                       'id="qclear"', 'id="qclose"', 'id="queueadd"',
                       'id="tqueue"'):
            self.assertIn(anchor, html)
        self.assertIn("function queueNext", html)
        self.assertIn("QKEY", html)

    def test_end_of_book_advances_the_queue(self):
        html = self._html()
        self.assertIn("queueNext()", html)
        self.assertIn("loadBook(nqb.id)", html)


class TestUserStats(unittest.TestCase):
    """Per-user stats live behind the account chip and read from the stats
    route, not from anything the client made up."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_stats_surface_is_in_the_account_menu(self):
        html = self._html()
        self.assertIn('id="stats"', html)
        self.assertIn("function loadStats", html)
        self.assertIn("/stats?user=", html)
        self.assertIn("/listen", html)
        self.assertIn("Characters voiced", html)
        self.assertIn("Books listened", html)
        self.assertIn("Listen time", html)


class TestSelectToPlay(unittest.TestCase):
    """Select text in the reader, press one button, and the audio jumps to
    roughly that spot. The mapping is a character fraction, so the guard pins
    the seam: the button exists, it derives a fraction from the selection, and
    it seeks the one shared audio element."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_button_and_helpers_exist(self):
        html = self._html()
        self.assertIn('id="selplay"', html)
        self.assertIn("function playFromSelection", html)
        self.assertIn("function selectionCharOffset", html)
        self.assertIn("function chapterRenderedChars", html)

    def test_fraction_seek_targets_the_shared_audio_element(self):
        html = self._html()
        self.assertIn("selFrac", html)
        self.assertIn("seekFraction", html)
        self.assertIn("$a.currentTime", html)
        self.assertIn("$('#selplay').onclick = playFromSelection", html)


class TestGetStarted(unittest.TestCase):
    """A new account lands on an empty library with no idea the shape is
    book -> voice -> listen, and the voice step is invisible until it blocks
    them. The first-run card lives on that screen, ticks each step from live
    state, and gets out of the way once a chapter is voiced (or dismissed)."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_card_is_on_the_library_screen(self):
        html = self._html()
        lib = html.split('id="v-lib"')[1].split("</section>")[0]
        for anchor in ('id="welcome"', 'id="wsteps"', 'id="wdismiss"'):
            self.assertIn(anchor, lib)

    def test_steps_come_from_live_state(self):
        html = self._html()
        for anchor in ("function renderWelcome", "welcomeHidden", "WELCOME_KEY",
                       "renderWelcome();"):
            self.assertIn(anchor, html)
        self.assertIn("b.rendered > 0", html)          # voiced = a chapter exists
        self.assertIn("c.enabled !== false", html)     # a real connected voice

    def test_it_steps_aside_after_a_voiced_chapter_or_dismiss(self):
        html = self._html()
        self.assertIn("voiced) { el.hidden = true; return; }", html)
        self.assertIn("localStorage.setItem(WELCOME_KEY, USER)", html)


class TestBalanceInHeader(unittest.TestCase):
    """The balance is not a place you visit, it is a number you can see. It
    rides in the header next to the bell, it comes from /credits (the same
    single source of truth the Plan panel reads), and pressing it lands on the
    screen that explains it."""

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_the_chip_is_in_the_header(self):
        html = self._html()
        self.assertIn('id="balchip"', html)
        self.assertIn("function drawChip", html)
        self.assertIn("function refreshBalance", html)
        self.assertIn("await api('/credits')", html)

    def test_it_opens_the_plan_screen(self):
        html = self._html()
        self.assertIn("$('#balchip').onclick", html)
        self.assertIn("setSetTab('plan')", html)

    def test_it_refreshes_when_the_number_can_have_moved(self):
        html = self._html()
        # after signing in, after a conversion (a render spends characters),
        # and when the tab regains focus (another tab may be rendering).
        self.assertIn("    refreshBalance();\n    return true;", html)
        self.assertIn("renderActiveJobs(); refreshBalance();", html)
        self.assertIn("addEventListener('focus'", html)

    def test_zero_does_not_read_as_a_failure_when_nothing_is_sold(self):
        html = self._html()
        self.assertIn("avail <= 0 && !!d.sale", html)
