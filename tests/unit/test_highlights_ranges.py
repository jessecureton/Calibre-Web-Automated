# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for translating highlight ranges between Kobo spans and XPointers."""

import os
import random
import re

import pytest

from cps.progress_syncing.highlights.ranges import (
    KoboRange,
    escape_span,
    kobo_range_to_xpointers,
    locate_text_range,
    span_char,
    unescape_span,
    xpointers_to_kobo_range,
)
from cps.progress_syncing.position_bridge.book import EpubBook
from cps.progress_syncing.position_bridge.xpointer import parse_xpointer

from .position_bridge_books import CH1, CH2, kepubify_like, write_book

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "position_bridge")
AK_EPUB = os.path.join(FIXTURES, "anna-karenina.epub")
AK_KEPUB = os.path.join(FIXTURES, "anna-karenina.kepub.epub")

CH1_PATH = "OEBPS/text/ch1.xhtml"
CH2_PATH = "OEBPS/text/ch2.xhtml"
CH1_XP = "/body/DocFragment[1]/body/p[1]/text().2"
CH2_XP = "/body/DocFragment[2]/body/p[4]/text().8"


def squeeze(text):
    return re.sub(r"\s+", "", text)


@pytest.fixture
def books(tmp_path):
    epub = write_book(tmp_path / "b.epub", {"ch1.xhtml": CH1, "ch2.xhtml": CH2})
    kepub = write_book(
        tmp_path / "b.kepub", {"ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(CH2)}
    )
    return epub, kepub


def resolve_text(epub_path, xpointer):
    epub = EpubBook(epub_path)
    xp = parse_xpointer(xpointer)
    chapter = epub.chapter_text(epub.spine[xp.fragment - 1])
    return chapter.text[chapter.offset_of(xp):]


def epub_between(epub, xp0, xp1):
    a, b = parse_xpointer(xp0), parse_xpointer(xp1)
    assert a.fragment == b.fragment
    chapter = epub.chapter_text(epub.spine[a.fragment - 1])
    return chapter.text[chapter.offset_of(a):chapter.offset_of(b)]


def kobo_between(kepub, r):
    stream = kepub.span_stream(kepub.resolve(r.chapter))
    start = stream.offset_of(unescape_span(r.start_path)) + r.start_char
    end = stream.offset_of(unescape_span(r.end_path)) + r.end_char
    return stream.text[start:end]


def ch2_range(kepub, start_char=4, end_char=30):
    stream = kepub.span_stream(CH2_PATH)
    start = stream.offset_of("kobo.4.1") + start_char
    return KoboRange(
        chapter=CH2_PATH,
        start_path=r"span#kobo\.4\.1",
        start_char=start_char,
        end_path=r"span#kobo\.4\.1",
        end_char=end_char,
        chapter_progress=start / len(stream.text),
    )


@pytest.mark.unit
class TestSpanPaths:
    def test_escape_and_unescape(self):
        assert escape_span("kobo.4.1") == r"span#kobo\.4\.1"
        assert unescape_span(r"span#kobo\.4\.1") == "kobo.4.1"
        assert unescape_span(r"kobo\.4\.1") == "kobo.4.1"
        assert unescape_span(escape_span("kobo.12.30")) == "kobo.12.30"

    def test_span_char_at_boundaries(self, books):
        kepub = EpubBook(books[1])
        stream = kepub.span_stream(CH2_PATH)
        boundary = stream.offset_of("kobo.1.2")
        first_len = boundary - stream.offset_of("kobo.1.1")
        assert span_char(stream, boundary) == ("kobo.1.2", 0)
        assert span_char(stream, boundary, end=True) == ("kobo.1.1", first_len)
        assert span_char(stream, boundary + 3) == ("kobo.1.2", 3)
        assert span_char(stream, boundary + 3, end=True) == ("kobo.1.2", 3)
        assert span_char(stream, 0) == ("kobo.1.1", 0)


@pytest.mark.unit
class TestSyntheticRanges:
    def test_kobo_range_round_trips_through_xpointers(self, books):
        epub_path, kepub_path = books
        epub, kepub = EpubBook(epub_path), EpubBook(kepub_path)
        r = ch2_range(kepub)
        xps = kobo_range_to_xpointers(epub, kepub, r)
        assert xps and resolve_text(epub_path, xps[0]).startswith("wife had discovered")
        assert xpointers_to_kobo_range(epub, kepub, *xps) == r

    def test_range_text_matches_on_both_sides(self, books):
        epub, kepub = EpubBook(books[0]), EpubBook(books[1])
        r = ch2_range(kepub)
        xps = kobo_range_to_xpointers(epub, kepub, r)
        assert squeeze(kobo_between(kepub, r)) == squeeze("wife had discovered that t")
        assert squeeze(epub_between(epub, *xps)) == squeeze(kobo_between(kepub, r))

    def test_end_points_just_after_last_character(self, books):
        epub, kepub = EpubBook(books[0]), EpubBook(books[1])
        stream = kepub.span_stream(CH2_PATH)
        length = len(stream.text) - stream.offset_of("kobo.4.1")
        r = ch2_range(kepub, 0, length)
        xps = kobo_range_to_xpointers(epub, kepub, r)
        # The end stays in the highlighted paragraph, not char 0 of what follows.
        assert xps[1] == "/body/DocFragment[2]/body/p[4]/text().69"
        assert xpointers_to_kobo_range(epub, kepub, *xps) == r

    def test_range_spanning_kobo_spans(self, books):
        epub, kepub = EpubBook(books[0]), EpubBook(books[1])
        xps = (
            "/body/DocFragment[2]/body/p[1]/text().6",
            "/body/DocFragment[2]/body/p[2]/text().10",
        )
        r = xpointers_to_kobo_range(epub, kepub, *xps)
        assert (r.start_path, r.start_char) == (r"span#kobo\.1\.1", 6)
        assert (r.end_path, r.end_char) == (r"span#kobo\.2\.1", 10)
        assert squeeze(kobo_between(kepub, r)) == squeeze(epub_between(epub, *xps))
        assert kobo_range_to_xpointers(epub, kepub, r) == xps

    def test_end_at_span_boundary_belongs_to_previous_span(self, books):
        epub, kepub = EpubBook(books[0]), EpubBook(books[1])
        # Ends just after "way.", the end of kobo.1.2 and of p[1].
        xps = (
            "/body/DocFragment[2]/body/p[1]/text().30",
            "/body/DocFragment[2]/body/p[1]/text().77",
        )
        r = xpointers_to_kobo_range(epub, kepub, *xps)
        stream = kepub.span_stream(CH2_PATH)
        assert r.end_path == r"span#kobo\.1\.2"
        assert r.end_char == stream.offset_of("kobo.2.1") - stream.offset_of("kobo.1.2")

    def test_range_across_chapters_is_untranslatable(self, books):
        epub, kepub = EpubBook(books[0]), EpubBook(books[1])
        assert xpointers_to_kobo_range(epub, kepub, CH1_XP, CH2_XP) is None
        r = KoboRange(CH2_PATH, r"span#kobo\.1\.1", 0, r"span#kobo\.9\.1", 3, 0.0)
        assert kobo_range_to_xpointers(epub, kepub, r) is None

    def test_end_before_start_is_untranslatable(self, books):
        epub, kepub = EpubBook(books[0]), EpubBook(books[1])
        xps = kobo_range_to_xpointers(epub, kepub, ch2_range(kepub))
        assert xpointers_to_kobo_range(epub, kepub, xps[1], xps[0]) is None
        assert kobo_range_to_xpointers(epub, kepub, ch2_range(kepub, 30, 4)) is None
        assert kobo_range_to_xpointers(epub, kepub, ch2_range(kepub, 4, 4)) is None

    def test_unknown_or_bad_input_is_untranslatable(self, books):
        epub, kepub = EpubBook(books[0]), EpubBook(books[1])
        r = ch2_range(kepub)
        assert kobo_range_to_xpointers(
            epub, kepub, KoboRange("nope.xhtml", r.start_path, 4, r.end_path, 30, 0.0)) is None
        assert kobo_range_to_xpointers(
            epub, kepub, KoboRange(CH2_PATH, r"span#kobo\.99\.1", 0, r.end_path, 30, 0.0)) is None
        assert kobo_range_to_xpointers(epub, kepub, ch2_range(kepub, 4, 500)) is None
        assert xpointers_to_kobo_range(epub, kepub, "epubcfi(/6/4)", CH2_XP) is None
        assert xpointers_to_kobo_range(
            epub, kepub, "/body/DocFragment[9]/body/p/text().0",
            "/body/DocFragment[9]/body/p/text().4") is None

    def test_different_text_on_the_other_side_is_untranslatable(self, tmp_path):
        # The EPUB has a word the KEPUB lacks inside the highlighted passage.
        epub = EpubBook(write_book(tmp_path / "e.epub", {
            "ch1.xhtml": CH1,
            "ch2.xhtml": CH2.replace("the husband was", "the husband secretly was"),
        }))
        kepub = EpubBook(write_book(tmp_path / "k.kepub", {
            "ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(CH2),
        }))
        # Both ends still carry by their text; only the text between them differs.
        assert kobo_range_to_xpointers(epub, kepub, ch2_range(kepub, 0, 69)) is None
        whole_p4 = ("/body/DocFragment[2]/body/p[4]/text().0",
                    "/body/DocFragment[2]/body/p[4]/text().78")
        assert xpointers_to_kobo_range(epub, kepub, *whole_p4) is None

    def test_locate_text_range_finds_nearest_repeat(self, books):
        kepub = EpubBook(books[1])
        first = locate_text_range(kepub, CH2_PATH, "unhappy", 0.0)
        second = locate_text_range(kepub, CH2_PATH, "unhappy", 0.3)
        assert (first.start_path, first.start_char) == (r"span#kobo\.1\.2", 6)
        assert (second.start_path, second.start_char) == (r"span#kobo\.1\.2", 24)
        assert squeeze(kobo_between(kepub, second)) == "unhappy"
        assert second.end_char - second.start_char == len("unhappy")
        assert first.chapter == CH2_PATH and 0 < first.chapter_progress < second.chapter_progress

    def test_locate_text_range_across_book(self, books):
        kepub = EpubBook(books[1])
        early = locate_text_range(kepub, None, "*  *\n*", 0.0)
        late = locate_text_range(kepub, None, "* * *", 0.9)
        assert early.chapter == CH1_PATH and early.start_path == r"span#kobo\.2\.1"
        assert late.chapter == CH2_PATH and late.start_path == r"span#kobo\.3\.1"
        assert (late.start_char, late.end_char) == (0, 5)

    def test_locate_text_range_spanning_spans(self, books):
        kepub = EpubBook(books[1])
        r = locate_text_range(kepub, "ch2.xhtml", "alike; every unhappy", 0.0)
        assert (r.start_path, r.end_path) == (r"span#kobo\.1\.1", r"span#kobo\.1\.2")
        assert squeeze(kobo_between(kepub, r)) == "alike;everyunhappy"

    def test_locate_text_range_missing(self, books):
        kepub = EpubBook(books[1])
        assert locate_text_range(kepub, CH2_PATH, "not in the book", 0.5) is None
        assert locate_text_range(kepub, CH2_PATH, "   ", 0.5) is None
        assert locate_text_range(kepub, "nope.xhtml", "unhappy", 0.5) is None
        # A passage split across two chapters is not one range.
        assert locate_text_range(kepub, None, "Then more. Happy families", 0.5) is None


@pytest.mark.unit
@pytest.mark.skipif(not os.path.exists(AK_KEPUB), reason="anna-karenina.kepub.epub fixture not generated yet")
class TestAnnaKarenina:
    def test_anna_karenina_ranges_round_trip(self):
        epub, kepub = EpubBook(AK_EPUB), EpubBook(AK_KEPUB)
        candidates = []
        for path in kepub.spine:
            stream = kepub.span_stream(path)
            ends = [s for _, s in stream.spans[1:]] + [len(stream.text)]
            for i, ((span, start), end) in enumerate(zip(stream.spans, ends)):
                if stream.text[start:end].strip():
                    candidates.append((path, i))
        rng = random.Random(20261030)
        misses = []
        for path, i in rng.sample(candidates, 50):
            stream = kepub.span_stream(path)
            spans = stream.spans
            last = min(i + rng.randint(0, 2), len(spans) - 1)
            last_end = spans[last + 1][1] if last + 1 < len(spans) else len(stream.text)
            start = spans[i][1]
            r = KoboRange(
                chapter=path,
                start_path=escape_span(spans[i][0]),
                start_char=0,
                end_path=escape_span(spans[last][0]),
                end_char=last_end - spans[last][1],
                chapter_progress=start / len(stream.text),
            )
            text = squeeze(stream.text[start:last_end])
            xps = kobo_range_to_xpointers(epub, kepub, r)
            back = xps and xpointers_to_kobo_range(epub, kepub, *xps)
            if not back:
                misses.append(f"{path}#{spans[i][0]}: {xps} -> {back}")
                continue
            there = squeeze(epub_between(epub, *xps))
            again = squeeze(kobo_between(kepub, back))
            if not (there == again == text and back.chapter == path
                    and back.start_path == r.start_path):
                misses.append(f"{path}#{spans[i][0]}: {text!r} -> {there!r} -> {again!r}")
        assert misses == [], f"{len(misses)} misses:\n" + "\n".join(misses[:10])
