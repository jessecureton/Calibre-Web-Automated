# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for CREngine XPointer resolution over EPUB XHTML."""

import json
import os
import posixpath
import re
import zipfile

import pytest
from lxml import etree

from cps.progress_syncing.position_bridge.xpointer import ChapterText, parse_xpointer

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "position_bridge")
EPUB = os.path.join(FIXTURES, "anna-karenina.epub")
REFERENCE = os.path.join(FIXTURES, "anna-karenina-xpointers.json")


def spine_paths(epub_path):
    """Zip paths of the spine items, in order (a minimal reader for these tests)."""
    with zipfile.ZipFile(epub_path) as z:
        container = etree.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf = etree.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {i.get("id"): i.get("href") for i in opf.iterfind(".//{*}manifest/{*}item")}
        return [
            posixpath.normpath(posixpath.join(base, manifest[r.get("idref")]))
            for r in opf.iterfind(".//{*}spine/{*}itemref")
        ]


def load_chapters(epub_path):
    paths = spine_paths(epub_path)
    with zipfile.ZipFile(epub_path) as z:
        return {i + 1: ChapterText(z.read(p)) for i, p in enumerate(paths)}


def reference_words(path):
    with open(path) as f:
        data = json.load(f)
    for fragment in data["fragments"]:
        for word in fragment["words"]:
            yield fragment["docfragment"], word


@pytest.fixture(scope="module")
def chapters():
    return load_chapters(EPUB)


@pytest.mark.unit
class TestReferenceXPointers:
    def test_reference_pull(self, chapters):
        misses = []
        for fragment, word in reference_words(REFERENCE):
            xp = parse_xpointer(word["xp"])
            assert xp.fragment == fragment
            offset = chapters[fragment].offset_of(xp)
            got = None if offset is None else chapters[fragment].text[offset:offset + len(word["text"])]
            if got != word["text"]:
                misses.append(f"{word['xp']}: expected {word['text']!r}, got {got!r}")
        assert misses == [], f"{len(misses)} misses:\n" + "\n".join(misses[:10])

    def test_reference_push(self, chapters):
        misses = []
        for fragment, word in reference_words(REFERENCE):
            offset = chapters[fragment].offset_of(parse_xpointer(word["xp"]))
            back = chapters[fragment].xpointer_at(fragment, offset)
            if back != word["xp"]:
                misses.append(f"{word['text']!r}: expected {word['xp']}, got {back}")
        assert misses == [], f"{len(misses)} misses:\n" + "\n".join(misses[:10])

    def test_element_only_xpointer(self, chapters):
        fragment, word = next(
            (f, w) for f, w in reference_words(REFERENCE) if w["xp"].endswith("/text().0")
        )
        element_only = word["xp"][: -len("/text().0")]
        chapter = chapters[fragment]
        assert chapter.offset_of(parse_xpointer(element_only)) == chapter.offset_of(
            parse_xpointer(word["xp"])
        )


@pytest.mark.unit
class TestParse:
    def test_parse_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_xpointer("epubcfi(/6/4)")

    def test_omitted_docfragment_index(self):
        assert parse_xpointer("/body/DocFragment/body/p").fragment == 1

    def test_parse_fields(self):
        xp = parse_xpointer("/body/DocFragment[6]/body/section/p[19]/text()[2].180")
        assert xp.fragment == 6
        assert xp.steps == (("section", None), ("p", 19))
        assert xp.text_index == 2
        assert xp.offset == 180

    def test_element_offset_means_text_offset(self):
        # CREngine sometimes writes `div[1].0`; Readest reads it as `div[1]/text().0`.
        xp = parse_xpointer("/body/DocFragment[3]/body/div[1].7")
        assert xp.steps == (("div", 1),)
        assert xp.text_index == 1 and xp.offset == 7

    def test_bare_text_step(self):
        xp = parse_xpointer("/body/DocFragment[2]/body/div[1]/h1/text().5")
        assert xp.text_index == 1 and xp.offset == 5


@pytest.mark.unit
class TestCrengineWhitespace:
    XHTML = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>\n'
        b'  <p>\n    One  two\n three <i>four</i>  five</p>\n'
        b'  <pre>a  b</pre>\n'
        b'</body></html>'
    )

    def test_leading_blank_dropped_and_runs_collapsed(self):
        chapter = ChapterText(self.XHTML)
        offset = chapter.text.index("five")
        # Text children of <p>: "\n    One  two\n three " and "  five"; crengine
        # keeps both (the first isn't blank) and collapses each whitespace run.
        assert chapter.xpointer_at(1, offset) == "/body/DocFragment[1]/body/p/text()[2].1"
        assert chapter.offset_of(parse_xpointer("/body/DocFragment[1]/body/p/text()[2].1")) == offset

    def test_pre_keeps_whitespace(self):
        chapter = ChapterText(self.XHTML)
        offset = chapter.text.index("a  b") + 3
        assert chapter.xpointer_at(1, offset) == "/body/DocFragment[1]/body/pre/text().3"


READEST = os.environ.get("READEST_CLONE")


@pytest.mark.unit
@pytest.mark.skipif(not READEST, reason="READEST_CLONE not set")
def test_crengine_oracle_external():
    """Readest's real-CREngine oracle for Alice, read in place (never copied here)."""
    base = os.path.join(READEST, "apps", "readest-app", "src", "__tests__", "fixtures")
    oracle = os.path.join(base, "crengine", "sample-alice.json")
    chapters = load_chapters(os.path.join(base, "data", "sample-alice.epub"))
    pull, push, words = [], [], 0
    for fragment, word in reference_words(oracle):
        words += 1
        xp_text = re.sub(r"^/body/DocFragment/", "/body/DocFragment[1]/", word["xp"])
        offset = chapters[fragment].offset_of(parse_xpointer(xp_text))
        got = None if offset is None else chapters[fragment].text[offset:offset + len(word["text"])]
        if got != word["text"]:
            pull.append(f"{word['xp']}: expected {word['text']!r}, got {got!r}")
            continue
        back = chapters[fragment].xpointer_at(fragment, offset)
        if back != xp_text:
            push.append(f"{word['text']!r}: expected {xp_text}, got {back}")
    assert words == 707
    assert pull == [] and push == [], "\n".join((pull + push)[:10])
