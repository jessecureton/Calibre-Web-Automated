# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Translation between Kobo span locations and CREngine XPointers.

The KEPUB and the EPUB share chapter files (kepubify keeps the layout) but not
their markup, so a position is carried across by its text: the next
CONTEXT_CHARS non-whitespace characters at the position are found in the other
rendition's chapter, nearest the same relative place. Anything that can't be
found is not translated; the bridge never guesses.
"""

import bisect
import re
from dataclasses import dataclass
from typing import Optional

from .book import EpubBook
from .xpointer import parse_xpointer

CONTEXT_CHARS = 40


@dataclass(frozen=True)
class KoboLocation:
    source: str              # full zip path
    span: str                # "kobo.X.Y"
    chapter_fraction: float  # 0..1, span start / chapter text length

    def key(self) -> str:
        return f"{self.source}#{self.span}"


class _Squeezed:
    """A text without whitespace, mapped back to raw offsets."""

    def __init__(self, text: str):
        self.raw = text
        self.index = [i for i, ch in enumerate(text) if not ch.isspace()]
        self.text = ''.join(text[i] for i in self.index)

    def at(self, raw_offset: int) -> int:
        """Squeezed index of the first non-whitespace character at or after raw_offset."""
        return bisect.bisect_left(self.index, raw_offset)

    def raw_at(self, squeezed: int) -> int:
        return self.index[squeezed] if squeezed < len(self.index) else len(self.raw)


def _find(haystack: _Squeezed, needle: str, expected: int) -> Optional[int]:
    """Squeezed index of the occurrence of needle nearest squeezed index expected."""
    if not needle:
        return None
    hits = [m.start() for m in re.finditer(re.escape(needle), haystack.text)]
    if not hits:
        return None
    return min(hits, key=lambda h: abs(h - expected))


def locate(haystack: str, needle: str, expected: int) -> Optional[int]:
    """Raw offset in haystack of the whitespace-insensitive hit nearest raw offset expected."""
    squeezed = _Squeezed(haystack)
    hit = _find(squeezed, re.sub(r'\s+', '', needle), squeezed.at(expected))
    return None if hit is None else squeezed.raw_at(hit)


def _squeezed(parsed) -> _Squeezed:
    """The squeezed form of a ChapterText or SpanStream's text, computed once."""
    if getattr(parsed, '_squeezed', None) is None:
        parsed._squeezed = _Squeezed(parsed.text)
    return parsed._squeezed


def _carry(source, offset: int, target) -> Optional[int]:
    """Offset in target's text of the position at offset in source's, matched by text."""
    src, dst = _squeezed(source), _squeezed(target)
    if not src.text or not dst.text:
        return 0
    pos = min(src.at(offset), len(src.text))
    # A window of text around the position, starting at it when there is room. A
    # shorter window is tried when the long one spans text only one side has
    # (kepubify leaves <pre> code listings without spans).
    for size in (CONTEXT_CHARS, CONTEXT_CHARS // 2, CONTEXT_CHARS // 4):
        start = max(0, min(pos, len(src.text) - size))
        lead = pos - start
        expected = round(start / len(src.text) * len(dst.text))
        hit = _find(dst, src.text[start:start + size], expected)
        if hit is not None:
            return dst.raw_at(hit + lead)
    return None


def _spine_fragment(epub: EpubBook, path: Optional[str]) -> Optional[int]:
    if path is None:
        return None
    try:
        return epub.spine.index(path) + 1
    except ValueError:
        return None


def kobo_to_xpointer(epub: EpubBook, kepub: EpubBook, source: str, span: str) -> Optional[str]:
    """XPointer into the EPUB for the start of span in the KEPUB chapter source."""
    kepub_path = kepub.resolve(source)
    epub_path = epub.resolve(kepub_path) if kepub_path else None
    fragment = _spine_fragment(epub, epub_path)
    if fragment is None:
        return None
    stream = kepub.span_stream(kepub_path)
    span_offset = stream.offset_of(span)
    if span_offset is None:
        return None
    chapter = epub.chapter_text(epub_path)
    offset = _carry(stream, span_offset, chapter)
    if offset is None:
        return None
    return chapter.xpointer_at(fragment, offset)


def xpointer_to_kobo(epub: EpubBook, kepub: EpubBook, xpointer: str) -> Optional[KoboLocation]:
    """The KEPUB span holding the position an XPointer into the EPUB points at."""
    try:
        xp = parse_xpointer(xpointer)
    except ValueError:
        return None
    if not 1 <= xp.fragment <= len(epub.spine):
        return None
    epub_path = epub.spine[xp.fragment - 1]
    kepub_path = kepub.resolve(epub_path)
    if kepub_path is None:
        return None
    chapter = epub.chapter_text(epub_path)
    offset = chapter.offset_of(xp)
    if offset is None:
        return None
    stream = kepub.span_stream(kepub_path)
    if not stream.spans:
        return None
    target = _carry(chapter, offset, stream)
    if target is None:
        return None
    span = stream.span_at(target)
    fraction = stream.offset_of(span) / len(stream.text) if stream.text else 0.0
    return KoboLocation(kepub_path, span, fraction)
