"""keiramarcos reader: one work, published as bundle pages that hold chapters.

No network. The fixtures are the shape the live site emits: a landing PAGE whose
"Story Posts" tab links out to dated posts, each post opening with the same
metadata block, chapter titles as <h1> inside .entry-content, a "Chapters 3-4"
link at the end of each bundle, the sharer and Jetpack like widget after "The
End", and a comment thread underneath. The fixtures are what keeps the live
parse from regressing without a request.
"""

import unittest

from plugins.reader.keiramarcos import plugin as km


HEADER = """<p class="wpwc-reading-time"><b>Reading Time:</b> 115 Minutes</p>
<p><strong>Title</strong>: A Glimpse of Us<br />
<strong>Author</strong>: Keira Marcos<br />
<strong>Fandom</strong>: 9-1-1<br />
<strong>Warnings</strong>: Explicit Sex<br />
<strong>Word Count</strong>: 56,004<br />
<strong>Summary</strong>: A soulmate AU.</p>
<p>* * * *</p>
"""

SHARER = """<div class='sharedaddy sd-block sd-like jetpack-likes-widget-wrapper'>
<h3 class="sd-title">Like this:</h3><p>Like Loading...</p></div>"""

COMMENTS = """<div id="comments"><h3>2 Comments</h3><p>Bravo! Please write more.</p></div>"""


def post(title, chapters, next_url="", total=2):
    """One bundle page: n chapters, each an <h1> with a paragraph under it."""
    body = "".join("<h1>%s</h1><p>%s</p>" % (t, ("%s body text, long enough to be read aloud. " % t) * 3)
                   for t in chapters)
    nxt = ('<h1><a href="%s">Chapters 3-4</a></h1>' % next_url) if next_url else ""
    return """<html><head><title>%s &#8211; Keira Marcos</title></head><body>
<article><h1 class="entry-title">%s</h1>
<div class="entry-content">%s%s%s</div>
%s
</article></body></html>""" % (title, title, HEADER, body, nxt, COMMENTS + SHARER)


LANDING = """<html><head><title>A Glimpse of Us &#8211; Keira Marcos</title></head><body>
<h1 class="entry-title">A Glimpse of Us</h1>
<div class="entry-content"><p>Story summary here.</p>
<div class="responsive-tabs">
<h2 class="tabtitle">Story Posts</h2>
<div class="tabcontent">
<h1><a href="https://keiramarcos.com/2024/03/a-glimpse-of-us-1-2/">Chapters 1-2</a> |
<a href="https://keiramarcos.com/2024/03/a-glimpse-of-us-2-2/">Chapters 3-4</a></h1>
</div>
<h2 class="tabtitle">Casting</h2>
<div class="tabcontent">
<a href="https://keiramarcos.com/2024/03/a-glimpse-of-us-casting/">the cast</a>
</div>
</div></div><div class='sharedaddy sd-like'><p>Like Loading...</p></div>
</body></html>"""

P1 = "https://keiramarcos.com/2024/03/a-glimpse-of-us-1-2/"
P2 = "https://keiramarcos.com/2024/03/a-glimpse-of-us-2-2/"
WORK = "https://keiramarcos.com/fan-fiction/a-glimpse-of-us/"

PAGES = {
    WORK: LANDING,
    P1: post("A Glimpse of Us &#8211; 1/2", ["Chapter 1", "Chapter 2"], next_url=P2),
    P2: post("A Glimpse of Us &#8211; 2/2", ["Chapter 3", "Chapter 4"]),
}


class FakeFetch:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def __call__(self, url, cred=None, credential_hosts=()):
        self.asked.append(url)
        if url not in self.pages:
            raise AssertionError("reader asked for a page outside the story: %s" % url)
        return self.pages[url]


class TestAddressing(unittest.TestCase):
    def test_part_marker_is_read_from_the_slug(self):
        self.assertEqual(km.split_part("a-glimpse-of-us-2-2"), ("a-glimpse-of-us", 2, 2))

    def test_landing_page_has_no_part(self):
        self.assertEqual(km.split_part("a-glimpse-of-us"), ("a-glimpse-of-us", None, None))

    def test_a_year_or_an_id_is_not_a_part(self):
        self.assertIsNone(km.split_part("2024-03")[1])
        self.assertIsNone(km.split_part("chapter-2-90")[1])      # 90 > cap

    def test_every_url_of_the_work_names_one_book(self):
        self.assertEqual(km.identity_of(WORK), km.identity_of(P1))
        self.assertEqual(km.identity_of(P2), km.identity_of(WORK))
        self.assertNotEqual(km.identity_of(WORK),
                            km.identity_of("https://keiramarcos.com/fan-fiction/daylight/"))

    def test_a_pasted_part_page_guesses_the_work_page(self):
        self.assertEqual(km.landing_of(P2), WORK)

    def test_only_this_site_matches(self):
        k = km.Keiramarcos()
        self.assertTrue(k.matches({"kind": "url", "url": WORK}))
        self.assertTrue(k.matches({"kind": "url", "url": "https://www.keiramarcos.com/x/"}))
        self.assertFalse(k.matches({"kind": "url", "url": "https://notkeiramarcos.com/x/"}))
        self.assertFalse(k.matches({"kind": "upload"}))


