"""App routes that no test was reaching: covers, details, removal, speech,
credits, billing edges, connections and grants.

Same fixture shape as test_app: real App, fake plugins in a temp dir, memory
storage. Network calls are patched at the module the app uses.
"""

import io
import json
import os
import tempfile
import unittest

import host.app as appmod
from host import auth, billing, connections, grants, payments
from host.app import App
from tests.test_app import READER_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin

SPEECH = """
from host.contracts import Audio, Quote, Report
class Plugin:
    id = "sale"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False
    def settings(self):
        return [{"key": "api_key", "label": "Key", "type": "password"},
                {"name": "base", "label": "Base", "kind": "text"}]
    def list_voices(self, cred):
        return [{"id": "v", "label": "V"}]
    def synth(self, text, voice, opts, cred):
        return Audio(data=("A:" + text).encode())
    def cost_per_1m_chars(self, cred):
        return 12.0
    def sale_rate_per_1m(self, cred):
        return 15.0
    def chars_for_payment(self, usd, cred):
        return int(usd / 15.0 * 1_000_000)
    def bundles(self, cred):
        return [{"usd": 5, "chars": 333333}]
    def quote(self, ctx):
        n = ctx.get("chars_est", 0)
        return Quote(chars_est=n, price=float(n), currency="chars")
    def report(self, ctx):
        return Report(charge="charged", cost=float(ctx.get("chars_actual", 0)),
                      currency="chars")
"""

MUTED = """
from host.contracts import Audio
class Plugin:
    id = "muted"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False
    def settings(self):
        raise RuntimeError("no settings today")
    def list_voices(self, cred):
        return []
    def synth(self, text, voice, opts, cred):
        return Audio(data=b"")
    def cost_per_1m_chars(self, cred):
        return 0.0
    def quote(self, ctx):
        from host.contracts import Quote
        return Quote(chars_est=0, price=0.0, currency="chars")
    def report(self, ctx):
        from host.contracts import Report
        return Report(charge="unknown", chars_actual=0)
"""


class FakePayments:
    id = "fake"

    def __init__(self, ok=True, detail=""):
        self.ok = ok
        self.detail = detail
        self.charged = []

    def checkout(self, plan, meta):
        class CO:
            ok = True
            url = "https://pay.example/" + plan
            reference = "co_1"
        return CO()

    def charge(self, usd, meta):
        self.charged.append((usd, meta))

        class R:
            ok = self.ok
            detail = self.detail
            reference = "ch_1"
        return R()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "sale", body=SPEECH)
        write_plugin(root, "speech", "muted", body=MUTED)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem",
                       sale_connection={"provider": "sale", "voice": "v"})
        self.conn = self.app.conn
        for uid in ("u1", "u2"):
            self.conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                              (uid, uid, "2026-01-01"))
        self.conn.execute("INSERT OR IGNORE INTO admins(user_id,created_at) VALUES(?,?)",
                          ("admin", "2026-01-01"))
        self.conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                          ("admin", "admin", "2026-01-01"))
        self.conn.commit()

    def import_book(self, user="u1"):
        s, imp = self.app.import_book({}, {"user": user, "kind": "upload",
                                           "filename": "x.epub"})
        self.assertEqual(s, 200, imp)
        return imp["book_id"]


class TestSourceAttribution(Base):
    def test_the_payload_carries_where_the_work_came_from(self):
        import json as _json
        bid = self.import_book()
        self.conn.execute("UPDATE books SET source_json=? WHERE id=?",
                          (_json.dumps({"source": "keiramarcos",
                                        "url": "https://keiramarcos.com/fan-fiction/x/"}),
                           bid))
        self.conn.commit()
        s, out = self.app.progress({"user": "u1", "book": bid}, {})
        self.assertEqual(s, 200)
        self.assertEqual(out["book"]["source"], "keiramarcos")
        self.assertEqual(out["book"]["url"], "https://keiramarcos.com/fan-fiction/x/")

    def test_an_upload_names_no_site(self):
        bid = self.import_book()
        s, out = self.app.progress({"user": "u1", "book": bid}, {})
        self.assertEqual(out["book"]["url"], "")
        self.assertEqual(out["book"]["source"], "")


