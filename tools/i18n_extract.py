"""Extract translatable strings from the client into ui/i18n/en.json.

English is the source language and the key is the English sentence itself, so
this tool does two jobs at once: it marks static markup with data-t (and
data-t-ph / data-t-title / data-t-aria for attributes) and it dumps the current
English catalog. Run it after editing ui/index.html; run tools/i18n_check.py to
see what is left that a translator cannot reach.

  python3 tools/i18n_extract.py            # rewrite ui/index.html + en.json
  python3 tools/i18n_extract.py --check    # report, change nothing
"""
import html
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "ui", "index.html")
CATALOG = os.path.join(ROOT, "ui", "i18n", "en.json")
SKIP = {"script", "style", "textarea"}
ATTRS = [("placeholder", "data-t-ph"), ("title", "data-t-title"),
         ("aria-label", "data-t-aria")]
WORDISH = re.compile(r"[A-Za-z]{2,}")
# "__HOST_VERSION__" and friends are filled in by the server: not a sentence,
# and worse, shipping one in the catalog would put the placeholder back into
# the page after the host had already replaced it.
PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


class TextFinder(HTMLParser):
    """Find elements whose whole content is one piece of text, and attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []          # (tag, has_element_child, text, has_data_t)
        self.simple = []         # (tag, text) elements whose content is only text
        self.attrs = []          # (attr_kind, value) attribute strings
        self.marked = set()      # text of elements that already carry data-t
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if self.stack:
            t = self.stack[-1]
            self.stack[-1] = (t[0], True, t[2], t[3])
        if tag in SKIP:
            self.skip += 1
        d = dict(attrs)
        self.stack.append((tag, False, None, "data-t" in d))
        for name, _kind in ATTRS:
            v = (d.get(name) or "").strip()
            if v and WORDISH.search(v):
                self.attrs.append((name, v))

    def handle_endtag(self, tag):
        if not self.stack:
            return
        t, has_child, text, has_dt = self.stack.pop()
        if tag in SKIP:
            self.skip -= 1
        if text and not self.skip and not PLACEHOLDER.search(text):
            v = html.unescape(text.strip())
            if v and WORDISH.search(v):
                if has_dt:
                    self.marked.add(v)
                if not has_child:
                    self.simple.append((t, v))

    def handle_entityref(self, name):
        self.handle_data("&%s;" % name)

    def handle_charref(self, name):
        self.handle_data("&#%s;" % name)

    def handle_data(self, d):
        if self.skip or not self.stack:
            return
        t, has_child, text, has_dt = self.stack[-1]
        self.stack[-1] = (t, has_child, (text or "") + d, has_dt)


# Script and style bodies are code, not copy. The marking passes are regexes
# over the page, and a regex cannot tell the two apart: a <script> whose body
# happens to contain no '<' matches the element rule, and the "fix" is a data-t
# attribute carrying the block's own source (html-escaped). That is the blank
# page of #173 and #175, arriving from the tool that was meant to catch it.
OPAQUE = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>)", re.S | re.I)


DUPLICABLE = ("data-t", "data-t-ph", "data-t-title", "data-t-aria")


def dedupe_marked_attrs(chunk):
    """Keep the first copy of each data-t* attribute on a tag.

    Repeated runs of this tool used to append the same marker again, and the
    page ends up with three identical attributes on one button: invalid HTML
    that browsers read by first-wins, so it works by accident.
    """
    def one_tag(m):
        tag = m.group(0)
        for name in DUPLICABLE:
            pat = re.compile(r'(\s%s="[^"]*")' % name)
            kept = []

            def keep(mm):
                if kept:
                    return ""
                kept.append(1)
                return mm.group(1)

            tag = pat.sub(keep, tag)
        return tag
    return re.sub(r"<[a-zA-Z][^<>]*>", one_tag, chunk)


def map_outside(src, fn):
    """Apply fn to the markup around script/style blocks, never inside them."""
    return "".join(part if i % 2 else fn(part)
                   for i, part in enumerate(OPAQUE.split(src)))


# --- script strings -------------------------------------------------------
# The page half of the client is markup; the other half is script, and the
# script was the gap that made Spanish a half-language. A message written in
# JS is a call, not an element, so the markup pass cannot see it: those strings
# live inside T('...') and this pass reads them straight out of the call.
# The literal arrives at runtime already decoded (T receives the value, not the
# source), so the key has to be decoded the same way or it never matches.
SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)
T_CALL = re.compile(r"(?<![\w.])T\(\s*(['\"])((?:\\.|(?!\1)[^\\\n])*)\1\s*[,)]")
JS_ESC = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)


def js_unescape(s):
    def one(m):
        g = m.group(1)
        if g[0] == "u" and len(g) == 5:
            return chr(int(g[1:], 16))
        if g[0] == "x" and len(g) == 3:
            return chr(int(g[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(g, g)
    return JS_ESC.sub(one, s)


def script_strings(src):
    """Every key passed to T() in a script block, decoded, in page order."""
    keys = []
    for body in SCRIPT.findall(src):
        # A block that is data (JSON-LD, importmap) is not code.
        head = src[src.find(body) - 60:src.find(body)]
        for m in T_CALL.finditer(body):
            v = js_unescape(m.group(2))
            if v and v not in keys:
                keys.append(v)
    return keys


def analyse():
    src = open(PAGE, encoding="utf-8").read()
    f = TextFinder()
    f.feed(src)
    return src, f


def esc(v):
    return html.escape(v, quote=True)


def check():
    src, f = analyse()
    marked = f.marked
    todo = [(t, v) for t, v in f.simple if v not in marked]
    try:
        have = json.load(open(CATALOG, encoding="utf-8"))["messages"]
    except (OSError, ValueError, KeyError):
        have = {}
    unused = sorted(set(have) - marked - {html.unescape(v) for _n, v in f.attrs})
    print("static strings needing data-t: %d" % len(todo))
    for t, v in todo[:40]:
        print("  <%s> %s" % (t, v))
    print("attribute strings: %d" % len(f.attrs))
    js = set(script_strings(src))
    print("T() script strings: %d" % len(js))
    unused = sorted(set(unused) - js)
    print("catalog entries not present in the page: %d" % len(unused))
    for v in unused[:20]:
        print("  - %s" % v)
    return 1 if todo or unused else 0


def write():
    src, f = analyse()
    marked = f.marked
    want = {}
    for t, v in f.simple:
        want[html.unescape(v)] = html.unescape(v)

    # 1) mark simple elements. Doing it textually keeps the page byte-identical
    #    everywhere else, which is what makes this safe to run between commits.
    def mark(m):
        tag, attrs, text = m.group(1), m.group(2), m.group(3)
        v = html.unescape(text.strip())
        if not v or not WORDISH.search(v) or (' data-t=' in attrs) \
                or v in marked or PLACEHOLDER.search(v):
            return m.group(0)
        # the key is written down, not inferred: textContent would decode
        # &amp; and the key would not match what the page looked up.
        return '<%s%s data-t="%s">%s</%s>' % (tag, attrs, esc(v), text, tag)

    marked_src = map_outside(
        src, lambda chunk: re.sub(r"<([a-zA-Z0-9]+)([^<>]*[^/]?)>([^<]+)</\1>",
                                  mark, chunk))

    # 2) mark attributes by adding data-t-ph / data-t-title / data-t-aria next to them
    for name, kind in ATTRS:
        def mark_attr(m, name=name, kind=kind):
            if kind in m.group(0):
                return m.group(0)
            return '%s="%s" %s="%s"' % (name, m.group(1), kind,
                                        esc(html.unescape(m.group(1))))
        marked_src = map_outside(
            marked_src,
            lambda chunk, name=name, kind=kind: re.sub(
                r'\b%s="([^"]*[A-Za-z]{2,}[^"]*)"(\s+%s="[^"]*")?' % (name, kind),
                lambda m: m.group(0) if m.group(2) else mark_attr(m), chunk))

    marked_src = map_outside(marked_src, dedupe_marked_attrs)

    if marked_src != src:
        open(PAGE, "w", encoding="utf-8").write(marked_src)

    for name, v in f.attrs:
        want[html.unescape(v)] = html.unescape(v)
    for v in script_strings(src):
        want[v] = v

    os.makedirs(os.path.dirname(CATALOG), exist_ok=True)
    old = {}
    if os.path.exists(CATALOG):
        try:
            old = json.load(open(CATALOG, encoding="utf-8"))
        except ValueError:
            old = {}
    messages = old.get("messages") or {}
    for k in want:
        messages.setdefault(k, k)
    doc = {"name": old.get("name") or "English",
           "dir": old.get("dir") or "ltr",
           "messages": dict(sorted(messages.items()))}
    with open(CATALOG, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    print("saw %d markup strings, %d attributes, %d script strings; "
          "catalog now %d entries"
          % (len(f.simple), len(f.attrs), len(script_strings(src)),
             len(messages)))
    return 0


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv else write())
