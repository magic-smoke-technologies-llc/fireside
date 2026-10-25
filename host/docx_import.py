"""DOCX -> chapters + blocks, with junk flagged and nothing deleted.

A .docx is a zip with word/document.xml. We read the body paragraphs in order,
find chapter starts from the heading styles, and keep every paragraph as its own
addressed block. Nothing is removed: suspected junk is flagged and left in the
text, because the user is about to pay characters for whatever survives.

Two things that are true of OOXML and easy to get wrong:

1. Running headers/footers are NOT in document.xml. They are separate parts
   (word/header1.xml, word/footer1.xml) pulled in from sectPr. So the classic
   "line repeated on every page" rule from a PDF import has no body text to hit
   here. We read those parts anyway: a header's text sometimes also appears in
   the body (a title page copied in), and that occurrence is worth flagging.

2. Page numbers are field codes (PAGE), and the body's table of contents is a
   TOC field. A TOC field's *result* is ordinary paragraphs we see in the body,
   so the reliable mark is the field instruction plus the shape of the lines
   (text ending in a tab or dot leader, then a number).

Plain text only. No attempt to re-serialize DOCX, no rich text, no tables as
tables (cell text is pulled in reading order).
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from host.junkrules import FLAG_RULES, is_toc_entry, norm as _norm, rules_for

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}

_HEADING_STYLE = re.compile(r"(?i)^heading\s*([1-9])$")


def _text_of(p) -> str:
    """Paragraph text, in order, with tabs and hard breaks kept as characters."""
    out = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{{{W}}}t":
            out.append(node.text or "")
        elif tag == f"{{{W}}}tab":
            out.append("\t")
        elif tag in (f"{{{W}}}br", f"{{{W}}}cr"):
            out.append("\n")
        elif tag == f"{{{W}}}noBreakHyphen":
            out.append("-")
    return "".join(out)


def _style(p) -> str:
    el = p.find("./w:pPr/w:pStyle", NS)
    if el is None:
        el = p.find(".//w:pStyle", NS)
    if el is None:
        return ""
    return el.get(f"{{{W}}}val") or ""


def _outline(p) -> int | None:
    el = p.find("./w:pPr/w:outlineLvl", NS)
    if el is None:
        return None
    try:
        return int(el.get(f"{{{W}}}val"))
    except (TypeError, ValueError):
        return None


def _heading_level(p) -> int:
    """1..9 for a heading paragraph, 0 for not a heading."""
    m = _HEADING_STYLE.match(_style(p))
    if m:
        return int(m.group(1))
    lvl = _outline(p)
    if lvl is not None and lvl < 9:
        return lvl + 1
    return 0


def _fields(p) -> list[str]:
    """Field instructions in this paragraph (TOC, PAGE, HYPERLINK, ...)."""
    return [(n.text or "").strip() for n in p.iter(f"{{{W}}}instrText")]


def _part_text(data: bytes, name: str) -> list[str]:
    try:
        raw = data and zipfile.ZipFile(io.BytesIO(data)).read(name)
    except (KeyError, zipfile.BadZipFile):
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    return [t for t in (_text_of(p) for p in root.iter(f"{{{W}}}p")) if t.strip()]


def _core_meta(data: bytes) -> dict:
    try:
        raw = zipfile.ZipFile(io.BytesIO(data)).read("docProps/core.xml")
        root = ET.fromstring(raw)
    except (KeyError, zipfile.BadZipFile, ET.ParseError):
        return {}
    out = {}
    for key, tag in (("title", "title"), ("author", "creator")):
        for el in root.iter():
            if el.tag.endswith("}" + tag) and (el.text or "").strip():
                out[key] = el.text.strip()
                break
    return out


def extract(data: bytes) -> dict:
    """Parse a DOCX into a draft: chapters of addressed blocks, plus flags.

    Returns {title, author, identity, chapters:[{title, blocks:[{id,text}], words}],
             flags:[{id, chapter, block, rule, text}], words}
    Nothing is deleted. `flags` is advisory and points at block ids.
    """
    z = zipfile.ZipFile(io.BytesIO(data))
    body = ET.fromstring(z.read("word/document.xml"))

    header_text = set()
    for name in z.namelist():
        if re.match(r"^word/(header|footer)\d*\.xml$", name):
            for t in _part_text(data, name):
                n = _norm(t)
                if n and len(n) > 1:
                    header_text.add(n)

    toc_field = False
    chapters: list[dict] = []
    pending: list[tuple[dict, str]] = []   # (block dict, in_toc) — flagged later
    cur = {"title": "", "blocks": []}

    def add_block(text: str) -> dict | None:
        text = text.replace("\u00a0", " ").rstrip()
        if not text.strip():
            return None
        b = {"text": text}
        cur["blocks"].append(b)
        return b

    for child in body.iter(f"{{{W}}}p"):
        instr = " ".join(_fields(child))
        if instr.upper().startswith("TOC"):
            toc_field = True
        text = _text_of(child)
        level = _heading_level(child)
        if level and 1 <= level <= 2 and text.strip():
            if cur["blocks"] or cur["title"]:
                chapters.append(cur)
            cur = {"title": text.strip()[:200], "blocks": []}
            continue
        if not text.strip():
            continue
        b = add_block(text)
        if b is not None:
            pending.append((b, bool(toc_field and is_toc_entry(text))))
    chapters.append(cur)

    # A document that starts with a heading leaves an empty leading chapter.
    if chapters and not chapters[0]["blocks"] and not chapters[0]["title"]:
        chapters.pop(0)
    if not chapters:
        chapters = [{"title": "", "blocks": []}]

    # Ids are assigned only once the chapter list is final, so a popped leading
    # chapter cannot shift a flag onto the wrong text.
    where: dict[int, tuple[int, str]] = {}
    for ci, ch in enumerate(chapters):
        for bi, b in enumerate(ch["blocks"]):
            b["id"] = f"c{ci}b{bi}"
            where[id(b)] = (ci, b["id"])

    flags: list[dict] = []
    for b, in_toc in pending:
        if id(b) not in where:
            continue
        ci, bid = where[id(b)]
        for rule in rules_for(b["text"], in_toc=in_toc, running=header_text):
            flags.append({"id": f"f{len(flags)}", "chapter": ci, "block": bid,
                          "rule": rule, "text": b["text"].strip()[:120]})

    words = sum(len(b["text"].split()) for ch in chapters for b in ch["blocks"])
    meta = _core_meta(data)
    plain = "\n".join(b["text"] for ch in chapters for b in ch["blocks"])
    return {
        "title": meta.get("title") or (chapters[0]["title"] if chapters else ""),
        "author": meta.get("author", ""),
        # Identity is the extracted text, not the file bytes: the same book
        # re-exported (new metadata, new zip timestamps) is still the same book.
        "identity": "docx:" + hashlib.sha256(plain.encode("utf-8")).hexdigest()[:32],
        "chapters": [{"title": ch["title"], "blocks": ch["blocks"],
                      "words": sum(len(b["text"].split()) for b in ch["blocks"])}
                     for ch in chapters],
        "flags": flags,
        "words": words,
    }
