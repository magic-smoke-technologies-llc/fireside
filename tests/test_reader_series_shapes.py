"""Two shapes of series listing on one site.

Cat pasted a series page and the picker offered four works called "Post 1",
"Post 2", "Post 1", "Post 2". The page lists two works, each published as two
posts:

    <h4>A Guardian's Path ~ 80,835 Words ~ <a>Post 1</a> | <a>Post 2</a></h4>

The other shape puts the title in the link itself, with the word count after it:

    <h2><a href="...">Heart's Desire</a> (82,200)</h2>

Reading anchors alone cannot tell "a part of this work" from "a work of this
series", so the heading is the unit, and a link that only says "Post 1" is a
part, not a book.
"""
import unittest

from plugins.reader.keiramarcos import plugin as km

BASE = "https://keiramarcos.com/fan-fiction/the-genesis-of-a-jedi-series/"

HEADING_SHAPE = """<html><body>
<h1 class="entry-title">The Genesis of a Jedi ~ Series ~</h1>
<div class="entry-content">
<div class="responsive-tabs">
<h2 class="tabtitle">Story Posts</h2>
<div class="tabcontent">
<h4>A Guardian&#8217;s Path ~ 80,835 Words ~
<a href="https://keiramarcos.com/2024/05/a-guardians-path-1-2/">Post 1</a> |
<a href="https://keiramarcos.com/2024/05/a-guardians-path-2-2/">Post 2</a></h4>
<p>Obi-Wan Kenobi has no interest whatsoever in being an emotional support animal.</p>
<h4>Trial by Fire ~ 58,781 Words ~
<a href="https://keiramarcos.com/2024/08/trial-by-fire-1-2/">Post 1</a> |
<a href="https://keiramarcos.com/2024/08/trial-by-fire-2-2/">Post 2</a></h4>
<p>Betrayal from within leaves Jedi Master Jaro Tapal in the hands of pirates.</p>
</div>
<h2 class="tabtitle">Casting</h2>
<div class="tabcontent"><a href="https://keiramarcos.com/cast/">the cast</a></div>
</div></div></body></html>"""

LINK_SHAPE = """<html><body>
<h1 class="entry-title">Heart &amp; Soul ~Series~</h1>
<div class="entry-content">
<div class="responsive-tabs">
<h2 class="tabtitle">Story Posts</h2>
<div class="tabcontent">
<h2><a href="https://keiramarcos.com/2021/06/hearts-desire-1-3/">Heart&#8217;s Desire</a> (82,200)</h2>
<p>Years after the war.</p>
<h2><a href="https://keiramarcos.com/2021/09/soul-magic-2/">Soul Magic</a> (25,127)</h2>
<p>Harry Potter is living a quiet life.</p>
</div></div></div></body></html>"""


class TestHeadingShape(unittest.TestCase):
    def setUp(self):
        self.items = km.series_items(HEADING_SHAPE, BASE)

    def test_two_works_not_four_posts(self):
        self.assertEqual(len(self.items), 2)

    def test_the_title_is_the_heading_not_the_link_label(self):
        self.assertEqual([i["title"] for i in self.items],
                         ["A Guardian\u2019s Path", "Trial by Fire"])

    def test_the_word_count_is_read_from_the_heading(self):
        self.assertEqual([i["words"] for i in self.items], [80835, 58781])

    def test_each_work_says_how_many_parts_it_was_published_in(self):
        self.assertEqual([i["parts"] for i in self.items], [2, 2])

    def test_the_first_part_is_the_url_that_lands_the_whole_work(self):
        # the reader follows a post's sibling parts, so part 1 imports both
        self.assertTrue(self.items[0]["url"].endswith("a-guardians-path-1-2/"))

    def test_the_page_it_came_from_is_still_a_refusal(self):
        # a link in this page's own family is this page's material, not a work
        page = HEADING_SHAPE.replace(
            "a-guardians-path-2-2/", "the-genesis-of-a-jedi-series/")
        self.assertEqual(len(km.series_items(page, BASE)), 2)


class TestLinkShape(unittest.TestCase):
    def test_title_and_count_come_from_the_link_and_what_follows_it(self):
        items = km.series_items(LINK_SHAPE, "https://keiramarcos.com/fan-fiction/heart-soul-series/")
        self.assertEqual([i["title"] for i in items], ["Heart\u2019s Desire", "Soul Magic"])
        self.assertEqual([i["words"] for i in items], [82200, 25127])
        # "hearts-desire-1-3" is part 1 of 3: a work in three parts, not three works
        self.assertEqual(items[0]["parts"], 3)


class TestTheSeriesTitle(unittest.TestCase):
    def test_the_site_tail_is_cut_off(self):
        self.assertEqual(km.series_title(HEADING_SHAPE), "The Genesis of a Jedi")
        self.assertEqual(km.series_title(LINK_SHAPE), "Heart & Soul")


if __name__ == "__main__":
    unittest.main()