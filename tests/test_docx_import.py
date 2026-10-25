"""DOCX import: chapters from heading styles, junk flagged and kept.

Fixture is hand-built OOXML so the tests describe the document, not a library.
"""
import io
import zipfile

from host.docx_import import extract

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def para(text, style=None, page_break=False):
    ppr = ""
    if style:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
    br = '<w:r><w:br w:type="page"/></w:r>' if page_break else ""
    runs = ""
    if text:
        runs = f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>"
    return f"<w:p>{ppr}{runs}{br}</w:p>"


def docx(body_paras, headers=(), title="", author=""):
    doc = ('<?xml version="1.0"?><w:document xmlns:w="%s"><w:body>%s</w:body></w:document>'
           % (W, "".join(body_paras)))
    parts = {
        "[Content_Types].xml": ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                '<Default Extension="xml" ContentType="application/xml"/></Types>'),
        "word/document.xml": doc,
    }
    for i, h in enumerate(headers, start=1):
        parts[f"word/header{i}.xml"] = (
            '<?xml version="1.0"?><w:hdr xmlns:w="%s">%s</w:hdr>' % (W, para(h)))
    core = ('<?xml version="1.0"?><cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>%s</dc:title><dc:creator>%s</dc:creator></cp:coreProperties>'
            % (title, author))
    parts["docProps/core.xml"] = core
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


BODY = [
    para("Table of Contents", style="TOCHeading"),
    para("Chapter One ................................ 1"),
    para("Chapter Two ................................ 14"),
    para("He was already late.", style="Heading1"),
    para("The street smelled like wet concrete and burnt coffee."),
    para("(c) 2019 Some Author. All rights reserved."),
    para("3"),
    para("SHE SAID NOTHING"),
    para("Okay."),
    para("He was already late."),
    para("The second chapter had a different problem."),
    para("Also by This Author", style="Heading1"),
]


def test_chapters_come_from_heading_styles():
    d = extract(docx(BODY, title="Late", author="A. Writer"))
    assert d["title"] == "Late" and d["author"] == "A. Writer"
    titles = [c["title"] for c in d["chapters"]]
    # front matter before the first heading is its own untitled chapter, so the
    # TOC and copyright page are visible and removable instead of guessed away
    assert titles == ["", "He was already late.", "Also by This Author"]


def test_nothing_is_deleted_only_flagged():
    d = extract(docx(BODY))
    text = "\n".join(b["text"] for c in d["chapters"] for b in c["blocks"])
    for junk in ("(c) 2019 Some Author. All rights reserved.", "SHE SAID NOTHING",
                 "Chapter One ................................ 1"):
        assert junk in text
    rules = {f["text"]: f["rule"] for f in d["flags"]}
    assert rules["Chapter One ................................ 1"] == "toc"
    assert rules["3"] in ("page-number", "fragment")
    assert rules["SHE SAID NOTHING"] == "all-caps"
    # A short complete thought is prose, not an artefact. Dialogue is mostly
    # short complete thoughts, and flagging them buried the real junk.
    assert "Okay." not in rules
    assert "(c) 2019 Some Author. All rights reserved." in [
        f["text"] for f in d["flags"] if f["rule"] == "front-matter"]


def test_header_text_also_in_body_is_flagged():
    body = [para("Late", style="Heading1"),
            para("Late"),
            para("A real sentence that is long enough.")]
    d = extract(docx(body, headers=["Late"]))
    assert ("repeated-header" in [f["rule"] for f in d["flags"] if f["text"] == "Late"])


def test_header_parts_never_become_body_text():
    body = [para("Late", style="Heading1"), para("A real sentence.")]
    d = extract(docx(body, headers=["Printed Header Line"]))
    assert all("Printed Header Line" not in b["text"]
               for c in d["chapters"] for b in c["blocks"])


def test_block_ids_survive_a_deletion_above_a_flag():
    """Marks point at block ids, never at character offsets."""
    d = extract(docx(BODY))
    flagged = [f for f in d["flags"] if f["rule"] == "all-caps"][0]
    ch = d["chapters"][flagged["chapter"]]
    target = [b for b in ch["blocks"] if b["id"] == flagged["block"]][0]
    assert target["text"] == "SHE SAID NOTHING"
    # simulate the user deleting an earlier block: ids of survivors do not move
    before = {b["id"]: b["text"] for b in ch["blocks"]}
    ch["blocks"] = [b for b in ch["blocks"] if b["text"] != "Okay."]
    after = {b["id"]: b["text"] for b in ch["blocks"]}
    assert before[flagged["block"]] == after[flagged["block"]]


def test_identity_is_the_text_not_the_bytes():
    a = extract(docx(BODY, title="Late", author="A. Writer"))
    b = extract(docx(BODY, title="LATE (draft)", author="Someone Else"))
    assert a["identity"] == b["identity"]
    c = extract(docx(BODY + [para("One extra line.")]))
    assert c["identity"] != a["identity"]


def test_words_counted():
    d = extract(docx([para("One two three."), para("Four five.")]))
    assert d["words"] == 5
    assert d["chapters"][0]["words"] == 5


def test_front_matter_before_first_heading_keeps_its_own_chapter():
    d = extract(docx([para("Title Page"), para("Copyright 2019")]))
    assert len(d["chapters"]) == 1
    assert d["chapters"][0]["title"] == ""


def test_a_doc_that_starts_with_a_heading_keeps_flags_on_the_right_text():
    body = [para("Chapter One", style="Heading1"),
            para("3"),
            para("SHE SAID NOTHING"),
            para("A real sentence that runs long enough to be prose.")]
    d = extract(docx(body))
    assert [c["title"] for c in d["chapters"]] == ["Chapter One"]
    by_text = {f["text"]: f for f in d["flags"]}
    assert by_text["3"]["rule"] == "page-number"
    assert by_text["SHE SAID NOTHING"]["rule"] == "all-caps"
    for f in d["flags"]:
        b = [b for b in d["chapters"][f["chapter"]]["blocks"] if b["id"] == f["block"]]
        assert b and b[0]["text"].strip() == f["text"]

def test_fragment_means_a_line_that_stops_mid_thought():
    from host.junkrules import rules_for
    # complete short lines: prose and dialogue, never flagged
    for ok in ("Okay.", "\u201cYes.\u201d", "He nodded.", "No!", "Wait\u2026",
               "Potions E", "BEQUESTS", "4 Privet Drive,"):
        assert "fragment" not in rules_for(ok), ok
    # actual artefacts: a stray character, a word broken at the hyphen
    assert "fragment" in rules_for("A")
    assert "fragment" in rules_for("the middle of a sen-")
