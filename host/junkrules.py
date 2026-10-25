"""Junk rules shared by every importer, and the flag shape they produce.

The rule of the house: a machine may flag text, never delete it. Whatever
survives the preview is what the user pays to have read aloud, so a wrong guess
that removes a line costs a sentence of their book.

`_rules_for` works on one block of plain text. Importers supply what they know
that plain text cannot: a docx has header parts, a pdf has lines that recur at
the same height on page after page.
"""
from __future__ import annotations

import re
import unicodedata

_DIGITS = re.compile(r"^\s*(?:\d{1,4}|[ivxlcdm]{1,7})\s*$", re.I)
_ALLCAPS = re.compile(r"^[^a-z]{2,}$")
_FRONT = re.compile(
    r"(?i)(all rights reserved|copyright\s*©|©\s*\d{4}|isbn|table of contents|"
    r"^\s*contents\s*$|also by|other books by|by the same author|"
    r"this is a work of fiction|no part of this book)"
)
# "Chapter One .......... 12" / "Chapter One\t12"
_TOC_LINE = re.compile(r"(?:\.{2,}|\t| {3,})\s*\d{1,4}\s*$")

FLAG_RULES = {
    "toc": "table of contents entry",
    "repeated-header": "repeats at the same place on many pages",
    "page-number": "looks like a page number",
    "all-caps": "short all-caps line",
    "fragment": "very short orphan line",
    "front-matter": "front/back matter",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip().casefold()


def is_toc_entry(text: str) -> bool:
    t = text.strip()
    return bool(t) and bool(_TOC_LINE.search(t))


# A line that ends like this is a complete thought, however short. Dialogue is
# mostly short complete thoughts ("Yes." "He nodded."), which is why the old
# rule - any block of two words or fewer - flagged three hundred lines of a
# novel and taught the reader to ignore the flag list. A fragment is a line that
# stops mid-thought, and that is visible in how it ends.
_TERMINAL = (".", "!", "?", ":", ";", "-", "\u2014", "\u2013", ")", "]", "\u2026",
             "\u201d", "\u2019", '"', "'", "\u00bb", "\u3002", "\uff01", "\uff1f")


def _looks_cut_off(text: str) -> bool:
    t = (text or "").strip()
    if not t or t.isdigit():
        return False
    # A stray couple of characters on its own line is an artefact of the parse.
    words = t.split()
    if len(words) == 1 and len(t) <= 3 and not t.endswith(_TERMINAL):
        return True
    # A hyphen at the end with nothing after it means a word was broken and the
    # other half went somewhere else.
    return t.endswith("-") and len(t) > 3


def rules_for(text: str, *, in_toc: bool = False, running: set | None = None) -> list[str]:
    """Every junk rule that fires for one block. Advisory only, never a delete."""
    t = text.strip()
    if not t:
        return []
    n = norm(t)
    rules = []
    if in_toc or is_toc_entry(t):
        rules.append("toc")
    if running and n in running:
        rules.append("repeated-header")
    # A bare number line is a page number or a chapter marker; either way a
    # reader should not narrate it, and it is only a flag, never a delete.
    if _DIGITS.match(t) and len(t) <= 5:
        rules.append("page-number")
    if _ALLCAPS.match(t) and len(t) <= 40 and len(t.split()) >= 2:
        rules.append("all-caps")
    if _looks_cut_off(t):
        rules.append("fragment")
    if _FRONT.search(t) and len(t) <= 200:
        rules.append("front-matter")
    return rules
