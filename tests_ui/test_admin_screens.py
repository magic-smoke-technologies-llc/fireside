"""The two screens Chris asked for on 11-14, read as a person reads them.

1. The admin roster is a control: a fixed pane with pages and a total, not one
   long column of accordions.
2. The third-party credentials (AO3 and the like) are their own tab, not a
   drawer under Voice.

Neither claim is provable from the host alone: the pane has to actually scroll,
the pager has to show a range, and the tab has to swap the panel that is drawn.
"""

from tests_ui.support import BrowserTest, wait_until, wait_visible


def open_settings(page, tab):
    page.click("#avatar")
    page.click("#menuset")
    page.click("#settabs button[data-p='%s']" % tab)
    wait_visible(page, "#v-set .setpanel[data-p='%s']" % tab)


class TestAdminRosterIsAControl(BrowserTest):

    def test_the_roster_shows_a_range_and_pages(self):
        self.register("boss")
        self.page.click("#navadmin")
        wait_visible(self.page, "#v-adm")
        wait_until(self.page, "() => document.querySelectorAll('#admlist .acct')"
                              ".length > 0")
        range_text = self.page.inner_text("#admrange")
        self.assertIn("of 1", range_text)
        # one account, so there is nothing to page to
        self.assertTrue(self.page.is_disabled("#admpageprev"))
        self.assertTrue(self.page.is_disabled("#admpagenext"))

        # the list is its own scroll pane, not the page's length
        style = self.page.evaluate("""() => {
            const s = getComputedStyle(document.querySelector('.admscroll'));
            return { overflow: s.overflowY, max: s.maxHeight };
        }""")
        self.assertEqual(style["overflow"], "auto")
        self.assertNotEqual(style["max"], "none")


class TestThirdPartyTab(BrowserTest):

    def test_ao3_settings_are_their_own_tab(self):
        self.register("party")
        open_settings(self.page, "party")
        text = self.page.inner_text("#v-set .setpanel[data-p='party']")
        self.assertIn("Connected sources", text)
        self.assertTrue(self.page.is_visible("#sources"))
        # and they are not still a drawer under Voice
        voice = self.page.inner_text("#v-set .setpanel[data-p='voice']")
        self.assertNotIn("Connected sources", voice)
        self.assertFalse(self.page.is_visible("#v-set .setpanel[data-p='voice']"))

    def test_the_voice_tab_no_longer_carries_a_source_drawer(self):
        self.register("party2")
        open_settings(self.page, "voice")
        self.assertEqual(self.page.query_selector_all(".setpanel[data-p='voice'] #sources"),
                         [])
        # the tab itself is one click away and carries the drawer's job
        self.page.click("#settabs button[data-p='party']")
        wait_visible(self.page, "#sources")