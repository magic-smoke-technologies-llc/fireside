"""A word typed on a book page belongs to that book.

Cat typed "DoD" while reading one book and the app offered to re-encode 88
chapters across 35 books. Two causes, both scope: a new word row defaulted to
"All books", and the re-encode check never sent the book it was standing in.
The host has always accepted a `book` filter; the page just never passed one.

This test drives the two functions the page uses, with the network answered by
a stub, so it fails if either default drifts back to the whole library.
"""

import unittest

from tests_ui.support import BrowserTest


class TestWordsScope(BrowserTest):

    def test_a_new_word_defaults_to_the_open_book(self):
        self.register("wordsscope")
        r = self.page.evaluate("""() => {
          book = { id: 'b1', title: 'One Book' };
          return { withBook: defaultScope() };
        }""")
        self.assertEqual(r["withBook"], "book:b1",
                         "a new word would be written against every book")
        # with no book open there is nothing to default to, and that is honest:
        # the empty scope means the reader chose "All books" themselves.
        r = self.page.evaluate("""() => {
          book = null; player = { id: '', title: '', chapters: [] };
          return defaultScope();
        }""")
        self.assertEqual(r, "")

    def test_the_check_asks_about_this_book_and_only_all_books_when_told(self):
        self.register("wordsscope2")
        r = self.page.evaluate("""async () => {
          book = { id: 'b1', title: 'One Book' };
          player = { id: '', title: '', chapters: [] };
          const calls = [];
          window.api = async (u) => { calls.push(u);
            return { chapters_affected: 0, books_affected: 0, books: [] }; };
          await showPronImpact(true);
          await showPronImpact(false, true);
          return calls;
        }""")
        self.assertEqual(r, ["/pron/impact?book=b1", "/pron/impact"],
                         "the default check left the book it was standing in")

    def test_a_saved_rule_keeps_the_scope_it_was_saved_with(self):
        # Cat opened the words editor on the book she was listening to and every
        # older word came back as "Only: this book". Nothing on the server had
        # changed; #264 read a blank scope (the whole library) as a missing one
        # and filled in the book on screen. Saving in that state would have
        # narrowed every library-wide word to one book.
        self.register("wordsscope3")
        r = self.page.evaluate("""() => {
          book = { id: 'b1', title: 'One Book' };
          player = { id: '', title: '', chapters: [] };
          SCOPEBOOKS = [{ id: 'b1', title: 'One Book' },
                        { id: 'b2', title: 'Other Book' }];
          SCOPEAUTHORS = []; SCOSERIES = [];
          showPron([{ from: 'DoD', to: 'dee oh dee' },
                    { from: 'Hermione', to: 'Her-my-oh-nee', book: 'b2' }]);
          const shown = [...document.querySelectorAll('#pronrows .pscope')]
                          .map(s => s.value);
          collectPron();
          return { shown: shown,
                   saved: PRON.map(r => [r.from, r.book || ''] ) };
        }""")
        self.assertEqual(r["shown"], ["", "book:b2"],
                         "a library-wide word was relabelled as this-book-only")
        # the round trip is the receipt: what a Save would write back must keep
        # the library-wide word library-wide
        self.assertEqual(r["saved"],
                         [["DoD", ""], ["Hermione", "b2"]],
                         "a Save would have narrowed a library-wide word")

    def test_a_new_row_still_starts_on_the_open_book(self):
        self.register("wordsscope4")
        r = self.page.evaluate("""() => {
          book = { id: 'b1', title: 'One Book' };
          player = { id: '', title: '', chapters: [] };
          SCOPEBOOKS = [{ id: 'b1', title: 'One Book' }];
          SCOPEAUTHORS = []; SCOSERIES = [];
          document.getElementById('pronadd').click();
          return [...document.querySelectorAll('#pronrows .pscope')]
                   .map(s => s.value);
        }""")
        self.assertEqual(r, ["book:b1"],
                         "a newly typed word no longer follows the open book")


if __name__ == "__main__":
    unittest.main()