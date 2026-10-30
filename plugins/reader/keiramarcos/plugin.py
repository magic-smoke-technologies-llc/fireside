"""v2 reader plugin: keiramarcos.com. One work, published as a landing page
that links out to posts, each post holding several chapters.

The shape, in the order the site actually uses it:

    /fan-fiction/a-glimpse-of-us/          <- landing page (a WordPress PAGE)
        Story Posts tab:                    Chapters 1-5 | Chapters 6-10
    /2024/03/a-glimpse-of-us-1-2/          <- a post (h1 per chapter inside)
    /2024/03/a-glimpse-of-us-2-2/          <- another post

So a chapter is not a page here; a page is a bundle of chapters. Give this
reader any URL of the work and it gathers the bundle pages, fetches each once,
and splits each page on its own <h1> headings, so the book comes out with one
chapter per chapter rather than one chapter per page.

Two things this site does that must not reach the speaker:

* Every bundle page opens with the same metadata block (Title / Author /
  Fandom / Warnings / Summary, plus a "Reading Time: 115 Minutes" line). It is
  read once, into the book's meta, and dropped from the first chapter.
* Each bundle page ends with a "Chapters 6-10" link to the next bundle. That is
  navigation, not a chapter title, so it is used to widen the set and never
  becomes a heading.

The URL family is the guard, exactly as in the quantumbang reader: a part page
slug carries -N-M (a-glimpse-of-us-1-2 is part 1 of 2), and the landing page
slug is the family, so a stray link out of the story can never join the book.
"""

import os
import re
import sys
import urllib.parse

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import Refused, to_book
from host.textutil import clean_fragment, strip_furniture

HOSTS = ("keiramarcos.com",)
MAX_PARTS = 60                     # a real story, not a crawl

_HREF = re.compile(r"""(?is)<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""")
# <=N-M> anywhere in a part slug: part N of M. Bounded so a year or an id
# cannot pass for a part number.
_PART = re.compile(r"-(?P<n>\d{1,2})-(?P<m>\d{1,2})(?=$|[/?#])")
# a tab title that means "the story lives here": Story, Story Posts, The Story,
# Story Chapters, a series of Episodes or Books. Casting / Author Notes /
# Playlist are tabs too and must not win.
_STORY_TAB = re.compile(r"(?i)\b(story|stories|chapter|chapters|part|parts|"
                        r"episode|episodes|book|books)\b")
_TAB = re.compile(r"""(?is)<h2\b[^>]*class="[^"]*\btabtitle\b[^"]*"[^>]*>(.*?)</h2>""")
# a dated post URL on this site: /2024/03/a-glimpse-of-us-1-2/
_POST_PATH = re.compile(r"^/20\d\d/\d\d/[^/]+/?$")
_H1 = re.compile(r"(?is)<h1\b[^>]*>(.*?)</h1>")
# "Chapters 6-10" / "Part 2 of 3" as a heading: navigation, not a chapter. A
# bare "Chapter 1" is a chapter, so only a span counts as navigation.
_NAV_HEAD = re.compile(r"(?i)^(?:chapters?|parts?|posts?)\s+\d+\s*(?:-|\u2013|\u2014|to)\s*\d+$"
                       r"|^(?:chapters?|parts?|posts?)\s+\d+\s+of\s+\d+$")
# Wordpress reading-time badge and other post-wide junk
_JUNK_P = re.compile(r'(?is)<p\b[^>]*class="[^"]*wpwc-reading-time[^"]*"[^>]*>.*?</p>')
# the sharer and the Jetpack "Like this" widget sit after "The End" and would
# otherwise be read aloud as the last paragraph of the story
_JUNK_BLOCK = re.compile(r"""(?is)<div\b[^>]*\bclass\s*=\s*["'][^"']*\b(?:jp-post-flair|jetpack-likes-widget-wrapper|sharedaddy)\b[^"']*["'][^>]*>""")
# a post title carries its part marker: "Daylight - 1/2"
_PART_TITLE = re.compile(r"\s*[-\u2013\u2014]\s*\d+\s*/\s*\d+\s*$")
_ENTRY = re.compile(r'(?is)<div\b[^>]*class="[^"]*\bentry-content\b[^"]*"[^>]*>')
# "Title: ...", "Content Rating: PG-13", "Relationship(s): Gen"
_FIELD = re.compile(r"^[A-Z][A-Za-z0-9 ()/.'\u2019-]{1,40}:\s*\S")
MAX_HEADER_PARAGRAPHS = 16
MAX_PAGES = 60

_TAGNAME = re.compile(r"(?is)<(/?)([a-z][a-z0-9]*)\b[^>]*?(/?)>")


# ---- addressing ---------------------------------------------------------------

def _host(url: str) -> str:
    return (urllib.parse.urlparse(url or "").hostname or "").lower()


