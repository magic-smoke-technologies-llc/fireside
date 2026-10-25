"""PDF -> chapters + blocks, with junk flagged and nothing deleted.

Same draft shape as the DOCX importer (title/author/identity/chapters/flags/
words), because the preview window does not care which file it came from.

A PDF has no styles, no paragraphs, no chapters. It has glyph runs at
coordinates. So every structural fact here is inferred, and the inferences are
written down rather than hidden:

- Chapters: the file's own bookmark outline when it has one (that is the author
  speaking, trust it first), otherwise lines that are both much larger than the
  body text and short, counted as chapter starts.
- Running heads: a line that recurs on many pages at the same height from the
  top or the bottom edge. This is the strongest signal a PDF gives and the one
  thing docx cannot offer. It is a flag, never a delete.
- Reading order: containers are sorted by column, then top to bottom. Two-column
  detection is a heuristic on x bands and page width. It is the part most likely
  to be wrong, so `layout` says whether a page looked two-column.
- Hyphenation: a line ending in "-" continues the next one; if the next word
  starts lowercase the hyphen is dropped (word-joined), otherwise kept (a real
  compound).

Extraction is pypdf for pages and outlines, pdfminer.six for layout. PyMuPDF is
AGPL and is not acceptable inside a paid host.

No OCR. A page with no text layer produces nothing, and an all-empty result is
reported as `scanned: true` so the caller can refuse it with a reason instead of
importing an empty book.
"""
from __future__ import annotations

import hashlib
import io
import re
import statistics

from host.junkrules import is_toc_entry, norm

_MIN_HEADER_PAGES = 3


def _lines_of_page(page) -> list[dict]:
    """Every text line on the page with the geometry we need, top-down."""
    from pdfminer.layout import LTChar, LTTextContainer, LTTextLine

    out = []
    height = float(page.height)
    for el in page:
        if not isinstance(el, LTTextContainer):
            continue
        for line in el:
            if not isinstance(line, LTTextLine):
                continue
            text = line.get_text() or ""
            text = text.replace("\u00a0", " ")
            text = re.sub(r"[ \t]+", " ", text).strip()
            if not text:
                continue
            sizes = [c.size for c in line if isinstance(c, LTChar)]
            out.append({
                "text": text,
                "x0": float(line.x0),
                "x1": float(line.x1),
                "y0": float(line.y0),
                "y1": float(line.y1),
                "size": round(max(sizes), 1) if sizes else 0.0,
                "from_top": height - float(line.y1),
                "from_bottom": float(line.y0),
            })
    return out


def _page_width(page) -> float:
    return float(page.width)


def _is_two_column(lines: list[dict], width: float) -> bool:
    """Two columns look like: a clean vertical gap with text on both sides."""
    if len(lines) < 12 or width <= 0:
        return False
    mid = width / 2
    left = [l for l in lines if l["x1"] <= mid + 6]
    right = [l for l in lines if l["x0"] >= mid - 6]
    if len(left) < 4 or len(right) < 4:
        return False
    # both sides must span a real vertical range, not just a header block
    def span(ls):
        return max(l["y1"] for l in ls) - min(l["y0"] for l in ls)
    return span(left) > 200 and span(right) > 200


def _ordered(lines: list[dict], width: float) -> list[dict]:
    if _is_two_column(lines, width):
        mid = width / 2
        return (sorted([l for l in lines if l["x0"] < mid], key=lambda l: -l["y1"]) +
                sorted([l for l in lines if l["x0"] >= mid], key=lambda l: -l["y1"]))
    return sorted(lines, key=lambda l: (-l["y1"], l["x0"]))


def _running_lines(pages_lines: list[list[dict]]) -> set[str]:
    """Lines that repeat on many pages near the top or the bottom edge."""
    counts: dict[str, int] = {}
    spots: dict[str, list[tuple[int, float]]] = {}
    for i, lines in enumerate(pages_lines):
        for l in lines:
            n = norm(l["text"])
            if len(n) < 2 or len(n) > 120:
                continue
            counts[n] = counts.get(n, 0) + 1
            spots.setdefault(n, []).append((i, round(l["from_top"], 0) if l["from_top"] < 80
                                            else round(l["from_bottom"], 0)))
    out = set()
    for n, c in counts.items():
        if c < _MIN_HEADER_PAGES or c < max(_MIN_HEADER_PAGES, len(pages_lines) * 0.25):
            continue
        # it must sit at the same height each time, or it is a repeated sentence
        ys = [y for _i, y in spots[n]]
        if statistics.pstdev(ys) <= 3.0:
            out.add(n)
    return out