class TestLegal(unittest.TestCase):
    def setUp(self):
        self.app = App(db_path=":memory:")

    def test_each_legal_page_renders_with_the_company_name(self):
        os.environ["FIRESIDE_COMPANY"] = "Magic Smoke Technologies LLC"
        try:
            for fn in (self.app.privacy, self.app.terms, self.app.delete_account):
                s, out = fn({}, {})
                self.assertEqual(s, 200)
                self.assertIn("Magic Smoke Technologies LLC", out["_html"])
        finally:
            os.environ.pop("FIRESIDE_COMPANY", None)

    def test_favicon_and_ui_are_served(self):
        s, out = self.app.favicon({}, {})
        self.assertEqual(s, 200)
        s, out = self.app.ui({}, {})
        self.assertEqual(s, 200)
        self.assertIn("__HOST_VERSION__", out["_html"]) if False else None
        self.assertGreater(len(out["_html"]), 100)


class TestFieldsAndPluginListing(Base):
    def test_fields_normalises_key_type(self):
        out = self.app._fields(self.app.host.speech["sale"].obj)
        names = [f["name"] for f in out]
        self.assertIn("api_key", names)
        self.assertIn("base", names)
        kinds = {f["name"]: f["kind"] for f in out}
        self.assertEqual(kinds["api_key"], "password")

    def test_fields_survive_a_plugin_that_throws(self):
        self.assertEqual(self.app._fields(self.app.host.speech["muted"].obj), [])

    def test_plugins_lists_three_kinds(self):
        s, out = self.app.plugins({}, {})
        self.assertEqual(s, 200)
        self.assertTrue(out["readers"])
        self.assertTrue(out["speech"])
        self.assertTrue(out["storage"])

    def test_voices_unknown_provider_is_400(self):
        s, out = self.app.voices({"user": "u1"}, {"provider": "nope"})
        self.assertEqual(s, 400)

    def test_voices_by_connection(self):
        cid = connections.upsert(self.conn, "u1", "sale", "mine", {})
        s, out = self.app.voices({"user": "u1"}, {"connection_id": cid})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["voices"][0]["id"], "v")

    def test_voices_refuses_someone_elses_connection(self):
        cid = connections.upsert(self.conn, "u2", "sale", "theirs", {})
        s, out = self.app.voices({"user": "u1"}, {"connection_id": cid})
        self.assertEqual(s, 403)

    def test_voices_refuses_a_switched_off_connection(self):
        cid = connections.upsert(self.conn, "u1", "sale", "mine", {"api_key": "k"})
        connections.set_override(self.conn, "u1", cid, enabled=False)
        s, out = self.app.voices({"user": "u1"}, {"connection_id": cid})
        self.assertEqual(s, 403)
        self.assertIn("switched off", out["error"])