class TestParse(unittest.TestCase):
    def test_chapters_come_from_headings_not_from_pages(self):
        book = km.Keiramarcos().read({"kind": "url", "url": WORK}, FakeFetch(dict(PAGES)))
        self.assertEqual(book.title, "A Glimpse of Us")
        self.assertEqual([c.title for c in book.chapters],
                         ["Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4"])
        self.assertTrue(book.chapters[2].text.startswith("Chapter 3 body"))
        self.assertEqual(book.meta["author"], "Keira Marcos")
        self.assertEqual(book.meta["word_count"], "56,004")
        self.assertEqual(book.meta["parts"], 2)
        self.assertEqual(book.meta["source"], "keiramarcos")

    def test_the_metadata_block_is_read_once_and_dropped(self):
        book = km.Keiramarcos().read({"kind": "url", "url": WORK}, FakeFetch(dict(PAGES)))
        for ch in book.chapters:
            self.assertNotIn("Author: Keira Marcos", ch.text)
            self.assertNotIn("Reading Time", ch.text)
            self.assertNotIn("Title: A Glimpse of Us", ch.text)

    def test_the_next_bundle_link_is_navigation_not_a_chapter(self):
        book = km.Keiramarcos().read({"kind": "url", "url": WORK}, FakeFetch(dict(PAGES)))
        self.assertNotIn("Chapters 3-4", [c.title for c in book.chapters])
        self.assertTrue(all("Chapters 3-4" not in c.text for c in book.chapters))

    def test_the_sharer_and_like_widget_are_not_read_aloud(self):
        book = km.Keiramarcos().read({"kind": "url", "url": WORK}, FakeFetch(dict(PAGES)))
        for ch in book.chapters:
            self.assertNotIn("Like Loading", ch.text)
            self.assertNotIn("Share this", ch.text)
            self.assertNotIn("Bravo", ch.text)

    def test_a_tab_that_is_not_the_story_is_not_crawled(self):
        f = FakeFetch(dict(PAGES))
        km.Keiramarcos().read({"kind": "url", "url": WORK}, f)
        self.assertFalse(any("casting" in u for u in f.asked))

    def test_a_standalone_post_is_one_bundle_of_chapters(self):
        # no work page reachable: the bundle page is the whole story we can see
        book = km.Keiramarcos().read({"kind": "url", "url": P2},
                                     FakeFetch({P2: PAGES[P2]}))
        self.assertEqual([c.title for c in book.chapters], ["Chapter 3", "Chapter 4"])
        self.assertEqual(book.title, "A Glimpse of Us")

    def test_pasting_the_second_bundle_still_gets_the_whole_work(self):
        f = FakeFetch(dict(PAGES))
        book = km.Keiramarcos().read({"kind": "url", "url": P2}, f)
        self.assertEqual([c.title for c in book.chapters],
                         ["Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4"])
        self.assertEqual(book.meta["url"], WORK)

    def test_a_thin_page_raises(self):
        url = "https://keiramarcos.com/2024/03/empty-1-1/"
        thin = "<html><body><p>nothing here</p></body></html>"
        with self.assertRaises(ValueError):
            km.Keiramarcos().read({"kind": "url", "url": url},
                                 FakeFetch({url: thin, km.landing_of(url): thin}))

    def test_a_series_page_is_refused_by_name_not_folded_into_one_book(self):
        from host.contracts import Refused
        series = """<html><body><h1 class="entry-title">Hold My Coffee (Series)</h1>
<div class="entry-content"><div class="responsive-tabs">
<h2 class="tabtitle">Stories</h2><div class="tabcontent">
<h1><a href="https://keiramarcos.com/2017/02/a-caged-tiger/">1. A Caged Tiger</a></h1>
<h1><a href="https://keiramarcos.com/2017/02/an-empty-coffin/">2. An Empty Coffin</a></h1>
</div><h2 class="tabtitle">Cast</h2><div class="tabcontent">
<a href="https://keiramarcos.com/fan-fiction/x/holdmycoffee/johnsheppard/">John</a>
</div></div></div></body></html>"""
        url = "https://keiramarcos.com/fan-fiction/stargate-atlantis-fan-fiction/holdmycoffee/"
        with self.assertRaises(Refused) as ctx:
            km.Keiramarcos().read({"kind": "url", "url": url}, FakeFetch({url: series}))
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("a-caged-tiger", ctx.exception.detail)

    def test_a_series_page_lists_what_is_there_with_titles_and_sizes(self):
        """The page already says how much is there: title, word count, parts.

        The live shape, from Heart & Soul: a Story/Stories tab whose entries are
        `<h2><a href="...">Title</a> (82,200)</h2>`, a work published in four
        parts listed as one work, and a work whose slug carries -N-M still being
        a work of the series rather than a bundle page of it.
        """
        series = """<html><body><h1 class="entry-title">Heart &#038; Soul (Series)</h1>
<div class="entry-content"><div class="responsive-tabs">
<h2 class="tabtitle">Stories</h2><div class="tabcontent">
<h2><a href="https://keiramarcos.com/2021/06/hearts-desire-1-3/">Heart&#8217;s Desire</a> (82,200)</h2>
<p>Years after the war.</p>
<h2><a href="https://keiramarcos.com/2021/09/soul-magic-2/">Soul Magic</a> (25,127)</h2>
<p>Loch Ness.</p>
<h2><a href="https://keiramarcos.com/2022/10/only-time-1-4/">Only Time</a> (122,430)</h2>
<p>Cast into the past.</p>
</div><h2 class="tabtitle">Casting</h2><div class="tabcontent">
<a href="https://keiramarcos.com/2021/06/hearts-desire-1-3/">Harry</a>
</div></div></div></body></html>"""
        url = "https://keiramarcos.com/fan-fiction/heart-soul-series/"
        items = km.series_items(series, url)
        self.assertEqual([i["title"] for i in items],
                         ["Heart\u2019s Desire", "Soul Magic", "Only Time"])
        self.assertEqual([i["words"] for i in items], [82200, 25127, 122430])
        self.assertEqual([i["parts"] for i in items], [3, None, 4])
        self.assertEqual([i["url"] for i in items],
                         ["https://keiramarcos.com/2021/06/hearts-desire-1-3/",
                          "https://keiramarcos.com/2021/09/soul-magic-2/",
                          "https://keiramarcos.com/2022/10/only-time-1-4/"])
        self.assertEqual(km.series_title(series), "Heart & Soul")

    def test_an_episodes_tab_is_a_series_too(self):
        series = """<html><body><h1 class="entry-title">The Vanguard (Series)</h1>
<div class="entry-content"><div class="responsive-tabs">
<h2 class="tabtitle">Episodes</h2><div class="tabcontent">
<p>1. <a href="https://keiramarcos.com/2021/10/burning-bridges/">Burning Bridges</a> (9,220 words)</p>
<p>2. <a href="https://keiramarcos.com/2021/10/unexpected-reinforcements/">Unexpected Reinforcements</a> (10,739 words)</p>
</div><h2 class="tabtitle">Casting</h2><div class="tabcontent"></div>
</div></div></body></html>"""
        url = "https://keiramarcos.com/fan-fiction/the-vanguard-series/"
        items = km.series_items(series, url)
        self.assertEqual([i["title"] for i in items],
                         ["Burning Bridges", "Unexpected Reinforcements"])
        self.assertEqual([i["words"] for i in items], [9220, 10739])

    def test_a_work_page_lists_no_works_of_its_own(self):
        """One work's Stories tab links its own parts: same family, not a series."""
        self.assertEqual(km.series_items(LANDING, WORK), [])

    def test_the_series_refusal_carries_the_list_to_pick_from(self):
        from host.contracts import Refused
        series = """<html><body><h1 class="entry-title">Hold My Coffee (Series)</h1>
<div class="entry-content"><div class="responsive-tabs">
<h2 class="tabtitle">Stories</h2><div class="tabcontent">
<h2><a href="https://keiramarcos.com/2017/02/a-caged-tiger/">A Caged Tiger</a> (41,000)</h2>
<h2><a href="https://keiramarcos.com/2017/02/an-empty-coffin/">An Empty Coffin</a> (32,000)</h2>
</div></div></div></body></html>"""
        url = "https://keiramarcos.com/fan-fiction/stargate-atlantis-fan-fiction/holdmycoffee/"
        k = km.Keiramarcos()
        with self.assertRaises(Refused) as ctx:
            k.read({"kind": "url", "url": url}, FakeFetch({url: series}))
        ser = ctx.exception.extra["series"]
        self.assertEqual(ser["title"], "Hold My Coffee")
        self.assertEqual(ser["page"], url)
        self.assertEqual([w["title"] for w in ser["works"]],
                         ["A Caged Tiger", "An Empty Coffin"])
        # and the same list is available without triggering the refusal
        self.assertEqual(k.series({"kind": "url", "url": url},
                                  FakeFetch({url: series})),
                         {"title": "Hold My Coffee", "page": url,
                          "works": km.series_items(series, url)})

    def test_series_is_none_for_a_work_page(self):
        self.assertIsNone(km.Keiramarcos().series(
            {"kind": "url", "url": WORK}, FakeFetch(dict(PAGES))))

    def test_a_stored_copy_from_a_bundle_link_is_the_same_work(self):
        k = km.Keiramarcos()
        self.assertTrue(k.same({"url": P1}, {"url": WORK}))
        self.assertFalse(k.same({"url": WORK},
                                {"url": "https://keiramarcos.com/fan-fiction/daylight/"}))


if __name__ == "__main__":
    unittest.main()