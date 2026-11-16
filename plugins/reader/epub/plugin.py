"""v2 reader plugin: epub. Self-contained: the parse lives here.

Nothing above host/pluginhost.py imports a plugin by name, and this package
imports no other plugin. It uses the host's shared text helpers and the `fetch`
the host hands it -- never a socket of its own.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import Field, Refused, to_book


import html
import io
import os
import re
import hashlib
import urllib.parse
import zipfile
from host import safezip

from host.textutil import text_of


def _zip_read(z, path):
    """Read one member, allowing for the two ways an epub writes a filename.

    The href is a URL, so a file literally named "A & B.xhtml" arrives as
    "A &amp; B.xhtml", and one named "CR!AB.html" can arrive percent-encoded as
    "CR%21AB.html". The zip entry itself is neither. Try the spelling in the
    file first, then the decoded forms, and only then give up.
    """
    seen = []
    for cand in (path, html.unescape(path), urllib.parse.unquote(path),
                 urllib.parse.unquote(html.unescape(path))):
        if cand in seen:
            continue
        seen.append(cand)
        try:
            return safezip.read(z, cand)
        except KeyError:
            continue
    raise KeyError(path)


def _tag(name):
    """A tag pattern that accepts an XML namespace prefix.

    EPUB 3 allows the package document to prefix its own elements
    (<opf:item>, <opf:itemref>, <opf:spine>), and real books do it. A parser
    that only knows the unprefixed spelling reads those files as a title with
    no chapters at all and refuses a perfectly good book.
    """
    return r"(?:[A-Za-z0-9_.-]+:)?" + name


def _first_heading(xhtml: str, fallback: str) -> str:
    m = re.search(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", xhtml)
    if m:
        t = text_of(m.group(1))
        if t:
            return t[:120]
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", xhtml)
    if m:
        t = text_of(m.group(1))
        if t:
            return t[:120]
    return fallback


_IMAGE_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
              "svg": "image/svg+xml"}


def _isbn_of(opf: str) -> str:
    """The first dc:identifier that looks like an ISBN (10 or 13), else ""."""
    for m in re.finditer(r"(?is)<dc:identifier[^>]*>(.*?)</dc:identifier>", opf):
        v = text_of(m.group(1)).strip()
        v = re.sub(r"(?i)^urn:isbn:", "", v)
        v = re.sub(r"[^0-9Xx]", "", v)
        if re.fullmatch(r"\d{9}[\dXx]", v) or re.fullmatch(r"\d{13}", v):
            return v.upper()
    return ""


def _meta_of(opf: str) -> dict:
    """What the file says about itself, taken only where it actually says it.

    dc:creator is the author. calibre:series / calibre:series_index is the one
    place an ebook reliably carries its series. Nothing is inferred from the
    title: a series guessed out of "Book 2 of the Whatever" is wrong often
    enough that a blank field the reader can fill is better. Absent means "".
    """
    out = {}
    for key, tag in (("author", "dc:creator"), ("series", "calibre:series"),
                     ("series_index", "calibre:series_index"),
                     ("publisher", "dc:publisher"), ("published", "dc:date")):
        m = (re.search(r"(?is)<" + tag + r"[^>]*>(.*?)</" + tag + r">", opf)
             or re.search(r'(?is)<' + _tag("meta") + r'[^>]*name="' + tag + r'"[^>]*content="([^"]*)"', opf)
             or re.search(r'(?is)<' + _tag("meta") + r'[^>]*property="' + tag + r'"[^>]*>(.*?)</' + _tag("meta") + r'>', opf))
        if m:
            v = text_of(m.group(1)).strip()
            if v:
                out[key] = v[:255]
    # the EPUB3 spelling of the same two facts
    for key, prop in (("series", "belongs-to-collection"),
                      ("series_index", "group-position")):
        if out.get(key):
            continue
        m = re.search(r'(?is)<' + _tag("meta") + r'[^>]*property="' + prop + r'"[^>]*>(.*?)</' + _tag("meta") + r'>', opf)
        if m:
            v = text_of(m.group(1)).strip()
            if v:
                out[key] = v[:255]
    return out


def _subjects_of(opf: str):
    """Every dc:subject the file declares, in order, deduped, capped.

    This is the only place an ebook reliably says what it is about, and it is
    what the shelf's filter chips are built from. It is a suggestion from the
    publisher, not a truth: the import marks these tags as coming from the
    file so a reader's own tag is never confused with one.
    """
    out = []
    for m in re.finditer(r"(?is)<dc:subject[^>]*>(.*?)</dc:subject>", opf):
        v = text_of(m.group(1)).strip()
        if v and v.lower() not in [x.lower() for x in out]:
            out.append(v[:40])
        if len(out) >= 12:
            break
    return out


def _cover_of(z, manifest, opf: str, base: str):
    """The epub's own cover image as (bytes, mime), or None.

    Three ways an epub names its cover, tried in order: the EPUB2
    <meta name="cover"> id, the EPUB3 properties="cover-image" flag, then any
    image whose href says cover. Falling back to the first image at all keeps a
    slightly-off epub from losing its art.
    """
    href = ""
    m = re.search(r'(?is)<' + _tag("meta") + r'[^>]*name="cover"[^>]*content="([^"]+)"', opf)
    if m and m.group(1) in manifest:
        href = manifest[m.group(1)][0]
    if not href:
        for h, _mt, props in manifest.values():
            if "cover-image" in (props or ""):
                href = h
                break
    if not href:
        for h, mt, _props in manifest.values():
            if mt.startswith("image/") and "cover" in h.lower():
                href = h
                break
    if not href:
        for h, mt, _props in manifest.values():
            if mt.startswith("image/"):
                href = h
                break
    if not href:
        return None
    p = (href if not base else os.path.join(base, href)).replace("\\", "/")
    try:
        data = _zip_read(z, p)
    except KeyError:
        return None
    if not data:
        return None
    ext = p.rsplit(".", 1)[-1].lower() if "." in p else "jpg"
    return data, _IMAGE_EXT.get(ext, "image/jpeg")


def parse_epub(data: bytes, name: str) -> dict:
    """bytes of an .epub -> {'title':..., 'chapters':[{'title','text'}...]}"""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()

    opf_path = None
    if "META-INF/container.xml" in names:
        m = re.search(r'full-path="([^"]+)"',
                      safezip.read(z, "META-INF/container.xml").decode("utf-8", "replace"))
        if m:
            opf_path = html.unescape(m.group(1))
    if not opf_path:
        opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
    if not opf_path:
        raise ValueError("no .opf in this epub")

    opf = safezip.read(z, opf_path).decode("utf-8", "replace")
    base = os.path.dirname(opf_path)

    title = "Untitled"
    m = re.search(r"(?is)<dc:title[^>]*>(.*?)</dc:title>", opf)
    if m:
        title = text_of(m.group(1)) or title

    manifest = {}
    for m in re.finditer(r"(?is)<" + _tag("item") + r"\b[^>]*/?>", opf):
        tag = m.group(0)
        i = re.search(r'id="([^"]+)"', tag)
        h = re.search(r'href="([^"]+)"', tag)
        mt = re.search(r'media-type="([^"]+)"', tag)
        pr = re.search(r'properties="([^"]+)"', tag)
        if i and h:
            # href is an XML attribute, so an ampersand in a filename arrives
            # as &amp; while the zip entry is named with a bare &. Unescaping
            # is what lets a book whose chapter files carry & be read at all.
            manifest[i.group(1)] = (html.unescape(h.group(1)),
                                    (mt.group(1) if mt else ""),
                                    (pr.group(1) if pr else ""))

    order = []
    for m in re.finditer(r"(?is)<" + _tag("itemref") + r"\b[^>]*/?>", opf):
        i = re.search(r'idref="([^"]+)"', m.group(0))
        if i and i.group(1) in manifest:
            order.append(manifest[i.group(1)])

    chapters = []
    for idx, (href, mt, _props) in enumerate(order):
        if "html" not in mt and not href.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        p = href if not base else os.path.join(base, href)
        p = p.replace("\\", "/")
        try:
            raw = _zip_read(z, p).decode("utf-8", "replace")
        except KeyError:
            continue
        txt = text_of(raw)
        if len(txt) < 2:
            continue
        chapters.append({"title": _first_heading(raw, "Chapter %d" % (len(chapters) + 1)),
                         "text": txt})

    if not chapters:
        raise ValueError("no readable chapters found")
    out = {"title": title, "chapters": chapters, "isbn": _isbn_of(opf)}
    out.update(_meta_of(opf))
    out["subjects"] = _subjects_of(opf)
    cov = _cover_of(z, manifest, opf, base)
    if cov:
        out["cover"], out["cover_mime"] = cov
    return out


def identity_of(data: bytes, name: str = "", isbn: str = "") -> str:
    """One book, however its file is named: the ISBN when the epub carries one
    (a re-download off a store is the same book), else a hash of the bytes."""
    if isbn:
        return "epub:isbn:" + isbn
    return "epub:sha:" + hashlib.sha256(data or b"").hexdigest()


class Epub:
    id = "epub"
    label = "EPUB file"

    def matches(self, req) -> bool:
        """Any upload? Not any more: epub, pdf and docx all arrive as uploads,
        and only one of them should claim the bytes. An epub is a zip with a
        META-INF/container.xml; a docx is a zip with word/document.xml and no
        container. Asking by content means a .docx renamed .epub still imports
        correctly, and an extension is never the whole answer."""
        if req.get("kind") != "upload":
            return False
        data = req.get("data") or b""
        if data[:5] == b"%PDF-":
            return False
        if data[:4] != b"PK\x03\x04":
            # A bare .epub extension on something that is not a zip: let it
            # through only if the name says so, so the parse can refuse it with
            # a real message instead of "no reader matched".
            return (req.get("filename") or "").lower().endswith(".epub")
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except (zipfile.BadZipFile, OSError):
            # Not a zip at all. Claim it only if the name says epub, so the
            # parse can refuse it with a reason instead of the host answering
            # "no reader matched" for a file that was plainly meant to be one.
            return (req.get("filename") or "").lower().endswith(".epub")
        if "META-INF/container.xml" in names:
            return True
        return not any(n == "word/document.xml" for n in names)

    def read(self, req, fetch, cred=None) -> dict:
        try:
            book = parse_epub(req["data"], req.get("filename") or "book.epub")
        except (zipfile.BadZipFile, OSError, KeyError) as e:
            # A truncated download or a renamed file is a bad file, not a crash.
            raise Refused("not an epub",
                          "this file could not be opened as an epub (%s)" % type(e).__name__)
        book["meta"] = {"source": self.id}
        if book.get("isbn"):
            book["meta"]["isbn"] = book["isbn"]
        for key in ("author", "series", "series_index", "publisher", "published"):
            if book.get(key):
                book["meta"][key] = book[key]
        if book.get("subjects"):
            book["meta"]["subjects"] = list(book["subjects"])
        book["meta"]["identity"] = identity_of(
            req.get("data") or b"", req.get("filename") or "", book.get("isbn") or "")
        return to_book(book)

    def same(self, stored, incoming) -> bool:
        a = (stored or {}).get("identity") or ""
        b = (incoming or {}).get("identity") or ""
        return bool(a and a == b)

    def settings(self):
        return []

    def credential_hosts(self):
        return []