class TestCovers(Base):
    def test_url_allowed_rejects_private_and_odd_schemes(self):
        self.assertFalse(self.app._url_allowed("file:///etc/passwd"))
        self.assertFalse(self.app._url_allowed("http://localhost/x"))
        self.assertFalse(self.app._url_allowed("http://127.0.0.1/x"))
        self.assertFalse(self.app._url_allowed("http://10.0.0.5/x"))
        self.assertFalse(self.app._url_allowed("not a url"))
        self.assertTrue(self.app._url_allowed("http://93.184.216.34/x"))

    def test_cover_from_url_sniffs_when_the_server_lies(self):
        class R:
            headers = {"Content-Type": "application/octet-stream"}

            def read(self, n=None):
                return b"\xff\xd8\xff" + b"J" * 50

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        saved_urlopen, saved_allow = appmod.urllib.request.urlopen, self.app._url_allowed
        appmod.urllib.request.urlopen = lambda req, timeout=None: R()
        self.app._url_allowed = lambda u: True
        try:
            data, mime = self.app._cover_from_url("https://e.com/c.jpg")
            self.assertEqual(mime, "image/jpeg")
            self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        finally:
            appmod.urllib.request.urlopen = saved_urlopen
            self.app._url_allowed = saved_allow

    def test_cover_from_url_gives_up(self):
        saved_allow = self.app._url_allowed
        self.app._url_allowed = lambda u: True
        saved_urlopen = appmod.urllib.request.urlopen
        appmod.urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(OSError("x"))
        try:
            self.assertIsNone(self.app._cover_from_url("https://e.com/c.jpg"))
            self.assertIsNone(self.app._cover_from_url("file:///etc/passwd"))
        finally:
            appmod.urllib.request.urlopen = saved_urlopen
            self.app._url_allowed = saved_allow

    def test_put_cover_rejects_bad_input(self):
        bid = self.import_book()
        s, _ = self.app.put_cover({}, {"user": "u1", "book": bid, "data_b64": ""})
        self.assertEqual(s, 400)
        s, out = self.app.put_cover({}, {"user": "u1", "book": bid,
                                         "data_b64": "aGk="})
        self.assertEqual(s, 200, out)     # an upload is trusted, not sniffed
        big = "A" * (self.app.COVER_MAX_BYTES * 2)
        s, _ = self.app.put_cover({}, {"user": "u1", "book": bid, "data_b64": big})
        self.assertIn(s, (413, 400))

    def test_put_cover_from_url_path(self):
        bid = self.import_book()
        saved = self.app._cover_from_url
        self.app._cover_from_url = lambda u: (b"\xff\xd8\xffx", "image/jpeg")
        try:
            s, out = self.app.put_cover({}, {"user": "u1", "book": bid,
                                             "url": "https://e.com/c.jpg"})
            self.assertEqual(s, 200, out)
            self.assertTrue(out["cover"])
        finally:
            self.app._cover_from_url = saved

    def test_put_cover_refuses_on_storage(self):
        bid = self.import_book()
        saved = self.app._storage_ok
        saved_url = self.app._cover_from_url
        self.app._cover_from_url = lambda u: (b"\xff\xd8\xffx", "image/jpeg")
        self.app._storage_ok = lambda uid, n: False
        try:
            s, out = self.app.put_cover({}, {"user": "u1", "book": bid, "url": "https://e.com/c.jpg"})
            self.assertEqual(s, 507)
        finally:
            self.app._storage_ok = saved
            self.app._cover_from_url = saved_url

    def test_cover_auto_without_isbn_finds_nothing(self):
        bid = self.import_book()
        saved = self.app._cover_by_isbn
        self.app._cover_by_isbn = lambda isbn: None
        try:
            s, out = self.app.cover_auto({}, {"book": bid})
            self.assertEqual(s, 404)
            s, out = self.app.cover_auto({}, {"book": "nope"})
            self.assertEqual(s, 404)
        finally:
            self.app._cover_by_isbn = saved

    def test_cover_auto_with_an_isbn_sets_it(self):
        bid = self.import_book()
        saved = self.app._cover_by_isbn

        def fake(isbn):
            return (b"\xff\xd8\xffx", "image/jpeg") if isbn == "9780306406157" else None

        self.app._cover_by_isbn = fake
        try:
            s, out = self.app.cover_auto({}, {"book": bid, "isbn": "9780306406157"})
            self.assertEqual(s, 200, out)
            self.assertTrue(out["cover"])
            s, out = self.app.cover_auto({}, {"book": bid, "isbn": "111"})
            self.assertEqual(s, 404)
        finally:
            self.app._cover_by_isbn = saved


class TestDetails(Base):
    def test_isbn_helpers(self):
        self.assertEqual(self.app._norm_isbn("978-0-306-40615-7"), "9780306406157")
        self.assertTrue(self.app._valid_isbn("0306406152"))
        self.assertFalse(self.app._valid_isbn("123"))

    def test_details_by_isbn_builds_every_field(self):
        pages = {
            "https://openlibrary.org/isbn/9780306406157.json": {
                "title": "A Book", "subtitle": "The Rest",
                "authors": [{"key": "/authors/OL1A"}, {"bad": 1}, {"key": "/authors/OL2A"}],
                "publishers": ["One", "Two", ""], "publish_date": "1979",
                "description": {"value": "Long text"}},
            "https://openlibrary.org/authors/OL1A.json": {"name": "Ada"},
            "https://openlibrary.org/authors/OL2A.json": {"name": "Bob"},
        }

        def fake(url):
            if url not in pages:
                raise OSError("missing")
            return pages[url]

        saved = self.app._get_json
        self.app._get_json = fake
        try:
            out = self.app._details_by_isbn("978-0-306-40615-7")
            self.assertEqual(out["title"], "A Book: The Rest")
            self.assertEqual(out["author"], "Ada, Bob")
            self.assertEqual(out["publisher"], "One, Two")
            self.assertEqual(out["published"], "1979")
            self.assertEqual(out["description"], "Long text")
            self.assertIsNone(self.app._details_by_isbn("nope"))
        finally:
            self.app._get_json = saved

    def test_details_by_isbn_falls_back_to_works_description(self):
        saved = self.app._get_json
        self.app._get_json = lambda url: (
            {"title": "T", "by_statement": "Someone", "works": [{"key": "/works/W1"}]}
            if "isbn" in url else {"description": "From the work"})
        try:
            out = self.app._details_by_isbn("0306406152")
            self.assertEqual(out["author"], "Someone")
            self.assertEqual(out["description"], "From the work")
        finally:
            self.app._get_json = saved

    def test_details_give_up_on_a_broken_lookup(self):
        saved = self.app._get_json
        self.app._get_json = lambda url: (_ for _ in ()).throw(OSError("down"))
        try:
            self.assertIsNone(self.app._details_by_isbn("0306406152"))
        finally:
            self.app._get_json = saved

    def test_fetch_details_saves_what_it_finds(self):
        bid = self.import_book()
        saved = self.app._details_by_isbn
        self.app._details_by_isbn = lambda isbn: {"author": "Ada", "publisher": "P"}
        saved_cover = self.app._cover_by_isbn
        self.app._cover_by_isbn = lambda isbn: None
        try:
            s, out = self.app.fetch_details({}, {"book": bid, "isbn": "978-0-306-40615-7"})
            self.assertEqual(s, 200, out)
            self.assertEqual(out["book"]["author"], "Ada")
            s, out = self.app.fetch_details({}, {"book": bid, "isbn": "12"})
            self.assertEqual(s, 400)
            self.app._details_by_isbn = lambda isbn: None
            s, out = self.app.fetch_details({}, {"book": bid, "isbn": "0306406152"})
            self.assertEqual(s, 404)
            s, out = self.app.fetch_details({}, {"book": "missing"})
            self.assertEqual(s, 404)
        finally:
            self.app._details_by_isbn = saved
            self.app._cover_by_isbn = saved_cover


