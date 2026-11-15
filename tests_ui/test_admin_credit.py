"""An admin has to be able to put credit on a reader's balance, from the screen.

Chris asked for this on 11-14: "give me a UI where I can add/remove funds from
a user's balance." The unit test proves the route; this proves the thing he will
actually use: the row on the admin screen, the amount typed into it, and the
number that comes back. The receipt is not the toast. It is the balance the host
reports on the next read, and the ledger line the reader sees on their own
credits page with a name on it.
"""

from tests_ui.support import BrowserTest, wait_until, wait_visible


def open_admin(page):
    page.click("#navadmin")
    wait_visible(page, "#v-adm")
    wait_until(page, "() => document.querySelectorAll('#admlist .accthead')"
                     ".length > 0")


def find_row(page, name):
    """The account row whose header carries this username, opened."""
    handle = page.evaluate_handle("""(name) => {
        const rows = [...document.querySelectorAll('#admlist .acct')];
        return rows.find(r => r.textContent.includes(name)) || null;
    }""", name)
    el = handle.as_element()
    assert el is not None, "no admin row for %r" % name
    el.query_selector(".accthead").click()
    return el


class TestAdminCredit(BrowserTest):

    def test_an_admin_adds_credit_and_the_reader_sees_it(self):
        self.register("boss")               # the first account is the admin

        # a second person, through their own browser door
        other = self.context.browser.new_context()
        try:
            page2 = other.new_page()
            from tests_ui.support import wait_until as wu
            page2.goto(self.host.url("/"))
            wu(page2, "() => document.readyState === 'complete'")
            wait_visible(page2, "#lock")
            wu(page2, "() => document.querySelector('#locksub')"
                      ".textContent.trim().length > 0")
            page2.click("#locktoggle")
            page2.fill("#lockuser", "reader")
            page2.fill("#lockemail", "reader@example.test")
            page2.fill("#lockpass", "bench-correction-1")
            page2.fill("#lockpass2", "bench-correction-1")
            page2.click("#lockgo")
            wu(page2, "() => !document.querySelector('#lock')"
                      " || document.querySelector('#lock').hidden")
        finally:
            pass

        open_admin(self.page)
        row = find_row(self.page, "reader")
        wait_until(self.page, """() => {
            const rows = [...document.querySelectorAll('#admlist .acct')];
            const r = rows.find(r => r.textContent.includes('reader'));
            return !!(r && !r.querySelector('.acctbody').hidden);
        }""")
        amt = row.query_selector("input[type='number']")
        amt.fill("5")
        btns = row.query_selector_all("button")
        add = [b for b in btns if b.inner_text().strip() == "Add credit"][0]
        add.click()
        wait_until(self.page, "() => /Balance is now/.test("
                              "document.querySelector('#admmsg').textContent)")

        # the receipt is the host's number, read fresh from the admin route
        out = self.page.evaluate("""async () => {
            const r = await fetch('/admin/accounts');
            return await r.json();
        }""")
        reader = [a for a in out["accounts"] if a["username"] == "reader"][0]
        self.assertEqual(reader["balance"], 5.0)

        # and the reader's own history says who moved it
        history = page2.evaluate("""async () => {
            const r = await fetch('/credits/history');
            return await r.json();
        }""")
        kinds = [(e["kind"], e["delta"]) for e in history["entries"]]
        self.assertIn(("adjust", 5.0), kinds)
        other.close()