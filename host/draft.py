"""Draft -> Book, and draft -> the preview a user decides on.

Every file importer (pdf, docx) produces the same draft: title/author/identity/
chapters of blocks/flags/words. This module is the only place that knows how a
draft becomes a Book or a preview payload, so the readers stay thin and the
preview window never learns a file format.

The rule the importers follow carries through here: nothing is deleted. A flag
is an offer ("this line looks like a running head, drop it?"), and the preview
shows every block with the flags attached so the user can accept or ignore them
before they pay characters for the result.
"""
from __future__ import annotations

from host.contracts import Book, Chapter


def blocks_text(blocks) -> str:
    """Chapter text as the host stores it: blocks joined by a blank line."""
    return "\n\n".join(b["text"] for b in blocks if b.get("text"))


def draft_to_book(draft: dict, source: str) -> Book:
    chs = []
    for ch in draft.get("chapters") or []:
        text = blocks_text(ch.get("blocks") or [])
        if text.strip():
            chs.append(Chapter(title=(ch.get("title") or "").strip(), text=text))
    meta = {"source": source, "identity": draft.get("identity") or "",
            "author": draft.get("author") or ""}
    book = Book(title=(draft.get("title") or "").strip() or "Untitled",
                chapters=chs, meta=meta)
    return book


def draft_summary(draft: dict, source: str, price_per_mchar: float = 0.0) -> dict:
    """What the preview needs, with the cost stated in characters.

    `chars` is the count that will be charged if the user imports as shown, so
    flagged-but-kept text is included: dropping the flags changes the number,
    and the preview recomputes it from whatever the user leaves in place.
    """
    chapters = []
    total_chars = 0
    for ci, ch in enumerate(draft.get("chapters") or []):
        blocks = ch.get("blocks") or []
        text = blocks_text(blocks)
        n = len(text)
        total_chars += n
        chapters.append({"index": ci,
                         "title": ch.get("title") or "",
                         "words": ch.get("words", 0),
                         "chars": n,
                         "blocks": [{"id": b.get("id", ""), "text": b.get("text", "")}
                                    for b in blocks]})
    out = {"source": source,
           "title": draft.get("title") or "",
           "author": draft.get("author") or "",
           "identity": draft.get("identity") or "",
           "chapters": chapters,
           "flags": draft.get("flags") or [],
           "words": draft.get("words", 0),
           "chars": total_chars}
    for k in ("pages", "text_pages", "chapter_source", "two_column_pages"):
        if k in draft:
            out[k] = draft[k]
    if price_per_mchar:
        out["price"] = round(total_chars / 1_000_000.0 * price_per_mchar, 2)
    return out