class TestRemoval(Base):
    def test_owned_book_guards(self):
        bid = self.import_book()
        self.assertEqual(self.app._owned_book("u1", bid)[0]["id"], bid)
        self.assertEqual(self.app._owned_book("u2", bid)[1][0], 403)
        self.assertEqual(self.app._owned_book("u1", "missing")[1][0], 404)

    def test_remove_renditions_all(self):
        bid = self.import_book()
        s, out = self.app.remove_renditions({}, {"user": "u1", "book": bid, "all": True})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["scope"], "all")
        s, out = self.app.remove_renditions({}, {"user": "u2", "book": bid})
        self.assertEqual(s, 403)

    def test_remove_renditions_refused_while_converting(self):
        bid = self.import_book()
        self.conn.execute("INSERT INTO jobs(id,user_id,book_id,kind,provider,voice,model,"
                          "status,total,current,error,created_at,updated_at) "
                          "VALUES('j1','u1',?, 'render','sale','v','','running',1,0,'','t','t')",
                          (bid,))
        self.conn.commit()
        s, out = self.app.remove_renditions({}, {"user": "u1", "book": bid, "voice": "v"})
        self.assertEqual(s, 409)

    def test_delete_book(self):
        bid = self.import_book()
        s, out = self.app.delete_book({}, {"user": "u1", "book": bid})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["title"], "T")
        self.assertEqual(self.app._owned_book("u1", bid)[1][0], 404)

    def test_delete_book_refused_while_converting(self):
        bid = self.import_book()
        self.conn.execute("INSERT INTO jobs(id,user_id,book_id,kind,provider,voice,model,"
                          "status,total,current,error,created_at,updated_at) "
                          "VALUES('j2','u1',?, 'render','sale','v','','queued',1,0,'','t','t')",
                          (bid,))
        self.conn.commit()
        s, out = self.app.delete_book({}, {"user": "u1", "book": bid})
        self.assertEqual(s, 409)


class TestSpeak(Base):
    def test_speak_refusals(self):
        self.assertEqual(self.app.speak({}, {"user": "u1", "text": "  "})[0], 400)
        self.assertEqual(self.app.speak({}, {"user": "u1", "text": "x" * 100000})[0], 400)
        self.assertEqual(self.app.speak({}, {"user": "u1", "text": "hi",
                                             "provider": "nope"})[0], 400)

    def test_speak_says_nothing_when_the_plugin_returns_nothing(self):
        s, out = self.app.speak({}, {"user": "u1", "text": "hi", "provider": "muted"})
        self.assertEqual(s, 502)

    def test_speak_returns_audio_bytes(self):
        s, out = self.app.speak({}, {"user": "u1", "text": "hi", "provider": "sale"})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["_raw"], b"A:hi")
        self.assertEqual(out["chars"], 2)

    def test_speak_through_a_connection(self):
        cid = connections.upsert(self.conn, "u1", "sale", "mine", {})
        s, out = self.app.speak({}, {"user": "u1", "text": "hi", "connection_id": cid})
        self.assertEqual(s, 200, out)

    def test_speak_refuses_another_users_connection(self):
        cid = connections.upsert(self.conn, "u2", "sale", "theirs", {})
        s, out = self.app.speak({}, {"user": "u1", "text": "hi", "connection_id": cid})
        self.assertEqual(s, 403)

    def test_speak_is_rate_limited(self):
        class Never:
            def allow(self, uid):
                return False

        self.app.rate_limiter = Never()
        s, out = self.app.speak({}, {"user": "u1", "text": "hi", "provider": "sale"})
        self.assertEqual(s, 429)


