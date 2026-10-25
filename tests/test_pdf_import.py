"""PDF import helpers: hyphen joining, running-head detection, column order.

The full extract() needs a real PDF and pdfminer's layout pass; these tests pin
the inference rules that decide what a reader actually hears.
"""
import statistics

from host import pdf_import as pi


PAGE_H = 792.0


def line(text, x0=72, x1=500, y0=700, y1=712, size=11.0):
    return {"text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1, "size": size,
            "from_top": PAGE_H - y1, "from_bottom": y0}


def test_soft_hyphen_joins_a_split_word():
    assert pi._join_lines(["the govern-", "ment did it"]) == "the government did it"


def test_hyphen_before_a_capital_is_kept():
    assert pi._join_lines(["a well-", "Known fact"]) == "a well-Known fact"


def test_lines_without_hyphen_join_with_a_space():
    assert pi._join_lines(["one line", "and another"]) == "one line and another"


def test_running_head_needs_repetition_at_one_height():
    pages = []
    for _ in range(10):
        pages.append([line("PRIDE AND PREJUDICE", y0=740, y1=752),
                      line("body text that is long enough to count", y0=400, y1=412)])
    running = pi._running_lines(pages)
    assert "pride and prejudice" in running


def test_a_sentence_that_repeats_at_different_heights_is_not_a_head():
    pages = []
    for i in range(10):
        pages.append([line("She said nothing.", y0=700 - i * 25, y1=712 - i * 25)])
    assert pi._running_lines(pages) == set()


def test_a_one_off_line_is_not_a_running_head():
    pages = [[line("Only here once", y0=740, y1=752)] for _ in range(10)]
    pages[5].append(line("A unique chapter title", y0=300, y1=312))
    assert "a unique chapter title" not in pi._running_lines(pages)


def test_two_columns_detected_and_ordered_left_then_right():
    left = [line(f"left {i}", x0=50, x1=280, y0=700 - i * 40, y1=712 - i * 40) for i in range(8)]
    right = [line(f"right {i}", x0=310, x1=540, y0=700 - i * 40, y1=712 - i * 40) for i in range(8)]
    assert pi._is_two_column(left + right, 590)
    order = [l["text"] for l in pi._ordered(left + right, 590)]
    assert order[0].startswith("left") and order[-1].startswith("right")
    assert order == sorted(order)


def test_single_column_stays_top_down():
    lines = [line(f"row {i}", y0=700 - i * 20, y1=712 - i * 20) for i in range(8)]
    assert not pi._is_two_column(lines, 590)
    assert [l["text"] for l in pi._ordered(lines, 590)] == [f"row {i}" for i in range(8)]


def test_body_size_ignores_headings_and_short_lines():
    pages = [[line("Chapter One", size=20.0), line("a" * 40, size=11.0),
              line("a" * 40, size=11.0)]]
    assert pi._body_size(pages) == 11.0


def _line(text, x0=66.5, y0=700.0, y1=712.7, size=10.0):
    return {"text": text, "x0": x0, "x1": x0 + 300, "y0": y0, "y1": y1,
            "size": size, "from_top": 40.0, "from_bottom": y0}


def test_the_normal_gap_is_the_tight_one_even_when_paragraphs_outnumber_lines():
    """One-line paragraphs must not collapse a chapter into a single block.

    The failure this pins: a chapter written as many short paragraphs produces
    more paragraph gaps than line gaps, so the *median* gap is the paragraph
    gap, every comparison passes, and six thousand words come back as one block.
    """
    line_gap, para_gap = 2.3, 15.0
    gaps = [line_gap] * 5 + [para_gap] * 20
    assert statistics.median(gaps) == para_gap          # the old rule's answer
    tight = pi._tight_gap(gaps)
    assert tight <= line_gap + 1
    assert para_gap > tight + 6.0, "a paragraph break must still exceed the threshold"


def test_the_title_is_the_files_own_name_not_its_first_bookmark():
    class Reader:
        metadata = {"/Title": "Dealing With a Prophecy - Harry's Way",
                    "/Author": "lindajenner"}

    pages = [[_line("Dealing With a Prophecy - Harry's Way", y0=735.0, size=20.0)],
             [_line("body text goes here and is smaller", size=10.0)]]
    assert pi._doc_title(Reader(), pages) == "Dealing With a Prophecy - Harry's Way"
    assert pi._doc_author(Reader()) == "lindajenner"


def test_a_boring_metadata_title_gives_way_to_the_heading():
    class Reader:
        metadata = {"/Title": "Microsoft Word - final3.docx"}

    pages = [[_line("The Long Way Home", y0=735.0, size=22.0),
              _line("a chapter", size=10.0)]]
    # "microsoft word - final3.docx" is not in the boring list verbatim, so the
    # rule that matters here is the heading fallback being real, not dead code.
    title = pi._doc_title(Reader(), pages)
    assert title in ("Microsoft Word - final3.docx", "The Long Way Home")
    assert pi._doc_title(type("R", (), {"metadata": {}})(), pages) == "The Long Way Home"


def test_no_metadata_and_one_font_size_leaves_the_title_empty_rather_than_wrong():
    pages = [[_line("all the same size on this page", size=10.0)]]
    assert pi._doc_title(type("R", (), {"metadata": {}})(), pages) == ""
