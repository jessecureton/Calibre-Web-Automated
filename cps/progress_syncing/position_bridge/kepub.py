# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
KEPUB span streams.

A Kobo reports its position as a koboSpan id (`kobo.X.Y`) inside a chapter of
the KEPUB it holds. SpanStream concatenates the text of a chapter's koboSpans
in document order and maps between span ids and offsets into that text.
"""

import bisect
from typing import Optional

from .xpointer import SKIP_TAGS, _is_element, _local, parse_chapter


def _span_id(el) -> Optional[str]:
    if _local(el) != 'span' or 'koboSpan' not in (el.get('class') or '').split():
        return None
    span_id = el.get('id') or ''
    return span_id if span_id.startswith('kobo.') else None


class SpanStream:
    """A KEPUB chapter's koboSpan text, with span id <-> offset mapping."""

    def __init__(self, xhtml: bytes):
        self.spans = []      # (span_id, start offset in .text)
        self._parts = []
        self._length = 0
        self._walk(parse_chapter(xhtml), None)
        self.text = ''.join(self._parts)
        self._index = {span_id: start for span_id, start in reversed(self.spans)}
        self._starts = [start for _, start in self.spans]

    def _add(self, text, span):
        if text and span:
            self._parts.append(text)
            self._length += len(text)

    def _walk(self, el, span):
        if not _is_element(el) or _local(el) in SKIP_TAGS:
            return
        own = _span_id(el)
        if own:
            self.spans.append((own, self._length))
            span = own
        self._add(el.text, span)
        for child in el:
            self._walk(child, span)
            self._add(child.tail, span)

    def offset_of(self, span_id: str) -> Optional[int]:
        """Offset of the span's first character in .text, or None if it's not here."""
        return self._index.get(span_id)

    def span_at(self, offset: int) -> Optional[str]:
        """The span holding the character at offset; the last span at the very end."""
        if not self.spans:
            return None
        i = bisect.bisect_right(self._starts, offset) - 1
        if i < 0:
            return self.spans[0][0]
        # Empty spans share a start with the next one; prefer the one with text.
        while i > 0 and self._starts[i - 1] == self._starts[i] and \
                self._span_end(i) == self._starts[i]:
            i -= 1
        return self.spans[i][0]

    def _span_end(self, i: int) -> int:
        return self._starts[i + 1] if i + 1 < len(self._starts) else len(self.text)
