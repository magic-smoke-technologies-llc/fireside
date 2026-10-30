"""The series picker is in the page the host actually serves.

A green host test proves the endpoint; it says nothing about whether the screen
that asks the question exists. This reads ui/index.html the way the host serves
it, and checks the parts a person has to be able to touch: the list, the three
buttons, and the sentence that says what the page is.

On unittest.TestCase on purpose: CI runs unittest discover, which collects
TestCase classes, and a bare `def test_x()` is collected by nothing.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


class TestSeriesPickerMarkup(unittest.TestCase):
    def test_the_picker_is_in_the_page(self):
        for needle in ('id="serpick"', 'id="serlist"', 'id="sertitle"',
                       'id="serall"', 'id="serpull"', 'id="sercancel"'):
            self.assertIn(needle, PAGE)

    def test_the_picker_is_hidden_until_a_series_page_is_pasted(self):
        m = re.search(r'<div class="addpanel" id="serpick"[^>]*>', PAGE)
        self.assertTrue(m and "hidden" in m.group(0))

    def test_the_picker_posts_to_the_series_route_with_the_page_it_came_from(self):
        self.assertIn("'/import/series'", PAGE)
        # The page url travels with the picks, because the host re-reads the
        # page and refuses anything that is not linked from it.
        self.assertTrue(re.search(r"'/import/series',\s*\{[^}]*url:\s*ser\.page", PAGE))

    def test_the_refusal_is_caught_and_shown_rather_than_printed_as_an_error(self):
        self.assertIn("e.data.series", PAGE)
        self.assertIn("showSeries(", PAGE)

    def test_the_list_labels_itself_for_a_screen_reader(self):
        m = re.search(r'<div class="serlist"[^>]*>', PAGE)
        self.assertTrue(m and 'aria-label="Works on this page"' in m.group(0))

    def test_a_work_already_on_the_shelf_cannot_be_picked_twice(self):
        # The row is shown, marked, and its checkbox is disabled rather than
        # hidden: a work you own is still part of what the page holds.
        self.assertIn("cb.disabled = true", PAGE)
        self.assertIn("already on your shelf", PAGE)

class TestThePickerIsReadableAndMoves(unittest.TestCase):
    """Cat's report: four checkboxes and no visible text, then a pull with no
    sign it was happening. Both are things you can only see on the screen, so
    these read the served page for the properties that made them invisible."""

    def test_the_row_text_uses_a_token_that_exists(self):
        # the title was styled with var(--fg), which no theme defines: the
        # declaration is invalid, the colour falls back, and the row reads as
        # 12px uppercase in the dimmest grey - i.e. as nothing on a phone.
        self.assertNotIn("var(--fg)", PAGE)
        m = re.search(r"\.serrow \.sert\{([^}]*)\}", PAGE)
        self.assertTrue(m)
        self.assertIn("var(--text)", m.group(1))
        self.assertIn("font-size:15px", m.group(1))

    def test_the_row_is_not_uppercase_and_not_12px(self):
        m = re.search(r"\.serrow\{([^}]*)\}", PAGE)
        self.assertTrue(m, "the row lost its own rule")
        self.assertIn("text-transform:none", m.group(1))
        self.assertIn("font-size:14px", m.group(1))

    def test_every_row_gets_a_title_even_when_the_page_gives_none(self):
        self.assertIn("T('Untitled work')", PAGE)

    def test_there_is_a_live_status_line_while_the_pull_runs(self):
        m = re.search(r'<p class="small" id="serstatus"[^>]*>', PAGE)
        self.assertTrue(m)
        self.assertIn('role="status"', m.group(0))
        self.assertIn('aria-live="polite"', m.group(0))
        self.assertIn("Pulling {i} of {n}: {title}", PAGE)

    def test_the_pull_asks_for_one_work_at_a_time_so_it_can_report_progress(self):
        self.assertIn("picks: [picks[i]]", PAGE)

    def test_the_settings_sheet_can_be_closed_from_the_top(self):
        self.assertIn('id="settingsx"', PAGE)
        self.assertIn("$('#settingsx').onclick", PAGE)

    def test_the_cancel_button_is_translated(self):
        m = re.search(r'<button class="btn quiet" id="sercancel"[^>]*>', PAGE)
        self.assertIn('data-t="Cancel"', m.group(0))
