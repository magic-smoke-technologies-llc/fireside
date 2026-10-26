"""The free example book.

One bundled public-domain text every account can add in a click, so a brand
new reader has something to hear before they own a book or a voice. The text is
a plain asset file, not a download: it is the same bytes on every deployment,
which is what lets a single pre-render (see docs) warm the render cache so the
example is free to voice for everyone after that.

The mark `EXAMPLE_MARK` is written into the book's meta, so a second add finds
the copy already on the shelf instead of making another one.
"""

import json
import os

from .contracts import Book, Chapter

EXAMPLE_MARK = "happy-prince"
# The voice the example is pre-rendered in. Kept here so a deployment can name
# a different one by setting `voice` on FIRESIDE_SALE_CONNECTION; this is only
# the fallback when the sale spec carries none.
DEFAULT_VOICE = "en-US-AndrewNeural"
_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "assets", "example")
_ASSET = os.path.join(_DIR, "happy_prince.json")
# The Project Gutenberg cover for this ebook: the 1888 title page, which is
# also where the illustrators' names come from. Bundled for the same reason the
# text is, identical bytes, so a free book with no ISBN still gets a real cover
# instead of an empty square on the shelf.
_COVER = os.path.join(_DIR, "cover.jpg")

# The descriptive fields a book row can hold. The asset is the one source for
# all of them, so the shelf, the book page and the settings panel read the same
# record instead of each guessing at it.
DETAIL_FIELDS = ("author", "publisher", "published", "description")


def details():
    """The bibliographic record for the example, blanks dropped.

    Read fresh each time on purpose: the same function fills in a copy that was
    added before the record was complete, so it cannot be a constant baked at
    import."""
    with open(_ASSET, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: str(data.get(k, "")).strip() for k in DETAIL_FIELDS
            if str(data.get(k, "")).strip()}


def book():
    """The example as a host Book. Raises if the asset is missing, which is a
    deployment error, not a user error."""
    with open(_ASSET, "r", encoding="utf-8") as f:
        data = json.load(f)
    chapters = [Chapter(title=c.get("title", ""), text=c.get("text", ""))
                for c in data.get("chapters", [])]
    meta = {"example": EXAMPLE_MARK}
    meta.update(details())
    meta["source"] = data.get("source", "")
    for k in ("language", "rights"):
        if data.get(k):
            meta[k] = data[k]
    cover, mime = None, ""
    try:
        with open(_COVER, "rb") as f:
            cover = f.read()
        mime = "image/jpeg"
    except OSError:
        pass                              # no cover is fine; the text still reads
    return Book(title=data.get("title", "Example"), chapters=chapters, meta=meta,
                cover=cover, cover_mime=mime)