def _body_size(pages_lines: list[list[dict]]) -> float:
    sizes = [l["size"] for lines in pages_lines for l in lines if len(l["text"]) > 25]
    return statistics.median(sizes) if sizes else 0.0


def _heading_lines(pages_lines: list[list[dict]], body: float) -> list[tuple[int, str]]:
    """(page_index, text) for lines that look like chapter starts, in order."""
    found = []
    for i, lines in enumerate(pages_lines):
        for l in _ordered(lines, 10 ** 6):
            if body and l["size"] >= body * 1.35 and len(l["text"]) <= 80 and l["text"].strip():
                found.append((i, l["text"].strip()))
    return found


def _tight_gap(gaps: list[float]) -> float:
    """The leading inside a paragraph, which is the *smaller* of the two gaps.

    A page gives two populations: lines that continue a paragraph (tight) and
    lines that start one (loose, a blank line). Taking the median picks
    whichever population happens to be more common, and on a chapter split into
    many short paragraphs that is the *loose* one, so every paragraph merges
    into one six-thousand-word block. So: find the tightest cluster and call
    that the leading, whatever proportion of the page it is. Measured on a real
    449-page book, 5 of 27 chapters were collapsing into single blocks under the
    median; this rule gives every chapter a block count in proportion to its
    words.
    """
    if not gaps:
        return 0.0
    s = sorted(gaps)
    floor = s[0]
    tight = [g for g in s if g <= floor + 4.0]
    return statistics.median(tight)


def _outline_starts(reader) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    def walk(items, depth=0):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
                continue
            title = (getattr(it, "title", "") or "").strip()
            try:
                page_no = reader.get_destination_page_number(it)
            except Exception:
                continue
            if title and page_no is not None and page_no >= 0:
                out.append((int(page_no), title))

    try:
        walk(reader.outline)
    except Exception:
        return []
    return out


def _join_lines(texts: list[str]) -> str:
    """Rebuild a paragraph from wrapped lines, undoing soft hyphenation."""
    out = ""
    for raw in texts:
        t = raw.strip()
        if not t:
            continue
        if not out:
            out = t
            continue
        if out.endswith("-") and len(out) > 1:
            prev = out[:-1]
            if t[:1].islower():
                out = prev + t
            else:
                out = prev + "-" + t
        else:
            out = out + " " + t
    return out


_BORING_TITLES = {"", "untitled", "document", "microsoft word", "title",
                  "n/a", "none", "no title", "pdf"}


def _doc_title(reader, pages_lines) -> str:
    """What this book is called, in the order a reader would answer it.

    The file's own /Title is the author's (or the exporter's) answer and it is
    usually right, so it wins. A bookmark outline is a *structure*, not a name:
    the first entry is "Preface" or "Cover" far more often than it is the title,
    which is how a book ends up named after its own front matter. When there is
    no usable metadata, fall back to the largest text on page one, and only then
    to the first bookmark.
    """
    meta = ""
    try:
        info = reader.metadata or {}
        meta = (info.get("/Title") or "").strip()
    except Exception:
        meta = ""
    if norm(meta) and norm(meta) not in _BORING_TITLES and len(meta) <= 200:
        return re.sub(r"\s+", " ", meta)

    from_heading = _heading_title(pages_lines)
    if from_heading:
        return from_heading
    return ""


def _doc_author(reader) -> str:
    try:
        info = reader.metadata or {}
        a = (info.get("/Author") or "").strip()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", a)[:200]


def _heading_title(pages_lines) -> str:
    """The biggest line on the first page that has text. One guess, bounded.

    Timid on purpose: a page with one font size gives no signal, and a wrong
    title is worse than an empty one because the user can fix empty at import
    and will not notice wrong.
    """
    for lines in pages_lines:
        sized = [l for l in lines if l.get("size") and len(l["text"]) >= 3]
        if not sized:
            continue
        body = statistics.median(l["size"] for l in sized)
        big = [l for l in sized if l["size"] >= body + 2.0]
        if not big:
            return ""
        best = max(big, key=lambda l: (l["size"], -len(l["text"])))
        text = best["text"].strip()
        if norm(text) in _BORING_TITLES or len(text.split()) > 20:
            return ""
        return re.sub(r"\s+", " ", text)
    return ""


MISSING_DEPS = ("the PDF reader needs two packages that are not installed here: "
                "run \"pip install -r requirements.txt\" (pypdf, pdfminer.six). "
                "EPUB, DOCX and web imports do not need them.")

try:
    from host.contracts import Refused as _Refused
except Exception:                       # contracts is always importable in-tree
    _Refused = None


