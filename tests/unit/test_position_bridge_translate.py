# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for translating between Kobo spans and CREngine XPointers."""

import json
import os
import re
import zipfile

import pytest

from cps.progress_syncing.position_bridge.book import EpubBook, content_digest
from cps.progress_syncing.position_bridge.kepub import SpanStream
from cps.progress_syncing.position_bridge.translate import (
    KoboLocation,
    kobo_to_xpointer,
    locate,
    xpointer_to_kobo,
)
from cps.progress_syncing.position_bridge.xpointer import ChapterText, parse_xpointer

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "position_bridge")
EPUB = os.path.join(FIXTURES, "anna-karenina.epub")
KEPUB = os.path.join(FIXTURES, "anna-karenina.kepub.epub")
REFERENCE = os.path.join(FIXTURES, "anna-karenina-xpointers.json")

CONTAINER = (
    '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
    'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
OPF = (
    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
    '<manifest><item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/></manifest>'
    '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>'
)
HEAD = '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>'
CH1 = HEAD + '<p>A scene break follows.</p><p>* * *</p><p>Then more.</p></body></html>'
CH2 = HEAD + (
    '<p>Happy families are all alike; every unhappy family is unhappy in its own way.</p>'
    '<p>Everything was in confusion in the Oblonskys’ house.</p>'
    '<p>* * *</p><p>The wife had discovered that the husband was carrying on an intrigue.</p>'
    '</body></html>'
)


def kepubify_like(xhtml):
    """Wrap each sentence-ish run of a paragraph in koboSpans, roughly as kepubify does."""
    counter = [0]

    def para(m):
        counter[0] += 1
        parts = re.findall(r"[^.;]+[.;]?\s*", m.group(2))
        spans = "".join(
            f'<span class="koboSpan" id="kobo.{counter[0]}.{i + 1}">{p}</span>'
            for i, p in enumerate(parts)
        )
        return f"<p{m.group(1) or ''}>{spans}</p>"

    body = re.sub(r"<p(\s[^>]*)?>(.*?)</p>", para, xhtml)
    return body.replace("<body>", '<body><div id="book-columns"><div id="book-inner">').replace(
        "</body>", "</div></div></body>"
    )


def write_book(path, chapters):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        for name, body in chapters.items():
            z.writestr(f"OEBPS/text/{name}", body)
    return str(path)


@pytest.fixture
def synthetic(tmp_path):
    epub = write_book(tmp_path / "b.epub", {"ch1.xhtml": CH1, "ch2.xhtml": CH2})
    kepub = write_book(
        tmp_path / "b.kepub", {"ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(CH2)}
    )
    return EpubBook(epub), EpubBook(kepub)


@pytest.mark.unit
class TestBook:
    def test_spine(self, synthetic):
        epub, _ = synthetic
        assert epub.spine == ["OEBPS/text/ch1.xhtml", "OEBPS/text/ch2.xhtml"]

    def test_resolve_variants(self, synthetic):
        epub, _ = synthetic
        assert epub.resolve("OEBPS/text/ch2.xhtml") == "OEBPS/text/ch2.xhtml"
        assert epub.resolve("text/ch2.xhtml") == "OEBPS/text/ch2.xhtml"
        assert epub.resolve("ch2.xhtml") == "OEBPS/text/ch2.xhtml"
        assert epub.resolve("nope.xhtml") is None

    def test_content_digest_ignores_opf(self, tmp_path):
        a = write_book(tmp_path / "a.epub", {"ch1.xhtml": CH1})
        b = tmp_path / "b.epub"
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr("OEBPS/content.opf", OPF.replace("3.0", "2.0"))
            z.writestr("OEBPS/text/ch1.xhtml", CH1)
        c = write_book(tmp_path / "c.epub", {"ch1.xhtml": CH1.replace("Then", "And then")})
        assert content_digest(a) == content_digest(str(b))
        assert content_digest(a) != content_digest(c)


@pytest.mark.unit
class TestLocate:
    def test_repeated_text_picks_nearest(self):
        assert locate("ab * * * cd * * * ef", "* * *", expected=12) == 12
        assert locate("ab * * * cd * * * ef", "* * *", expected=0) == 3

    def test_whitespace_insensitive(self):
        assert locate("one\n   two three", "one two", expected=0) == 0

    def test_missing(self):
        assert locate("abc", "xyz", expected=0) is None


@pytest.mark.unit
class TestSyntheticTranslation:
    def test_every_span_roundtrips(self, synthetic):
        epub, kepub = synthetic
        for path in kepub.spine:
            stream = SpanStream(kepub.chapter(path))
            for span, _ in stream.spans:
                xp = kobo_to_xpointer(epub, kepub, path, span)
                back = xpointer_to_kobo(epub, kepub, xp)
                assert back.span == span and back.source == path, (span, xp, back)

    def test_kobo_to_xpointer_lands_on_span_text(self, synthetic):
        epub, kepub = synthetic
        xp = kobo_to_xpointer(epub, kepub, "OEBPS/text/ch2.xhtml", "kobo.2.1")
        chapter = ChapterText(epub.chapter("OEBPS/text/ch2.xhtml"))
        offset = chapter.offset_of(parse_xpointer(xp))
        assert chapter.text[offset:].startswith("Everything was in confusion")
        assert xp.startswith("/body/DocFragment[2]/")

    def test_repeated_scene_break_maps_to_its_own_place(self, synthetic):
        epub, kepub = synthetic
        xp = kobo_to_xpointer(epub, kepub, "OEBPS/text/ch2.xhtml", "kobo.3.1")
        assert xp == "/body/DocFragment[2]/body/p[3]/text().0"

    def test_xpointer_mid_span_maps_to_containing_span(self, synthetic):
        epub, kepub = synthetic
        loc = xpointer_to_kobo(epub, kepub, "/body/DocFragment[2]/body/p[1]/text().40")
        assert loc == KoboLocation("OEBPS/text/ch2.xhtml", "kobo.1.2", loc.chapter_fraction)
        assert 0 < loc.chapter_fraction < 0.5

    def test_context_across_unspanned_code_block(self, tmp_path):
        # kepubify leaves <pre> without spans, so the KEPUB text skips the code.
        code = HEAD + (
            '<p>Look at this short example:</p><pre>x = compute(1, 2)</pre>'
            '<p>Then it ran.</p></body></html>'
        )
        epub = EpubBook(write_book(tmp_path / "c.epub", {"ch1.xhtml": code, "ch2.xhtml": CH2}))
        kepub = EpubBook(write_book(tmp_path / "c.kepub", {
            "ch1.xhtml": kepubify_like(code),
            "ch2.xhtml": kepubify_like(CH2),
        }))
        xp = kobo_to_xpointer(epub, kepub, "OEBPS/text/ch1.xhtml", "kobo.1.1")
        assert xp == "/body/DocFragment[1]/body/p[1]/text().0"

    def test_unknown_source_returns_none(self, synthetic):
        epub, kepub = synthetic
        assert kobo_to_xpointer(epub, kepub, "nope.xhtml", "kobo.1.1") is None
        assert kobo_to_xpointer(epub, kepub, "OEBPS/text/ch2.xhtml", "kobo.99.1") is None

    def test_bad_xpointer_returns_none(self, synthetic):
        epub, kepub = synthetic
        assert xpointer_to_kobo(epub, kepub, "/body/DocFragment[99]/body/p/text().0") is None
        assert xpointer_to_kobo(epub, kepub, "epubcfi(/6/4)") is None


@pytest.mark.unit
@pytest.mark.skipif(not os.path.exists(KEPUB), reason="anna-karenina.kepub.epub fixture not generated yet")
class TestAnnaKarenina:
    def test_reference_words_to_spans_and_back(self):
        epub, kepub = EpubBook(EPUB), EpubBook(KEPUB)
        with open(REFERENCE) as f:
            data = json.load(f)
        misses = []
        for fragment in data["fragments"]:
            path = epub.spine[fragment["docfragment"] - 1]
            chapter = ChapterText(epub.chapter(path))
            for word in fragment["words"]:
                loc = xpointer_to_kobo(epub, kepub, word["xp"])
                back = loc and kobo_to_xpointer(epub, kepub, loc.source, loc.span)
                if not back:
                    misses.append(f"{word['xp']}: no translation")
                    continue
                start = chapter.offset_of(parse_xpointer(back))
                word_at = chapter.offset_of(parse_xpointer(word["xp"]))
                stream = SpanStream(kepub.chapter(loc.source))
                span_len = len(stream.text) - stream.offset_of(loc.span)
                if not (start <= word_at <= start + span_len):
                    misses.append(f"{word['xp']}: came back at {back}")
        assert misses == [], f"{len(misses)} misses:\n" + "\n".join(misses[:10])

    def test_every_span_roundtrips(self):
        epub, kepub = EpubBook(EPUB), EpubBook(KEPUB)
        misses = []
        for path in kepub.spine:
            stream = SpanStream(kepub.chapter(path))
            ends = [s for _, s in stream.spans[1:]] + [len(stream.text)]
            for (span, start), end in zip(stream.spans, ends):
                if end == start:
                    continue
                xp = kobo_to_xpointer(epub, kepub, path, span)
                back = xp and xpointer_to_kobo(epub, kepub, xp)
                if not back or back.span != span:
                    misses.append(f"{path}#{span} -> {xp} -> {back}")
        assert misses == [], f"{len(misses)} misses:\n" + "\n".join(misses[:10])