def is_keiramarcos(url: str) -> bool:
    h = _host(url)
    if not any(h == x or h.endswith("." + x) for x in HOSTS):
        return False
    return urllib.parse.urlparse(url).scheme in ("http", "https")


def path_of(url: str) -> str:
    return (urllib.parse.urlparse(url).path or "").strip("/").lower()


def last_segment(url: str) -> str:
    parts = [p for p in path_of(url).split("/") if p]
    return parts[-1] if parts else ""


def is_post_url(url: str) -> bool:
    return bool(_POST_PATH.match(urllib.parse.urlparse(url).path or ""))


def split_part(slug: str):
    """(family, n, of) for a part slug; (slug, None, None) for a landing page."""
    for m in _PART.finditer(slug):
        n, total = int(m.group("n")), int(m.group("m"))
        if not (1 <= n <= total <= MAX_PARTS):
            continue
        return (slug[:m.start()] + slug[m.end():]).strip("-"), n, total
    return slug, None, None


def family_of(url: str) -> str:
    return split_part(last_segment(url))[0]


def identity_of(url: str) -> str:
    """One work however it is addressed: the landing slug, or the part slug with
    the -N-M cut out. Any bundle page and the landing page name the same book."""
    return "keiramarcos:" + family_of(url)


def landing_of(url: str) -> str:
    """A guessed landing URL for a part page: the family slug under /fan-fiction/.
    Used only when the caller pasted a bundle page instead of the work page."""
    fam = family_of(url)
    u = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(u._replace(path="/fan-fiction/%s/" % fam,
                                              query="", fragment=""))


# ---- reading one page ---------------------------------------------------------

def _element(page: str, start: int):
    """Slice one balanced <div> from its opening tag. A lazy regex ends at the
    first inner close, which truncates exactly the pages worth reading."""
    depth = 0
    for m in _TAGNAME.finditer(page, start):
        if m.group(2).lower() != "div" or m.group(3):
            continue
        if m.group(1):
            depth -= 1
            if depth == 0:
                return page[start:m.end()]
        else:
            depth += 1
    return page[start:]


def drop_widgets(html: str) -> str:
    """Remove the post-flair / share / like widgets, whole div and all."""
    out, last = [], 0
    for m in _JUNK_BLOCK.finditer(html):
        if m.start() < last:
            continue
        out.append(html[last:m.start()])
        last = m.start() + len(_element(html, m.start()))
    out.append(html[last:])
    return "".join(out)


def content_region(page: str) -> str:
    """The post body: the entry-content div if there is one, else the page."""
    m = _ENTRY.search(page)
    if m:
        return _element(page, m.start())
    return page


def links_in(html: str, base_url: str) -> list:
    """Absolute same-host links, in page order, deduped, no query/fragment."""
    base_host = _host(base_url)
    out, seen = [], set()
    for raw in _HREF.findall(html or ""):
        url = urllib.parse.urljoin(base_url, raw.strip())
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or _host(url) != base_host:
            continue
        clean = urllib.parse.urlunparse(u._replace(query="", fragment=""))
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def story_tab_html(page: str) -> str:
    """The markup of the tab that holds the story, if the tab markup is there."""
    tabs = list(_TAB.finditer(page))
    for i, m in enumerate(tabs):
        end = tabs[i + 1].start() if i + 1 < len(tabs) else len(page)
        if _STORY_TAB.search(clean_fragment(m.group(1))):
            return page[m.end():end]
    return ""


def story_tab_links(page: str, url: str) -> list:
    """Links out of the tab that holds the story, if the tab markup is there."""
    chosen = story_tab_html(page)
    return links_in(chosen, url) if chosen else []


def part_links(page: str, url: str) -> dict:
    """{(n, of): url} for every bundle page of this family this page points at."""
    family = family_of(url)
    found = {}
    fam, n, of = split_part(last_segment(url))
    if n is not None and is_post_url(url):
        found[(n, of)] = url
    for link in story_tab_links(page, url) + links_in(content_region(page), url):
        if not is_post_url(link):
            continue
        f, pn, pm = split_part(last_segment(link))
        if f != family or pn is None:
            continue
        found.setdefault((pn, pm), link)
    return found


# A work listed on a series page: the title anchor, then "(82,200)" or
# "(9,220 words)" a few characters later.
_ANCHOR = re.compile(r'(?is)<a\b[^>]*?\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>')
_WORDS = re.compile(r"(?is)\((?:approx\.?\s*)?([\d,]{3,})\s*(?:words?)?\)")
_LEADING_NUM = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*")
_SERIES_SUFFIX = re.compile(r"\s*\((?:series|episodes|books?)\)\s*$", re.IGNORECASE)
MAX_SERIES_WORKS = 40              # a series, not a sitemap