def extract(data: bytes) -> dict:
    """Parse a PDF into the same draft shape the preview window expects."""
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams
        import pypdf
    except ImportError:
        # A missing optional package is a refusal with a reason, not a 500.
        # This is the only place the two are imported, so the rest of the app
        # still boots and still reads every other format.
        if _Refused:
            raise _Refused("the pdf reader is not installed here", MISSING_DEPS)
        raise ImportError(MISSING_DEPS)

    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = list(extract_pages(io.BytesIO(data),
                               laparams=LAParams(line_margin=0.4, char_margin=1.5,
                                                 detect_vertical=True)))

    pages_lines = [_lines_of_page(p) for p in pages]
    widths = [_page_width(p) for p in pages]
    body = _body_size(pages_lines)
    running = _running_lines(pages_lines)
    text_pages = sum(1 for lines in pages_lines if lines)

    outline = _outline_starts(reader)
    starts = outline if len(outline) >= 2 else _heading_lines(pages_lines, body)
    if len(starts) < 2:
        starts = [(0, "")]
    # dedupe and sort by page, keeping the first title claimed for a page
    seen_pages: dict[int, str] = {}
    for page_no, title in starts:
        seen_pages.setdefault(page_no, title)
    ordered_starts = sorted(seen_pages.items())

    chapters: list[dict] = []
    for page_no, title in ordered_starts:
        chapters.append({"title": title[:200], "lines": [], "start": page_no})

    def chapter_for(page_index: int) -> dict:
        chosen = chapters[0]
        for ch in chapters:
            if ch["start"] <= page_index:
                chosen = ch
        return chosen

    layout_notes = []
    for i, lines in enumerate(pages_lines):
        if not lines:
            continue
        ch = chapter_for(i)
        width = widths[i]
        if _is_two_column(lines, width):
            layout_notes.append(i)
        for l in _ordered(lines, width):
            ch["lines"].append(l)

    # Build paragraphs from lines. A paragraph continues when the vertical gap
    # to the next line is normal for this page and the left edge lines up; a
    # bigger gap or a shifted margin starts a new block.
    blocks_out: list[dict] = []
    pending: list[tuple[dict, str]] = []
    for ci, ch in enumerate(chapters):
        lines = ch["lines"]
        if ch["title"] and lines and norm(lines[0]["text"]) == norm(ch["title"]):
            lines = lines[1:]
        gaps = [lines[i]["y0"] - lines[i + 1]["y1"] for i in range(len(lines) - 1)
                if lines[i]["y0"] > lines[i + 1]["y1"]]
        normal_gap = _tight_gap(gaps)
        paras: list[list[dict]] = []
        for l in lines:
            if paras:
                prev = paras[-1][-1]
                gap = prev["y0"] - l["y1"]
                same_para = (gap <= normal_gap + 6.0
                             and abs(prev["x0"] - l["x0"]) < 24)
                if same_para:
                    paras[-1].append(l)
                    continue
            paras.append([l])
        ch["blocks"] = []
        for p in paras:
            text = _join_lines([l["text"] for l in p]).strip()
            if not text:
                continue
            b = {"text": text}
            ch["blocks"].append(b)
            pending.append((b, bool(is_toc_entry(text))))

    where: dict[int, tuple[int, str]] = {}
    for ci, ch in enumerate(chapters):
        ch.setdefault("blocks", [])
        for bi, b in enumerate(ch["blocks"]):
            b["id"] = f"c{ci}b{bi}"
            where[id(b)] = (ci, b["id"])

    flags: list[dict] = []
    for b, in_toc in pending:
        ci, bid = where[id(b)]
        from host.junkrules import rules_for
        for rule in rules_for(b["text"], in_toc=in_toc, running=running):
            flags.append({"id": f"f{len(flags)}", "chapter": ci, "block": bid,
                          "rule": rule, "text": b["text"].strip()[:120]})

    plain = "\n".join(b["text"] for ch in chapters for b in ch["blocks"])
    words = sum(len(b["text"].split()) for ch in chapters for b in ch["blocks"])
    return {
        "title": _doc_title(reader, pages_lines),
        "author": _doc_author(reader),
        # Same rule as docx: identity is the extracted text, so a re-export is
        # still the same book. pdf bytes carry producer/timestamp noise.
        "identity": "pdf:" + hashlib.sha256(plain.encode("utf-8")).hexdigest()[:32],
        "chapters": [{"title": ch["title"], "blocks": ch.get("blocks", []),
                      "words": sum(len(b["text"].split()) for b in ch.get("blocks", []))}
                     for ch in chapters],
        "flags": flags,
        "words": words,
        "pages": len(pages),
        "text_pages": text_pages,
        "scanned": text_pages == 0,
        "chapter_source": "outline" if outline and len(outline) >= 2 else "font-size",
        "two_column_pages": layout_notes,
    }
