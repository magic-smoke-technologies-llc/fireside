"""The wallet history has to say what each row is, without being read closely.

Chris, 2026-11-10, on his own wallet page: below the top-up buttons "there is a
list of transactions against my wallet. But it has no visual distinction so I
was really confused why I was seeing 'platform allowance for period ending 0'
until I realized what it was".

Two defects, not one:

  * a plan allowance is stored as a topup, so the ledger row was shaped like a
    card payment and looked like one in the list, and
  * the note was written from a period end of 0, which is not a date anyone can
    read. Rows like that are already in deployed ledgers, so the read side has
    to be honest about them too.

What is asserted here is what the screen paints from the host's own ledger:
a grant row is a different shape and a named kind, a card row keeps its own,
and no row resurrects "period ending 0".
"""

import os
import sqlite3
import time

from tests_ui.support import BrowserTest, wait_until, wait_visible


def seed_ledger(host, user, rows):
    """Write ledger rows the way the accountant would, straight into the DB.

    No endpoint invents a ledger row, which is the point of a ledger. Rows are
    (id, kind, delta, note) with the currency fixed at USD.
    """
    path = os.path.join(host.data, "fireside.db")
    deadline = time.time() + 15
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.1)
    conn = sqlite3.connect(path, timeout=15)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO balances(user_id,currency,amount) VALUES(?,?,?)",
            ("local:" + user, "USD", 5.0))
        for i, (rid, kind, delta, note) in enumerate(rows):
            conn.execute(
                "INSERT OR REPLACE INTO ledger(id,user_id,kind,delta,currency,"
                "book_id,job_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, "local:" + user, kind, delta, "USD", "", "", note,
                 "2026-11-10T14:0%d:00Z" % i))
        conn.commit()
    finally:
        conn.close()


def open_wallet(page):
    page.click("#avatar")
    page.click("#menuset")
    page.click("#settabs button[data-p='wallet']")
    wait_visible(page, "#v-set .setpanel[data-p='wallet']")
    wait_until(page, "() => document.getElementById('wbal')"
                     ".textContent.trim() !== '\\u2026'")


def open_activity(page):
    """The ledger is a tab on the wallet screen, not a block under the buttons."""
    open_wallet(page)
    page.click("#wsubtabs button[data-w='activity']")
    wait_visible(page, "#v-set .wsub[data-w='activity']")
    wait_until(page, "() => document.querySelectorAll('#wlist .wr').length > 0")


class TestWalletActivity(BrowserTest):

    def test_the_wallet_opens_on_the_money_not_the_ledger(self):
        """The balance is always visible; the list waits behind its own tab.

        This is the layout half of Chris's report (11-10): the ledger sat under
        the add buttons, so a row that needed explaining turned up before the
        reader had asked for a history. A fix that hides the balance too would
        pass any test about the list and lose the point of the screen."""
        user = self.register("wallettabs")
        seed_ledger(self.host, user, [
            ("topup:local:%s:abc" % user, "topup", 5.0, "money added"),
        ])
        self.page.reload()
        self.wait_in_app()
        open_wallet(self.page)

        self.assertEqual(self.page.inner_text("#wbal").strip(), "$5.00")
        self.assertFalse(self.page.is_visible("#v-set .wsub[data-w='activity']"))
        self.assertTrue(self.page.is_visible("#wtopup"))
        tabs = self.page.inner_text("#wsubtabs")
        self.assertIn("Add credit", tabs)
        self.assertIn("Credit activity", tabs)

        self.page.click("#wsubtabs button[data-w='activity']")
        wait_visible(self.page, "#v-set .wsub[data-w='activity']")
        wait_until(self.page, "() => document.querySelectorAll('#wlist .wr')"
                              ".length > 0")
        # the balance does not move when the tab does
        self.assertEqual(self.page.inner_text("#wbal").strip(), "$5.00")
        self.assertFalse(self.page.is_visible("#wtopup"))

    def test_a_plan_allowance_is_not_a_card_payment(self):
        user = self.register("walletledger")
        seed_ledger(self.host, user, [
            # what a deployed ledger actually holds: a grant stored as a topup,
            # with a note written when the period end was still 0
            ("allowance:local:%s:0" % user, "topup", 3.0,
             "platform allowance for period ending 0"),
            ("topup:local:%s:abc" % user, "topup", 5.0, "money added"),
        ])
        self.page.reload()
        self.wait_in_app()
        open_activity(self.page)

        rows = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#wlist .wr'))"
            ".map(r => ({cls: r.className, txt: r.innerText,"
            " tag: (r.querySelector('.wk') || {}).textContent || ''}))")
        self.assertEqual(len(rows), 2, rows)
        by_tag = {r["tag"].strip(): r for r in rows}
        self.assertEqual(sorted(by_tag), ["Card", "Plan"], rows)
        grant, card = by_tag["Plan"], by_tag["Card"]
        self.assertIn("included", grant["cls"])
        self.assertNotIn("included", card["cls"])

        # the stale note must not reach the screen in either shape
        whole = self.page.inner_text("#wlist")
        self.assertNotIn("period ending 0", whole)
        self.assertIn("Included with your plan", whole)

        # and the list is a named tab, not a mystery block under the buttons
        page_text = self.page.inner_text("#wsubtabs")
        self.assertIn("Credit activity", page_text)

    def test_the_grant_keeps_its_date_once_the_period_end_is_real(self):
        """The honest case still names a period, and the date is not a number.

        This is the positive control: a fix that only deletes the phrase would
        pass the test above and lose the one thing that row is for."""
        user = self.register("walletperiod")
        end = 1794000000   # 2026-11-07, a real period end
        seed_ledger(self.host, user, [
            ("allowance:local:%s:%d" % (user, end), "topup", 3.0,
             "included with your plan, this period ends %s"
             % time.strftime("%d %b %Y", time.gmtime(end))),
        ])
        self.page.reload()
        self.wait_in_app()
        open_activity(self.page)
        whole = self.page.inner_text("#wlist")
        self.assertIn("Included with your plan", whole)
        self.assertIn("this period ends 06 Nov 2026", whole)
        self.assertNotIn("1794000000", whole)


if __name__ == "__main__":
    import unittest
    unittest.main()