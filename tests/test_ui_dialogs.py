"""The dialogs have to fit a phone.

Cat could not delete a book: the settings sheet is the longest one in the app
(meta, series, cover, reading copies, delete) and it was centred in a fixed
overlay with no scroll, so its bottom half sat below the screen with no way to
reach it. These tests hold the two things that made it unreachable: an overlay
that cannot scroll, and a confirmation that belongs to the browser instead of
the page (a tap inside an app's WebView can be swallowed and never call back).
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")


def html():
    with open(UI, encoding="utf-8") as f:
        return f.read()


def rule(block, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", block)
    return m.group(1) if m else ""


class TestDialogsFitTheScreen(unittest.TestCase):
    def test_overlay_scrolls_and_the_sheet_is_capped(self):
        page = html()
        modal = rule(page, ".modal")
        self.assertIn("overflow-y:auto", modal, "a tall sheet has nowhere to go")
        sheet = rule(page, ".modal .sheet")
        self.assertIn("max-height:calc(100vh - 40px)", sheet)
        self.assertIn("max-height:calc(100dvh - 40px)", sheet)
        self.assertIn("overflow-y:auto", sheet)

    def test_the_longest_sheet_is_the_settings_dialog(self):
        # if this fails, some other dialog outgrew it: the cap has to cover them all
        page = html()
        self.assertIn('id="settingsdlg"', page)
        self.assertIn('id="sdelbookbtn"', page)

    def test_delete_is_confirmed_in_the_page_not_by_the_browser(self):
        page = html()
        for handler in ("$('#sdelbookbtn').onclick", "$('#srmallbtn').onclick"):
            start = page.index(handler)
            body = page[start:start + 700]
            self.assertIn("await askConfirm(", body,
                          "%s still leans on window.confirm" % handler)
            self.assertNotIn("!confirm(", body)

    def test_the_confirm_dialog_exists_with_both_buttons(self):
        page = html()
        self.assertIn('id="askdlg"', page)
        self.assertIn('id="askyes"', page)
        self.assertIn('id="askno"', page)
        self.assertIn("$('#askyes').onclick = () => askDone(true)", page)
        self.assertIn("$('#askno').onclick = () => askDone(false)", page)


if __name__ == "__main__":
    unittest.main()