"""The client is one long inline script, and a script that does not parse is a
page that does not exist.

#182 shipped a function without its closing brace. Every test passed: the CSS
tests parse stylesheets, the i18n tests parse markup, and nothing parsed the
script. The site then served a page whose only visible content was the skip
link, for sixteen hours, while 974 green tests said it was fine. That is the
same shape as a green harness proving nothing about the seam.

This is a brace balance check, not a full parser: it knows about strings,
template literals, comments and enough of regex syntax to not count the
punctuation inside one. It cannot tell you the script is valid. It can tell you
the file ends with something still open, which is exactly the failure that got
through.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "ui" / "index.html"
SCRIPT = re.compile(r"(?is)<script\b[^>]*>(.*?)</script>")

PAIRS = {")": "(", "]": "[", "}": "{"}
OPEN = set("([{")
REGEX_OK = set("(,=:[!&|?{};+-*%~^<>\n")
CLOSERS = {"(": ")", "[": "]", "{": "}"}


def script_bodies(html):
    return [b for b in SCRIPT.findall(html) if len(b) > 200]


def scan(src):
    """Return the open brackets at the end of the source, in source order.

    Each entry is (char, line). Empty means balanced.
    """
    stack = []
    i, n, line = 0, len(src), 1
    prev = "\n"                      # last significant character, for regex
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c in "'\"":
            quote, j = c, i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                j += 1
            line += src.count("\n", i, j)
            i = j
            prev = quote
            continue
        if c == "`":
            # template: skip it, but count the ${ } expressions it embeds
            depth = 0
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if depth == 0 and src[j] == "`":
                    j += 1
                    break
                if src.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if depth and src[j] == "}":
                    depth -= 1
                    j += 1
                    continue
                j += 1
            line += src.count("\n", i, j)
            i = j
            prev = "`"
            continue
        if c == "/" and prev in REGEX_OK and i + 1 < n and src[i + 1] not in "/*":
            j, in_class = i + 1, False
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    j += 1
                    break
                elif src[j] == "\n":
                    break
                j += 1
            i = j
            prev = "/"
            continue
        if c in OPEN:
            stack.append((c, line))
        elif c in PAIRS:
            if not stack:
                return [("unopened " + c, line)]
            top, tline = stack.pop()
            if top != PAIRS[c]:
                return [("mismatched " + top, tline), (c, line)]
        prev = c
        i += 1
    return stack


def test_client_script_brackets_balance():
    bodies = script_bodies(PAGE.read_text(encoding="utf-8"))
    assert bodies, "no inline client script found: did the page change shape?"
    problems = []
    for body in bodies:
        for ch, line in scan(body):
            problems.append("%s opened at client script line %d" % (ch, line))
    assert problems == [], "the client script does not finish what it starts: " + \
        "; ".join(problems)


def test_the_scanner_catches_the_bug_182_shipped():
    # the exact shape of the outage: a function whose body closes a string and
    # then just keeps going
    broken = "function sayState() {\n  box.textContent = 'x';\n// next\nfunction b() {}\n"
    assert scan(broken) == [("{", 1)]
    fixed = "function sayState() {\n  box.textContent = 'x';\n}\n"
    assert scan(fixed) == []
    # and it must not be fooled by the punctuation inside strings or regexes
    assert scan("const s = '{(['; const r = /[{}]/; const t = `${a}{`;\n") == []