def _words_in(tail: str):
    m = _WORDS.search(tail or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def series_items(page: str, url: str) -> list:
    """The works a series landing page lists, in reading order.

    A ~Series~ page is not one story: its Stories tab (or Episodes, or Books)
    links out to N unrelated posts, each a whole work of its own. What separates
    a work of this series from a bundle page of one work is the URL family: a
    bundle page carries the same family as its work's landing page, so a link
    whose family is this page's family is this page's own material and nothing
    to offer. A listed work may itself be published in -N-M parts ("Only Time
    - 1/4"); that is a work with four parts, not four works.

    Each item carries what the page already says about it, so a picker can show
    how much is there without opening a single work: title, word count (the
    site prints it after the link), and part count from the slug.
    """
    seg = story_tab_html(page)
    if not seg:
        return []
    here = family_of(url)
    base_host = _host(url)
    out, seen = [], set()
    for m in _ANCHOR.finditer(seg):
        link = urllib.parse.urljoin(url, m.group(1).strip())
        u = urllib.parse.urlparse(link)
        if u.scheme not in ("http", "https") or _host(link) != base_host:
            continue
        clean = urllib.parse.urlunparse(u._replace(query="", fragment=""))
        if not is_post_url(clean) or clean in seen:
            continue
        fam, n, of = split_part(last_segment(clean))
        if fam == here:
            continue
        seen.add(clean)
        title = clean_fragment(m.group(2))
        title = _LEADING_NUM.sub("", title)[:160].strip()
        out.append({"url": clean,
                    "title": title,
                    "words": _words_in(seg[m.end():m.end() + 120]),
                    "parts": of if n is not None else None})
        if len(out) >= MAX_SERIES_WORKS:
            break
    return out


def series_works(page: str, url: str) -> list:
    """Just the URLs of series_items, for callers that only need those."""
    return [i["url"] for i in series_items(page, url)]


def series_title(page: str) -> str:
    """The series name, with the site's "(Series)" tail cut off."""
    return _SERIES_SUFFIX.sub("", post_title(page)).strip()


def ordered_parts(found: dict, url: str) -> list:
    """Parts 1..of in order, for the most complete consistent run. Falls back to
    whatever was linked, in part order, when the run is not whole."""
    by_total = {}
    for (n, of) in found:
        by_total.setdefault(of, set()).add(n)
    for of, ns in sorted(by_total.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if all(i in ns for i in range(1, of + 1)):
            return [found[(i, of)] for i in range(1, of + 1)]
    return [found[k] for k in sorted(found)]


# ---- one page -> chapters -----------------------------------------------------

def split_chapters(html: str, fallback_title: str) -> list:
    """Split a bundle page on its own <h1> headings. One chapter per heading.

    The site uses <h1> for chapter titles and nothing else inside the body, and
    headings that are really navigation ("Chapters 6-10") are dropped, so each
    piece of text belongs to the chapter whose heading opened it."""
    heads = list(_H1.finditer(html))
    if not heads:
        # no chapter headings: one chapter only if there is a real body here
        text = clean_fragment(html)
        return [{"title": fallback_title, "text": text}] if len(text) >= 120 else []
    out = []
    prelude = html[:heads[0].start()]
    for i, m in enumerate(heads):
        seg = html[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(html)]
        title = clean_fragment(m.group(1))[:160].strip()
        text = clean_fragment(seg)
        if _NAV_HEAD.match(title):
            # navigation: keep the link (already collected) and the words out
            continue
        if len(text) < 2:
            continue
        out.append({"title": title or fallback_title, "text": text})
    if not out:
        text = clean_fragment(prelude) or clean_fragment(html)
        if len(text) >= 120:
            out.append({"title": fallback_title, "text": text})
    return out


def split_header(text: str):
    """(fields, body) for the repeated metadata block at the top of a bundle.

    Only a run of at least three `Key: value` paragraphs counts, so a story
    paragraph that happens to contain a colon is never eaten."""
    paras = text.split("\n\n")
    fields, end = {}, 0
    for i, p in enumerate(paras):
        if i >= MAX_HEADER_PARAGRAPHS:
            break
        line = p.strip()
        if not line:
            end = i + 1
            continue
        if line.startswith("* * * *"):
            end = i + 1
            continue
        if not _FIELD.match(line):
            break
        key, _, val = line.partition(":")
        fields[key.strip()] = val.strip()
        end = i + 1
    if len(fields) < 3:
        return {}, text
    return fields, "\n\n".join(paras[end:]).strip()


def post_title(page: str) -> str:
    m = re.search(r'(?is)<h1\b[^>]*class="[^"]*\bentry-title\b[^"]*"[^>]*>(.*?)</h1>', page)
    if m:
        t = clean_fragment(m.group(1))
        if t:
            return t[:200]
    return ""


# ---- the book -----------------------------------------------------------------

class Keiramarcos:
    id = "keiramarcos"
    label = "Keira Marcos story"

    def matches(self, req) -> bool:
        return req.get("kind") == "url" and is_keiramarcos(req.get("url") or "")

    def read(self, req, fetch, cred=None):
        url = req["url"]
        pages = {}
        first = None
        if is_post_url(url):
            # a bundle page was pasted: try the work page first so a part 2
            # paste still yields the whole book
            guess = landing_of(url)
            try:
                landing = fetch(guess, cred=cred, credential_hosts=self.credential_hosts())
            except Exception:
                landing = ""
            if landing and family_of(guess) and content_region(landing):
                first = (guess, landing)
        if first is None:
            first = (url, fetch(url, cred=cred, credential_hosts=self.credential_hosts()))
        start_url, start_page = first
        pages[start_url] = start_page

        works = series_items(start_page, start_url)
        if len(works) >= 2:
            # One import, one book. A series page is several books, and the
            # honest answer is to list them and let the reader pick, not to
            # fold them into one nor to make them paste links one at a time.
            raise Refused(
                "that is a series page",
                "this page lists %d works of its own. Pick the ones you want: %s"
                % (len(works), works[0]["url"]), status=422,
                series={"title": series_title(start_page) or "Series",
                        "page": start_url, "works": works})

        found = part_links(start_page, start_url)
        ordered = ordered_parts(found, start_url) if found else [start_url]
        if is_post_url(url) and url not in ordered:
            ordered = [url] + ordered

        fetched = []
        for part_url in ordered[:MAX_PARTS]:
            page = pages.get(part_url)
            if page is None:
                page = fetch(part_url, cred=cred,
                             credential_hosts=self.credential_hosts())
                pages[part_url] = page
            fetched.append((part_url, page))

        book = self._assemble(fetched, start_url)
        book["meta"]["parts"] = len(fetched)
        book["meta"]["identity"] = identity_of(start_url)
        return to_book(book)

    def series(self, req, fetch, cred=None):
        """What this page holds, if it is a series page: {title, page, works}.

        The host calls this again before importing a selection, so a picker can
        never make it fetch a URL that was not linked from the page it named.
        """
        url = req["url"]
        page = fetch(url, cred=cred, credential_hosts=self.credential_hosts())
        works = series_items(page, url)
        if len(works) < 2:
            return None
        return {"title": series_title(page) or "Series", "page": url, "works": works}

    def _assemble(self, fetched: list, work_url: str) -> dict:
        chapters, meta, title = [], {}, ""
        for i, (part_url, page) in enumerate(fetched):
            body = _JUNK_P.sub(" ", content_region(drop_widgets(strip_furniture(page))))
            fallback = _PART_TITLE.sub("", post_title(page)) or ("Part %d" % (i + 1))
            if i == 0:
                # the metadata block sits above the first chapter heading, so
                # it never lands in a chapter: read it here, once.
                first_h1 = _H1.search(body)
                prelude = body[:first_h1.start()] if first_h1 else body
                fields, _rest = split_header(clean_fragment(prelude))
                if fields:
                    title = fields.get("Title", "") or title
                    for k in ("Author", "Fandom", "Summary", "Word Count"):
                        if fields.get(k):
                            meta[k.lower().replace(" ", "_")] = fields[k]
            pieces = split_chapters(body, fallback)
            if not pieces and len(fetched) == 1:
                raise ValueError("no readable text on that page (a login wall, "
                                 "or the story builds itself in JavaScript)")
            for j, ch in enumerate(pieces):
                text = ch["text"]
                if i == 0 and j == 0:
                    fields, text = split_header(text)
                    if fields:
                        title = title or fields.get("Title", "")
                        for k in ("Author", "Fandom", "Summary", "Word Count"):
                            if fields.get(k) and k.lower() not in meta:
                                meta[k.lower().replace(" ", "_")] = fields[k]
                if len(text) < 2:
                    continue
                chapters.append({"title": ch["title"], "text": text})
        if not chapters:
            raise ValueError("could not find any chapter text on that page "
                             "(site markup may have changed)")
        if not title:
            title = _PART_TITLE.sub("", post_title(fetched[0][1])) or "Untitled"
        meta.update({"source": self.id, "url": work_url})
        return {"title": title[:200], "chapters": chapters, "meta": meta}

    def same(self, stored, incoming) -> bool:
        """A work already on the shelf, from any of its bundle links or its
        landing page, is this work."""
        a = (stored or {}).get("url") or ""
        b = (incoming or {}).get("url") or ""
        if not a or not b:
            ia = (stored or {}).get("identity") or ""
            ib = (incoming or {}).get("identity") or ""
            return bool(ia and ia == ib)
        if not is_keiramarcos(a):
            return False
        return identity_of(a) == identity_of(b)

    def settings(self):
        return []

    def credential_hosts(self):
        return []