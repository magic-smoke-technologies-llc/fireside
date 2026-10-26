"""Language negotiation and catalog injection for the browser client.

One catalog per language lives in ui/i18n/<code>.json:

    {"name": "Español", "dir": "ltr", "messages": {"Sign in": "Entrar"}}

Keys are the English sentence (gettext style) so a translator can work from a
plain list, and a missing key falls back to English instead of an identifier.
The host picks a language per request - ?lang= first, then the fs_lang cookie,
then Accept-Language - and hands the catalog to the page inline, so the client
never renders in one language and then swaps to another.
"""

import glob
import html
import json
import os
import re

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
LOCALE_DIR = os.path.join(UI_DIR, "i18n")
DEFAULT = "en"
COOKIE = "fs_lang"
RTL = {"ar", "fa", "he", "ur"}

_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_cache = {}


def _norm(code):
    """'es_MX', 'ES-mx', 'es' -> 'es-mx' / 'es'. Unusable input -> ''."""
    if not code:
        return ""
    code = str(code).strip().replace("_", "-")
    if not _LANG_RE.match(code):
        return ""
    return code.lower()


def available():
    """Every language we ship a catalog for, English included, English first."""
    out = []
    for path in sorted(glob.glob(os.path.join(LOCALE_DIR, "*.json"))):
        code = os.path.basename(path)[:-5]
        cat = catalog(code)
        out.append({"code": code, "name": cat.get("name") or code,
                    "dir": cat.get("dir") or ("rtl" if code.split("-")[0] in RTL else "ltr")})
    out.sort(key=lambda l: (l["code"] != DEFAULT, l["code"]))
    return out


def catalog(code):
    code = _norm(code) or DEFAULT
    if code not in _cache:
        path = os.path.join(LOCALE_DIR, code + ".json")
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, OSError):
                data = {}
        data.setdefault("name", code)
        data.setdefault("messages", {})
        data["dir"] = data.get("dir") or ("rtl" if code.split("-")[0] in RTL else "ltr")
        _cache[code] = data
    return _cache[code]


def _from_accept_language(header, codes):
    """Best supported match for an Accept-Language header, English as last word."""
    if not header:
        return ""
    scored = []
    for i, part in enumerate(header.split(",")):
        bits = part.strip().split(";")
        tag = _norm(bits[0])
        if not tag:
            continue
        q = 1.0
        for b in bits[1:]:
            b = b.strip()
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        scored.append((-q, i, tag))
    for _q, _i, tag in sorted(scored):
        if tag in codes:
            return tag
        base = tag.split("-")[0]
        for c in codes:
            if c.split("-")[0] == base:
                return c
    return ""


def negotiate(query_lang=None, cookie_lang=None, accept_language=None, codes=None):
    """Which catalog this request gets. English when nothing matches."""
    codes = codes if codes is not None else [l["code"] for l in available()]
    if DEFAULT not in codes:
        codes = [DEFAULT] + list(codes)
    for cand in (query_lang, cookie_lang):
        cand = _norm(cand)
        if cand in codes:
            return cand
        if cand:
            base = cand.split("-")[0]
            for c in codes:
                if c.split("-")[0] == base:
                    return c
    return _from_accept_language(accept_language, codes) or DEFAULT


_OPAQUE = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>)", re.S | re.I)
_TEXT_EL = re.compile(r'(<[a-zA-Z][^>]*\bdata-t="([^"]*)"[^>]*>)([^<>]*)(</[a-zA-Z][^>]*>)')


def _outside_scripts(page, fn):
    return "".join(part if i % 2 else fn(part)
                   for i, part in enumerate(_OPAQUE.split(page)))


def _render_text(chunk, messages):
    """Swap the English shown in a marked element for its translation.

    The catalog arriving with the page is not the page being in that language:
    the app script has to run before data-t elements change, so a Spanish
    reader sees English navigation until JS executes. Doing the swap here means
    the first paint is already Spanish, and the page still reads in that
    language if the script never runs.
    """
    def one(m):
        open_tag, key, inner, close_tag = m.group(1), m.group(2), m.group(3), m.group(4)
        text = messages.get(html.unescape(key))
        if text is None or not inner.strip():
            return m.group(0)
        return open_tag + html.escape(text, quote=False) + close_tag
    return _TEXT_EL.sub(one, chunk)


def _render_attrs(chunk, messages):
    """Translate the attribute a marker names, and the raw one beside it."""
    for marker, attr in (("data-t-ph", "placeholder"), ("data-t-title", "title"),
                         ("data-t-aria", "aria-label")):
        pat = re.compile(r'%s="([^"]*)"' % marker)

        def swap(m, attr=attr, marker=marker):
            text = messages.get(html.unescape(m.group(1)))
            if text is None:
                return m.group(0)
            return '%s="%s" %s="%s"' % (attr, html.escape(text),
                                        marker, m.group(1))

        chunk = pat.sub(swap, chunk)
    return chunk


def render(page, cat):
    """The page with the catalog already applied to its markup."""
    messages = cat.get("messages") or {}
    if cat.get("locale") == DEFAULT or not messages:
        return page

    def per_chunk(chunk):
        return _render_attrs(_render_text(chunk, messages), messages)

    return _outside_scripts(page, per_chunk)


def inject(page, code=None, cookies=None, query=None, accept_language=None):
    """Return the page with lang/dir and the catalog spliced in.

    Also the only place /i18n.js is referenced, so a page that never goes
    through here plainly does not support languages.
    """
    code = code or negotiate(query, (cookies or {}).get(COOKIE), accept_language)
    cat = catalog(code)
    payload = {
        "locale": code,
        "dir": cat["dir"],
        "messages": cat["messages"],
        "locales": available(),
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    script = ('<script src="/i18n.js"></script>\n'
              '<script>window.__FS_I18N__=' + blob + ';</script>\n')
    page = render(page, {"locale": code, "messages": cat["messages"]})
    page = re.sub(r'<html lang="[^"]*"', '<html lang="%s" dir="%s"' % (code, cat["dir"]),
                  page, count=1)
    if "</head>" in page:
        return page.replace("</head>", script + "</head>", 1)
    return script + page
