"""The front door and the shelf, driven in a real browser.

The claim under test is narrow and worth it: a stranger can make an account on a
fresh install, lands in their own library, sees exactly the books the host says
they own, and cannot see anyone else's. Those are the two things a broken
deploy gets wrong without any error message: the door opens onto nothing, or
the shelf shows somebody else's books.

Every test gets its own host process and its own empty data dir, so "fresh
install" means fresh, not "whatever the last test left behind".
"""

import unittest

from tests_ui.support import BrowserTest, cookie_jar, wait_until, wait_visible


class TestSignInAndLibrary(BrowserTest):

    def test_first_visit_asks_to_create_the_first_account(self):
        """A brand-new install has no accounts, so the door is a create form.

        Getting this wrong is invisible in the API and obvious to a person: a
        first visitor who is offered "sign in" to an account that does not exist
        has nothing to type."""
        self.open_site()
        wait_visible(self.page, "#lock")
        self.assertEqual(self.page.text_content("#locktitle").strip(), "set up fireside")
        self.assertEqual(self.page.text_content("#lockgo").strip(), "Create account")
        self.assertTrue(self.page.is_visible("#lockemailwrap"),
                        "sign-up has to ask for an address")
        self.assertTrue(self.page.is_visible("#lockconfirm"),
                        "sign-up types the password twice")
        # the app behind the door is not showing its furniture to a stranger
        self.assertTrue(self.page.evaluate(
            "() => document.body.classList.contains('locked')"))
        self.assert_no_page_errors()

    def test_registering_lands_in_an_empty_library_of_their_own(self):
        self.register("ui_first")
        self.wait_library(0)
        self.assertTrue(self.page.is_visible("#libempty"))
        self.assertEqual(self.shelf(), [])
        status, me = self.host.request("/me", cookies=cookie_jar(self.context))
        self.assertEqual(status, 200)
        self.assertTrue(me["authenticated"])
        self.assert_no_page_errors()

    def test_shelf_shows_exactly_the_books_the_host_reports(self):
        """Add the bundled example book the way a person does, then compare the
        rendered shelf against the host's own answer for the same session."""
        self.register("ui_shelf")
        self.page.click("#addbtn")
        wait_visible(self.page, "#exbtn")
        self.page.click("#exbtn")
        # adding a book takes you to the book; the shelf is the thing under test
        self.wait_library(1)
        self.open_library()

        books = self.library_via_host()
        self.assertEqual(len(books), 1, "the host does not agree the book was added")
        rendered = self.shelf()
        self.assertEqual([c["title"] for c in rendered], [b["title"] for b in books])
        self.assertEqual(rendered[0]["line"], "%d of %d voiced"
                         % (books[0]["rendered"], books[0]["chapters"]))
        self.assertTrue(self.page.is_visible("#shelflabel"))
        self.assertFalse(self.page.is_visible("#libempty"))

        # and it is still there after a reload, because the shelf is the host's
        # answer and not something the page kept in its own pocket
        self.page.reload()
        self.wait_library(1)
        self.open_library()
        self.assertEqual([c["title"] for c in self.shelf()], [books[0]["title"]])
        self.assert_no_page_errors()

    def test_wrong_password_leaves_the_door_shut(self):
        self.register("ui_door")
        self.sign_out()

        self.open_site()
        wait_visible(self.page, "#lock")
        self.assertEqual(self.page.text_content("#locktitle").strip(), "sign in")
        self.page.fill("#lockuser", "ui_door")
        self.page.fill("#lockpass", "not-the-password")
        self.page.click("#lockgo")
        wait_until(self.page, "() => document.querySelector('#autherr').textContent.trim().length > 0")
        self.assertEqual(self.page.text_content("#autherr").strip(),
                         "invalid username or password.")
        self.assertTrue(self.page.is_visible("#lock"))
        status, me = self.host.request("/me", cookies=cookie_jar(self.context))
        self.assertFalse((me or {}).get("authenticated"),
                         "a failed sign-in still left a session behind")

        # the right password opens it, without a reload of the host
        self.page.fill("#lockpass", "bench-correction-1")
        self.page.click("#lockgo")
        self.wait_in_app()
        self.wait_library(0)
        # the 401 the wrong password earned is the only console error we accept
        self.assert_no_page_errors(allow_console=("401",))

    def test_one_account_cannot_see_another_shelf(self):
        """The second account is the test. It proves the shelf is scoped to the
        session rather than to whichever books are in the database."""
        self.register("ui_owner")
        self.page.click("#addbtn")
        wait_visible(self.page, "#exbtn")
        self.page.click("#exbtn")
        self.wait_library(1)
        self.open_library()
        owner_title = self.shelf()[0]["title"]
        self.sign_out()

        self.open_site()
        wait_visible(self.page, "#lock")
        self.page.click("#locktoggle")           # "Create a new account"
        wait_until(self.page, "() => document.querySelector('#locktitle').textContent.trim() === 'set up fireside'")
        self.page.fill("#lockuser", "ui_guest")
        self.page.fill("#lockemail", "ui_guest@example.test")
        self.page.fill("#lockpass", "second-person-9")
        self.page.fill("#lockpass2", "second-person-9")
        self.page.click("#lockgo")
        self.wait_in_app()

        self.wait_library(0)
        self.assertEqual(self.shelf(), [], "the guest sees books they do not own")
        self.assertTrue(self.page.is_visible("#libempty"))
        self.assertEqual(self.library_via_host(), [])
        self.sign_out()

        # and the owner's shelf is still the owner's
        self.sign_in("ui_owner")
        self.wait_library(1)
        self.assertEqual([c["title"] for c in self.shelf()], [owner_title])
        self.assert_no_page_errors()


if __name__ == "__main__":
    unittest.main()
