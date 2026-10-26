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
    page = re.sub(r'<html lang="[^"]*"', '<html lang="%s" dir="%s"' % (code, cat["dir"]),
                  page, count=1)
    if "</head>" in page:
        return page.replace("</head>", script + "</head>", 1)
    return script + page
