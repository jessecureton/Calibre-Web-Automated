# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Translation of highlight ranges between a Kobo's KEPUB and CREngine XPointers.

A Kobo highlight is two points in one KEPUB chapter, each a koboSpan path in
Nickel's escaped form (`span#kobo\\.N\\.M`) plus a character offset into that
span's text. KOReader and Readest give the two ends as XPointers, the end one
just after the last highlighted character. Each end is carried across like a
reading position (see position_bridge.translate), by its text: the start by
the first character, the end by the last.

A range is only translated when both ends land in the same chapter, in order,
and the text between them is the same on both sides once whitespace is
ignored, and is the same occurrence of that text (the k-th copy on one side is
the k-th on the other). Anything else returns None: better undelivered than
misplaced.
"""

import bisect
import re
from dataclasses import dataclass
from typing import Optional

from lxml import etree

from ..position_bridge.book import EpubBook
from ..position_bridge.kepub import SpanStream
from ..position_bridge.translate import _carry, _find, _spine_fragment, _squeezed, _Squeezed
from ..position_bridge.xpointer import parse_xpointer

SPAN_PREFIX = 'span#'
# Joins chapters for a book-wide search; XML text can't hold it, so no match crosses it.
_CHAPTER_BARRIER = '\x00'
_XPOINTER_OFFSET = re.compile(r'\.(\d+)$')


@dataclass(frozen=True)
class KoboRange:
    chapter: str             # KEPUB zip path, as KoboLocation.source
    start_path: str          # "span#kobo\.N\.M"
    start_char: int          # offset into the start span's text
    end_path: str
    end_char: int            # exclusive: just after the last highlighted character
    chapter_progress: float  # 0..1, start offset / chapter span-stream text length


def escape_span(span_id: str) -> str:
    """Nickel's path for a koboSpan id: kobo.4.1 -> span#kobo\\.4\\.1."""
    return SPAN_PREFIX + span_id.replace('.', '\\.')


def unescape_span(path: str) -> str:
    """The koboSpan id in a Nickel path, with or without its span# prefix."""
    if path.startswith(SPAN_PREFIX):
        path = path[len(SPAN_PREFIX):]
    return path.replace('\\.', '.')


def span_char(stream: SpanStream, offset: int, end: bool = False) -> Optional[tuple[str, int]]:
    """(span id, character offset in it) for a stream offset.

    A start offset at a span boundary belongs to the span beginning there. An end
    offset belongs to the span holding the character before it, so an end at a
    span's start is the previous span at its full length, never char 0 of the next.
    """
    span = stream.span_at(offset - 1 if end and offset > 0 else offset)
    if span is None:
        return None
    return span, offset - stream.offset_of(span)


def _squeeze(text: str) -> str:
    return ''.join(ch for ch in text if not ch.isspace())


def _occurrences_before(haystack: str, needle: str, index: int) -> int:
    """How many (possibly overlapping) occurrences of needle start before index."""
    count, hit = 0, haystack.find(needle)
    while hit != -1 and hit < index:
        count += 1
        hit = haystack.find(needle, hit + 1)
    return count


def _same_passage(source, start: int, end: int, target, t_start: int, t_end: int) -> bool:
    """Whether target[t_start:t_end] is the same passage as source[start:end].

    The text must match ignoring whitespace, and be the same occurrence of it: a
    passage repeated in the chapter can carry, both ends together, to the wrong copy.
    """
    needle = _squeeze(source.text[start:end])
    if not needle or needle != _squeeze(target.text[t_start:t_end]):
        return False
    src, dst = _squeezed(source), _squeezed(target)
    return (_occurrences_before(src.text, needle, src.at(start))
            == _occurrences_before(dst.text, needle, dst.at(t_start)))


def _span_length(stream: SpanStream, span_id: str) -> Optional[int]:
    for i, (span, start) in enumerate(stream.spans):
        if span == span_id:
            end = stream.spans[i + 1][1] if i + 1 < len(stream.spans) else len(stream.text)
            return end - start
    return None


def _stream_offset(stream: SpanStream, path: str, char: int) -> Optional[int]:
    """Stream offset of char in the span at Nickel path, if it lies within the span."""
    span = unescape_span(path or '')
    start, length = stream.offset_of(span), _span_length(stream, span)
    if start is None or length is None or not 0 <= char <= length:
        return None
    return start + char


def _carry_end(source, offset: int, target) -> Optional[int]:
    """Offset in target just after the last non-whitespace character before offset in source."""
    squeezed = _squeezed(source)
    last = squeezed.at(offset) - 1
    if last < 0:
        return None
    hit = _carry(source, squeezed.index[last], target)
    return None if hit is None else hit + 1


def _end_xpointer(chapter, fragment: int, offset: int) -> str:
    """XPointer just after the character at offset - 1, in that character's text node.

    The character is never whitespace (ends are carried by their last visible
    character), so its collapsed offset plus one is the position after it.
    """
    xp = chapter.xpointer_at(fragment, offset - 1)
    m = _XPOINTER_OFFSET.search(xp)
    if not m:
        return xp
    return f'{xp[:m.start()]}.{int(m.group(1)) + 1}'


