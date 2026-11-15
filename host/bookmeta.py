"""Book metadata from outside: covers by URL or ISBN, details by ISBN.

Plain functions with no hold on the app, split out of host/app.py. The two that
reach the network take their gate or fetcher as an argument, so a test (or a
caller with a different policy) can swap it without patching module state.
"""

import json
import re
import urllib.request

from host import netguard

COVER_MAX_BYTES = 8 * 1024 * 1024
COVER_URLS = ("https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false",)

# a cover URL is user input, so it only ever reaches the public internet:
# http(s), an image content-type (or a recognised image signature), and a
# host that does not resolve into a private / loopback / link-local range.
IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"),
               (b"\x89PNG\r\n\x1a\n", "image/png"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
               (b"RIFF", "image/webp"))

# An ISBN is the key to a book's public metadata. OpenLibrary is keyless, so
# this works on a fresh install. Nothing here overwrites the book's own title.
DETAILS_MAX_CHARS = 4000
OL_EDITION = "https://openlibrary.org/isbn/{isbn}.json"
OL_KEY = "https://openlibrary.org{key}.json"


def norm_isbn(isbn):
    return re.sub(r"[^0-9Xx]", "", str(isbn or ""))


def valid_isbn(isbn):
    return bool(re.fullmatch(r"\d{9}[\dXx]|\d{13}", isbn or ""))


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "fireside/2 (book details)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read(4 * 1024 * 1024).decode("utf-8", "replace"))


def cover_by_isbn(isbn):
    """Fetch a cover for an ISBN from a keyless public source, or None."""
    isbn = re.sub(r"[^0-9Xx]", "", isbn or "")
    if not re.fullmatch(r"\d{9}[\dXx]|\d{13}", isbn or ""):
        return None
    for tpl in COVER_URLS:
        url = tpl.format(isbn=isbn)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "fireside/2 (cover lookup)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0]
                data = r.read(COVER_MAX_BYTES)
            if data and len(data) > 100:
                return data, (ctype if ctype.startswith("image/") else "image/jpeg")
        except Exception:
            continue
    return None


def cover_from_url(url, allowed=netguard.is_public_url):
    if not allowed(url):
        return None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "fireside/2 (cover fetch)"})
        with netguard.opener().open(req, timeout=20) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = r.read(COVER_MAX_BYTES + 1)
    except Exception:
        return None
    if not data or len(data) > COVER_MAX_BYTES:
        return None
    sniff = ""
    for magic, m in IMAGE_MAGIC:
        if data.startswith(magic):
            sniff = m
            break
    if ctype.startswith("image/"):
        mime = ctype
    elif sniff:
        mime = sniff
    else:
        return None
    return data, mime


def details_by_isbn(isbn, get_json=get_json):
    """Public metadata for an ISBN from OpenLibrary, or None."""
    isbn = norm_isbn(isbn)
    if not valid_isbn(isbn):
        return None
    try:
        ed = get_json(OL_EDITION.format(isbn=isbn))
    except Exception:
        return None
    out = {"isbn": isbn}
    title = ed.get("title") or ""
    if title and ed.get("subtitle"):
        title += ": " + ed["subtitle"]
    if title:
        out["title"] = title
    names = []
    for a in (ed.get("authors") or [])[:6]:
        key = a.get("key") if isinstance(a, dict) else None
        if not key:
            continue
        try:
            n = get_json(OL_KEY.format(key=key)).get("name")
        except Exception:
            n = ""
        if n:
            names.append(n)
    if not names and ed.get("by_statement"):
        names = [ed["by_statement"]]
    if names:
        out["author"] = ", ".join(names)
    pubs = [p for p in (ed.get("publishers") or []) if p][:3]
    if pubs:
        out["publisher"] = ", ".join(pubs)
    if ed.get("publish_date"):
        out["published"] = str(ed["publish_date"])
    desc = ed.get("description")
    if not desc:
        works = ed.get("works") or []
        if works and isinstance(works[0], dict) and works[0].get("key"):
            try:
                desc = get_json(
                    OL_KEY.format(key=works[0]["key"])).get("description")
            except Exception:
                desc = None
    if isinstance(desc, dict):
        desc = desc.get("value")
    if desc:
        out["description"] = str(desc).strip()[:DETAILS_MAX_CHARS]
    return out
