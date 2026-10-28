# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for KEPUB span streams."""

import glob
import os
import re
import zipfile

import pytest
from lxml import etree

from cps.progress_syncing.position_bridge.kepub import SpanStream
from cps.progress_syncing.position_bridge.xpointer import ChapterText

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "position_bridge")
EPUB = os.path.join(FIXTURES, "anna-karenina.epub")
KEPUB = os.path.join(FIXTURES, "anna-karenina.kepub.epub")
REAL = os.environ.get("POSITION_BRIDGE_REAL_LIBRARY")

CHAPTER = (
    b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>'
    b'<div id="book-columns"><div id="book-inner">'
    b'<p><span class="koboSpan" id="kobo.1.1">Happy families </span>'
    b'<span class="koboSpan" id="kobo.1.2">are all <i>alike</i>; </span>'
    b'<span class="koboSpan" id="kobo.1.3">every unhappy<br/> family</span></p>\n'
    b'<p><img src="x.png"/><span class="koboSpan" id="kobo.2.1">is unhappy.</span></p>'
    b'</div></div></body></html>'
)


def nows(s):
    return re.sub(r"\s+", "", s)


def text_without_pre(xhtml):
    """EPUB chapter text minus <pre> blocks, which kepubify leaves without spans."""
    root = etree.fromstring(xhtml, etree.XMLParser(recover=True, huge_tree=True))
    for pre in root.xpath("//*[local-name()='pre']"):
        pre.text = None
        for child in list(pre):
            pre.remove(child)
    return ChapterText(etree.tostring(root)).text


def chapter_pairs(epub_path, kepub_path):
    with zipfile.ZipFile(epub_path) as e, zipfile.ZipFile(kepub_path) as k:
        names = set(e.namelist()) & set(k.namelist())
        for name in sorted(n for n in names if n.endswith((".xhtml", ".html", ".htm"))):
            yield name, e.read(name), k.read(name)


def assert_roundtrip_and_text(epub_path, kepub_path):
    problems = []
    for name, epub_xhtml, kepub_xhtml in chapter_pairs(epub_path, kepub_path):
        stream = SpanStream(kepub_xhtml)
        ends = [start for _, start in stream.spans[1:]] + [len(stream.text)]
        for (span_id, start), end in zip(stream.spans, ends):
            if end == start:
                continue  # an empty span (e.g. around an image) holds no position of its own
            if stream.offset_of(span_id) != start or stream.span_at(start) != span_id:
                problems.append(f"{name}: {span_id} does not round-trip")
        if stream.spans and nows(stream.text) != nows(text_without_pre(epub_xhtml)):
            problems.append(f"{name}: span text differs from the EPUB chapter text")
    assert problems == [], "\n".join(problems[:10])


@pytest.mark.unit
class TestSpanStream:
    def test_text_and_offsets(self):
        stream = SpanStream(CHAPTER)
        assert stream.text == "Happy families are all alike; every unhappy familyis unhappy."
        assert [s for s, _ in stream.spans] == ["kobo.1.1", "kobo.1.2", "kobo.1.3", "kobo.2.1"]
        assert stream.offset_of("kobo.1.2") == len("Happy families ")

    def test_span_at(self):
        stream = SpanStream(CHAPTER)
        assert stream.span_at(stream.text.index("alike")) == "kobo.1.2"
        assert stream.span_at(stream.text.index("is unhappy")) == "kobo.2.1"
        assert stream.span_at(len(stream.text)) == "kobo.2.1"

    def test_unknown_span(self):
        assert SpanStream(CHAPTER).offset_of("kobo.999.999") is None

    def test_no_spans(self):
        stream = SpanStream(b"<html><body><p><img src='a.png'/></p></body></html>")
        assert stream.text == "" and stream.spans == [] and stream.span_at(0) is None


@pytest.mark.unit
@pytest.mark.skipif(not os.path.exists(KEPUB), reason="anna-karenina.kepub.epub fixture not generated yet")
class TestAnnaKarenina:
    def test_span_roundtrip(self):
        assert_roundtrip_and_text(EPUB, KEPUB)


@pytest.mark.unit
@pytest.mark.skipif(not REAL, reason="POSITION_BRIDGE_REAL_LIBRARY not set")
def test_real_library_spans():
    pairs = sorted(glob.glob(os.path.join(REAL, "*.kepub")))
    assert pairs
    for kepub_path in pairs:
        assert_roundtrip_and_text(kepub_path[: -len(".kepub")] + ".epub", kepub_path)