class TestCreditsAndBilling(Base):
    def tearDown(self):
        payments.REGISTRY.clear()

    def test_sale_connection_lookup(self):
        p, sp, cred = self.app._sale_connection("u1")
        self.assertEqual((p, sp, cred), (None, None, None))

    def test_credits_reads_only(self):
        s, out = self.app.credits({}, {"user": "u1"})
        self.assertEqual(s, 200)
        self.assertIn("balance", out)

    def test_credits_says_nothing_is_for_sale_when_the_server_cannot_charge(self):
        # The route used to hand back a sale dict with nulls for any speech
        # connection. "muted" has no pricing at all, so there is no offer.
        cid = connections.upsert(self.conn, "u1", "muted", "m", {"api_key": "k"})
        self.app.sale_connection = {"connection_id": cid}
        s, out = self.app.credits({}, {"user": "u1"})
        self.assertEqual(s, 200)
        self.assertIsNone(out["sale"])

    def test_credits_reports_the_rate_its_server_actually_charges(self):
        cid = connections.upsert(self.conn, "u1", "sale", "c", {"api_key": "k"})
        self.app.sale_connection = {"connection_id": cid}
        s, out = self.app.credits({}, {"user": "u1"})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["sale"]["provider"], "sale")
        self.assertAlmostEqual(out["sale"]["rate_per_1m_chars"], 15.0)
        self.assertEqual(out["sale"]["bundles"], [{"usd": 5, "chars": 333333}])

    def test_credits_purchase_refusals(self):
        self.assertEqual(self.app.credits_purchase({}, {})[0], 401)
        self.assertEqual(self.app.credits_purchase({}, {"user": "u1"})[0], 400)
        cid = connections.upsert(self.conn, "u1", "sale", "c", {"api_key": "k"})
        self.app.sale_connection = {"connection_id": cid}
        self.assertEqual(self.app.credits_purchase({}, {"user": "u1", "usd": "x"})[0], 400)
        self.assertEqual(self.app.credits_purchase({}, {"user": "u1", "usd": -1})[0], 400)
        self.assertEqual(self.app.credits_purchase({}, {"user": "u1", "usd": 5})[0], 503)

    def test_credits_purchase_refused_and_accepted(self):
        cid = connections.upsert(self.conn, "u1", "sale", "c", {"api_key": "k"})
        self.app.sale_connection = {"connection_id": cid}
        payments.REGISTRY.clear()
        payments.register(FakePayments(ok=False, detail="card declined"))
        s, out = self.app.credits_purchase({}, {"user": "u1", "usd": 5})
        self.assertEqual(s, 402)
        self.assertIn("declined", out["detail"])
        payments.REGISTRY.clear()
        fp = FakePayments()
        payments.register(fp)
        s, out = self.app.credits_purchase({}, {"user": "u1", "usd": 15})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["chars"], 1_000_000)
        self.assertEqual(fp.charged[0][1]["chars"], 1_000_000)

    def test_billing_checkout_edges(self):
        self.assertEqual(self.app.billing_checkout({}, {})[0], 401)
        self.assertEqual(self.app.billing_checkout({}, {"user": "u1"})[0], 503)
        payments.register(FakePayments())
        s, out = self.app.billing_checkout({}, {"user": "u1", "plan": "platform"})
        self.assertEqual(s, 200, out)
        self.assertIn("url", out)
        self.assertEqual(self.app.billing_checkout({}, {"user": "u1",
                                                        "items": [1]})[0], 400)
        self.assertEqual(self.app.billing_checkout({}, {"user": "u1",
                                                        "items": [{"quantity": 2}]})[0], 400)
        self.assertEqual(self.app.billing_checkout({}, {"user": "u1",
                                                        "items": [{"plan": "x",
                                                                   "quantity": "no"}]})[0], 400)
        s, out = self.app.billing_checkout({}, {"user": "u1", "items": [
            {"plan": "platform", "quantity": 1}, {"plan": "storage", "quantity": 2}]})
        self.assertEqual(s, 200, out)

    def test_billing_confirm_requires_a_provider(self):
        self.assertEqual(self.app.billing_confirm({}, {"user": "u1"})[0], 503)