def _kobo_range(kepub_path: str, stream: SpanStream, start: int, end: int) -> Optional[KoboRange]:
    first, last = span_char(stream, start), span_char(stream, end, end=True)
    if first is None or last is None:
        return None
    progress = start / len(stream.text) if stream.text else 0.0
    return KoboRange(
        kepub_path, escape_span(first[0]), first[1], escape_span(last[0]), last[1], progress)


def kobo_range_to_xpointers(epub: EpubBook, kepub: EpubBook, r: KoboRange) -> Optional[tuple[str, str]]:
    """(start, end) XPointers into the EPUB for a Kobo highlight range, or None."""
    kepub_path = kepub.resolve(r.chapter)
    epub_path = epub.resolve(kepub_path) if kepub_path else None
    fragment = _spine_fragment(epub, epub_path)
    if fragment is None:
        return None
    stream = kepub.span_stream(kepub_path)
    start = _stream_offset(stream, r.start_path, r.start_char)
    end = _stream_offset(stream, r.end_path, r.end_char)
    if start is None or end is None or end <= start:
        return None
    chapter = epub.chapter_text(epub_path)
    c_start = _carry(stream, start, chapter)
    c_end = _carry_end(stream, end, chapter)
    if c_start is None or c_end is None or c_end <= c_start:
        return None
    if not _same_passage(stream, start, end, chapter, c_start, c_end):
        return None
    return chapter.xpointer_at(fragment, c_start), _end_xpointer(chapter, fragment, c_end)


def xpointers_to_kobo_range(epub: EpubBook, kepub: EpubBook, xp0: str, xp1: str) -> Optional[KoboRange]:
    """The Kobo range for a highlight between two XPointers into the EPUB, or None."""
    try:
        a, b = parse_xpointer(xp0), parse_xpointer(xp1)
    except ValueError:
        return None
    if a.fragment != b.fragment or not 1 <= a.fragment <= len(epub.spine):
        return None
    epub_path = epub.spine[a.fragment - 1]
    kepub_path = kepub.resolve(epub_path)
    if kepub_path is None:
        return None
    chapter = epub.chapter_text(epub_path)
    start, end = chapter.offset_of(a), chapter.offset_of(b)
    if start is None or end is None or end <= start:
        return None
    stream = kepub.span_stream(kepub_path)
    if not stream.spans:
        return None
    k_start = _carry(chapter, start, stream)
    k_end = _carry_end(chapter, end, stream)
    if k_start is None or k_end is None or k_end <= k_start:
        return None
    if not _same_passage(chapter, start, end, stream, k_start, k_end):
        return None
    return _kobo_range(kepub_path, stream, k_start, k_end)


def _streams(kepub: EpubBook, paths) -> list:
    streams = []
    for path in paths:
        try:
            stream = kepub.span_stream(path)
        except (KeyError, etree.LxmlError):
            continue
        if stream.spans:
            streams.append((path, stream))
    return streams


def _search_scope(kepub: EpubBook, chapter: Optional[str], text: str):
    """(needle, streams, squeezed text, chapter bases) for a text search, or None if nothing can be searched."""
    needle = _squeeze(text).replace(_CHAPTER_BARRIER, '')
    if not needle:
        return None
    if chapter is not None:
        path = kepub.resolve(chapter)
        if path is None:
            return None
        streams = _streams(kepub, [path])
    else:
        streams = _streams(kepub, kepub.spine)
    if not streams:
        return None
    if len(streams) == 1:
        squeezed = _squeezed(streams[0][1])
    else:
        squeezed = _Squeezed(_CHAPTER_BARRIER.join(stream.text for _, stream in streams))
    bases, base = [], 0
    for _, stream in streams:
        bases.append(base)
        base += len(stream.text) + len(_CHAPTER_BARRIER)
    return needle, streams, squeezed, bases


def count_text_occurrences(kepub: EpubBook, chapter: Optional[str], text: str) -> int:
    """How many times text occurs, ignoring whitespace, in the chapter (or in the whole book if None).

    Overlapping occurrences count; 0 if the text is empty or the chapter can't be searched.
    """
    scope = _search_scope(kepub, chapter, text)
    if scope is None:
        return 0
    needle, _, squeezed, _ = scope
    return _occurrences_before(squeezed.text, needle, len(squeezed.text) + 1)


def locate_text_range(kepub: EpubBook, chapter: Optional[str], text: str, hint: float) -> Optional[KoboRange]:
    """The Kobo range covering text, matched ignoring whitespace, nearest hint.

    With a chapter, the search is that KEPUB chapter and hint a fraction of it;
    with None, every spine chapter in order and hint a fraction of the book.
    Where the text occurs more than once the nearest to the hint is taken: a
    caller that must not guess checks `count_text_occurrences` first.
    """
    scope = _search_scope(kepub, chapter, text)
    if scope is None:
        return None
    needle, streams, squeezed, bases = scope
    expected = squeezed.at(round(min(max(hint, 0.0), 1.0) * len(squeezed.raw)))
    hit = _find(squeezed, needle, expected)
    if hit is None:
        return None
    raw_start = squeezed.raw_at(hit)
    raw_end = squeezed.index[hit + len(needle) - 1] + 1
    i = bisect.bisect_right(bases, raw_start) - 1
    path, stream = streams[i]
    return _kobo_range(path, stream, raw_start - bases[i], raw_end - bases[i])
