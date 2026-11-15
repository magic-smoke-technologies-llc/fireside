"""The admin accounts screen: one row per account, and who may touch it.

The screen used to be three lists (grants, plans, sessions) that each repeated
every account. One row per account is the unit an admin thinks in, so the route
is tested for the things that make a row usable: the numbers are the account's
own, a row is found by its user id, and no stranger can read or act on any of it.
"""

import os
import tempfile
import unittest

from host import auth, billing, grants
from host.app import App
from tests.test_app import READER_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin

SPEECH_BODY = """
from host.contracts import Audio, Quote, Report
class Plugin:
    id = "fake"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False
    def settings(self):
        return []
    def list_voices(self, cred):
        return [{"id": "v", "label": "V"}]
    def synth(self, text, voice, opts, cred):
        return Audio(data=("A:" + text).encode())
    def cost_per_1m_chars(self, cred):
        return 0.0
    def quote(self, ctx):
        return Quote(chars_est=ctx.get("chars_est", 0), price=0.0)
    def report(self, ctx):
        return Report(charge="charged", cost=0.0, currency="USD")
"""


class TestAdminAccounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.conn = self.app.conn
        for uid in ("u_boss", "u_reader"):
            self.conn.execute("INSERT INTO users(id,name,created_at) VALUES(?,?,?)",
                              (uid, uid, "2026-01-01T00:00:00Z"))
        self.conn.execute("INSERT INTO local_accounts(username,user_id,digest,salt,"
                          "email,created_at) VALUES(?,?,?,?,?,?)",
                          ("alex", "u_reader", "x", "y", "", "2026-01-01"))
        self._make_admin("u_boss")

    def _make_admin(self, uid):
        self.conn.execute("INSERT INTO admins(user_id,created_at) VALUES(?,?)",
                          (uid, "2026-01-01"))

    def _wait(self, job_id, timeout=10.0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            row = self.conn.execute("SELECT status FROM jobs WHERE id=?",
                                    (job_id,)).fetchone()
            if row and row["status"] in ("done", "error"):
                self.assertEqual(row["status"], "done")
                return
            time.sleep(0.02)
        self.fail("job %s did not finish" % job_id)

    def _row(self, out, uid):
        return next(r for r in out["accounts"] if r["user_id"] == uid)

    def test_one_row_per_account_with_its_own_numbers(self):
        _, imp = self.app.import_book({}, {"user": "u_reader", "kind": "upload",
                                           "filename": "x.epub"})
        _, job = self.app.render({}, {"user": "u_reader", "book": imp["book_id"],
                                      "provider": "fake", "voice": "v"})
        self._wait(job["job_id"])
        grants.set_grants(self.conn, "u_reader", credits=True)
        billing.upsert(self.conn, "u_reader", "platform", "active", provider="admin")
        s, out = self.app.admin_accounts({"_user": "u_boss"}, {})
        self.assertEqual(s, 200)
        self.assertEqual(len(out["accounts"]), 2)
        boss, reader = self._row(out, "u_boss"), self._row(out, "u_reader")
        # the admin is listed first and named, even with no local account row
        self.assertEqual(out["accounts"][0]["user_id"], "u_boss")
        self.assertTrue(boss["admin"])
        self.assertEqual(reader["username"], "alex")
        self.assertFalse(reader["admin"])
        self.assertEqual(reader["plan"], "platform")
        self.assertEqual(reader["books"], 1)
        self.assertTrue(reader["credits_unlimited"])
        self.assertFalse(reader["storage_unlimited"])
        self.assertEqual(reader["chars_voiced"], len("hello worldsecond chapter"))
        self.assertGreater(reader["allowance_bytes"], 0)

    def test_a_stranger_cannot_read_the_roster(self):
        s, out = self.app.admin_accounts({"_user": "u_reader"}, {})
        self.assertEqual(s, 403)

    def test_delete_needs_the_targets_name_typed(self):
        s, out = self.app.admin_account_delete(
            {"_user": "u_boss"}, {"user": "u_reader", "confirm": "nope"})
        self.assertEqual(s, 400)
        self.assertIn("confirm", out["error"])
        s, out = self.app.admin_account_delete(
            {"_user": "u_boss"}, {"user": "u_reader", "confirm": "alex"})
        self.assertEqual(s, 200)
        self.assertTrue(out["ok"])
        left = {r["user_id"] for r in
                self.app.admin_accounts({"_user": "u_boss"}, {})[1]["accounts"]}
        self.assertEqual(left, {"u_boss"})

    def test_an_admin_cannot_delete_itself_or_another_admin(self):
        self._make_admin("u_reader")
        s, out = self.app.admin_account_delete(
            {"_user": "u_boss"}, {"user": "u_boss", "confirm": "u_boss"})
        self.assertEqual(s, 409)
        s, out = self.app.admin_account_delete(
            {"_user": "u_boss"}, {"user": "u_reader", "confirm": "alex"})
        self.assertEqual(s, 409)
        self.assertIn("admin", out["error"])

    def test_a_stranger_cannot_delete(self):
        s, out = self.app.admin_account_delete(
            {"_user": "u_reader"}, {"user": "u_boss", "confirm": "u_boss"})
        self.assertEqual(s, 403)


    # ---- balance adjustments ------------------------------------------------
    # Chris, 11-14: "give me a UI where I can add/remove funds from a user's
    # balance." The routes are the half a test can hold: the balance moves by
    # exactly the amount sent, the ledger row carries the admin's name, a
    # removal cannot dig past zero, and no stranger can move money at all.

    def test_credit_adds_and_the_ledger_names_who_did_it(self):
        s, out = self.app.admin_credit(
            {"_user": "u_boss"}, {"user": "u_reader", "amount": 5, "note": "apology"})
        self.assertEqual(s, 200)
        self.assertEqual(out["balance"], 5.0)
        self.assertEqual(out["before"], 0.0)
        row = self.conn.execute(
            "SELECT kind,delta,note FROM ledger WHERE user_id=? ORDER BY created_at",
            ("u_reader",)).fetchone()
        self.assertEqual(row["kind"], "adjust")
        self.assertEqual(row["delta"], 5.0)
        self.assertIn("u_boss", row["note"])
        self.assertIn("apology", row["note"])

    def test_credit_removes_and_refuses_to_dig_past_zero(self):
        self.app.admin_credit({"_user": "u_boss"}, {"user": "u_reader", "amount": 5})
        s, out = self.app.admin_credit(
            {"_user": "u_boss"}, {"user": "u_reader", "amount": -2.5})
        self.assertEqual(s, 200)
        self.assertEqual(out["balance"], 2.5)
        s, out = self.app.admin_credit(
            {"_user": "u_boss"}, {"user": "u_reader", "amount": -10})
        self.assertEqual(s, 400)
        self.assertIn("below zero", out["error"])
        from host import accounting
        self.assertEqual(accounting.balance(self.conn, "u_reader"), 2.5)

    def test_credit_rejects_junk_and_strangers(self):
        s, _ = self.app.admin_credit({"_user": "u_reader"}, {"user": "u_reader", "amount": 5})
        self.assertEqual(s, 403)
        s, out = self.app.admin_credit({"_user": "u_boss"}, {"user": "u_ghost", "amount": 5})
        self.assertEqual(s, 404)
        s, out = self.app.admin_credit({"_user": "u_boss"}, {"user": "u_reader", "amount": 0})
        self.assertEqual(s, 400)
        s, out = self.app.admin_credit({"_user": "u_boss"}, {"user": "u_reader", "amount": "abc"})
        self.assertEqual(s, 400)
        s, out = self.app.admin_credit({"_user": "u_boss"}, {"user": "u_reader", "amount": 5000})
        self.assertEqual(s, 400)

    def test_the_row_shows_the_balance_it_will_change(self):
        self.app.admin_credit({"_user": "u_boss"}, {"user": "u_reader", "amount": 7})
        _, out = self.app.admin_accounts({"_user": "u_boss"}, {})
        self.assertEqual(self._row(out, "u_reader")["balance"], 7.0)


if __name__ == "__main__":
    unittest.main()