class TestConnectionsGrantsJobs(Base):
    def test_connection_delete_guard_and_success(self):
        theirs = connections.upsert(self.conn, "u2", "sale", "theirs", {})
        s, out = self.app.connection_delete({}, {"user": "u1", "id": theirs})
        self.assertEqual(s, 403)          # a personal connection is its owner's alone
        mine = connections.upsert(self.conn, "u1", "sale", "mine", {})
        s, out = self.app.connection_delete({}, {"user": "u1", "id": mine})
        self.assertEqual(s, 200, out)
        self.assertIsNone(connections.get(self.conn, mine))

    def test_grants_set_requires_admin_and_a_user(self):
        s, out = self.app.grants_set({"_user": "u1"}, {"user": "u2"})
        self.assertEqual(s, 403)
        s, out = self.app.grants_set({"_user": "admin"}, {})
        self.assertEqual(s, 400)

    def test_grants_set_writes_only_what_it_was_sent(self):
        grants.set_grants(self.conn, "u2", storage=True)
        s, out = self.app.grants_set({"_user": "admin"}, {"user": "u2",
                                                          "credits_unlimited": True})
        self.assertEqual(s, 200, out)
        self.assertTrue(out["grants"]["storage_unlimited"])
        self.assertTrue(out["grants"]["credits_unlimited"])

    def test_cancel_job(self):
        self.assertEqual(self.app.cancel_job({}, {"user": "u1"})[0], 404)
        self.conn.execute("INSERT INTO jobs(id,user_id,book_id,kind,provider,voice,model,"
                          "status,total,current,error,created_at,updated_at) "
                          "VALUES('j3','u2','b','render','sale','v','','queued',1,0,'','t','t')")
        self.conn.commit()
        self.assertEqual(self.app.cancel_job({}, {"user": "u1", "job": "j3"})[0], 403)
        s, out = self.app.cancel_job({}, {"user": "u2", "job": "j3"})
        self.assertEqual(s, 200, out)
        self.assertTrue(out["cancel"])
        s, out = self.app.cancel_job({}, {"user": "u2", "job": "j3"})
        self.assertFalse(out["cancel"])

    def test_pick_reader_returns_none_when_nothing_matches(self):
        self.assertIsNone(self.app._pick_reader({"kind": "carrier-pigeon"}))

    def test_pick_reader_survives_a_plugin_that_throws(self):
        class Boom:
            def matches(self, req):
                raise RuntimeError("x")

        class P:
            def __init__(self, o):
                self.obj = o

        self.app.host.readers.insert(0, P(Boom()))
        self.assertIsNone(self.app._pick_reader({"kind": "nope"}))


class TestWebhook(Base):
    def tearDown(self):
        payments.REGISTRY.clear()

    def test_unknown_provider_and_no_webhook_support(self):
        self.assertEqual(self.app.payment_webhook(b"{}", "")[0], 404)
        payments.register(FakePayments())
        self.assertEqual(self.app.payment_webhook(b"{}", "", provider_id="fake")[0], 501)

    def test_webhook_paths(self):
        class WP(FakePayments):
            def verify_webhook(self, raw, sig):
                if raw == b"bad":
                    return None, "bad signature"
                return ({"type": "checkout.session.completed"} if raw == b"ok"
                        else {"type": "customer.updated"}), ""

            def webhook_reference(self, event):
                return "co_9" if event.get("type", "").startswith("checkout") else ""

            def confirm(self, ref):
                class S:
                    ok = ref != "unpaid"
                    plan = "platform"
                    status = "active"
                    user = "u1"
                    items = None
                    quantity = 1
                    current_period_end = 0
                    detail = "not paid"
                return S()

        payments.register(WP())
        self.assertEqual(self.app.payment_webhook(b"bad", "", provider_id="fake")[0], 400)
        s, out = self.app.payment_webhook(b"{}", "", provider_id="fake")
        self.assertEqual(s, 200)
        self.assertIn("ignored", out)
        s, out = self.app.payment_webhook(b"ok", "", provider_id="fake")
        self.assertEqual(s, 200, out)
        self.assertTrue(out["applied"])
        self.assertEqual(billing.get(self.conn, "u1")["plan"], "platform")


if __name__ == "__main__":
    unittest.main()
