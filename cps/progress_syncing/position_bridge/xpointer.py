# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
CREngine XPointers over EPUB chapter XHTML.

KOReader sync clients (KOReader, Readest) report positions as CREngine
XPointers such as `/body/DocFragment[6]/body/section/p[19]/text()[1].180`:
DocFragment N is spine item N, the steps below `body` index same-tag element
siblings (the index is omitted when there is only one), `text()[K]` picks the
Kth direct text child CREngine keeps, and the offset counts that text with each
whitespace run collapsed to one character.

ChapterText maps between those XPointers and offsets into the chapter's raw
body text, which is what the position bridge matches against the Kobo's KEPUB.
"""

import bisect
import html.entities
import re
from dataclasses import dataclass
from typing import Optional

from lxml import etree
from lxml import html as lxml_html

# Block containers, where CREngine drops a leading whitespace-only text child.
BLOCK_TAGS = frozenset({
    'body', 'div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'dl', 'dt',
    'dd', 'blockquote', 'pre', 'section', 'article', 'aside', 'header', 'footer', 'nav',
    'main', 'figure', 'figcaption', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th',
    'caption', 'address', 'details', 'summary', 'form', 'fieldset', 'center',
})
# Tags whose CREngine style is `white-space: pre`: offsets count raw text.
PRE_TAGS = frozenset({'pre', 'code', 'listing', 'plaintext', 'xmp', 'textarea'})
SKIP_TAGS = frozenset({'script', 'style'})
WHITESPACE = ' \t\n\r\f'

_XPOINTER = re.compile(
    r'^/body/DocFragment(?:\[(\d+)\])?/body'
    r'((?:/[A-Za-z][\w.-]*(?:\[\d+\])?)*?)'
    r'(?:/text\(\)(?:\[(\d+)\])?\.(\d+)|\.(\d+))?$'
)
_STEP = re.compile(r'/([A-Za-z][\w.-]*)(?:\[(\d+)\])?')
_XML_ENTITIES = frozenset({b'amp', b'lt', b'gt', b'quot', b'apos'})
_NAMED_ENTITY = re.compile(rb'&([A-Za-z][A-Za-z0-9]*);')


@dataclass(frozen=True)
class XPointer:
    fragment: int                              # DocFragment N (1-based); 1 when omitted
    steps: tuple                               # below /body: (tag, 1-based index or None)
    text_index: Optional[int]                  # K in text()[K]; 1 for bare text(); None when no text step
    offset: Optional[int]                      # collapsed-whitespace offset after the final "."


def parse_xpointer(s: str) -> XPointer:
    """Parse a CREngine EPUB XPointer; raises ValueError for anything else."""
    m = _XPOINTER.match(s or '')
    if not m:
        raise ValueError(f'not a CREngine EPUB XPointer: {s!r}')
    fragment, path, text_index, text_offset, node_offset = m.groups()
    steps = tuple((tag, int(i) if i else None) for tag, i in _STEP.findall(path))
    if text_offset is not None:
        return XPointer(int(fragment or 1), steps, int(text_index or 1), int(text_offset))
    if node_offset is not None:
        # `tag[i].N` is CREngine shorthand for `tag[i]/text().N`.
        return XPointer(int(fragment or 1), steps, 1, int(node_offset))
    return XPointer(int(fragment or 1), steps, None, None)


def _local(el) -> str:
    return etree.QName(el).localname.lower()


def _is_element(node) -> bool:
    return isinstance(node.tag, str)


def _replace_html_entities(data: bytes) -> bytes:
    """Resolve HTML named entities, which XHTML readers accept without a DTD."""
    def repl(m):
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        char = html.entities.html5.get(name.decode('ascii') + ';')
        return char.encode('utf-8') if char else m.group(0)
    return _NAMED_ENTITY.sub(repl, data)


def parse_chapter(xhtml: bytes):
    """Parse chapter XHTML leniently; returns the root element."""
    data = _replace_html_entities(xhtml)
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        root = None
    if root is None:
        root = lxml_html.fromstring(data)
    return root


def _to_collapsed(text: str, raw: int) -> int:
    collapsed = 0
    for i in range(min(raw, len(text))):
        if not _is_run_continuation(text, i):
            collapsed += 1
    return collapsed + max(0, raw - len(text))


def _to_raw(text: str, collapsed_offset: int) -> int:
    collapsed = 0
    for i in range(len(text)):
        if _is_run_continuation(text, i):
            continue
        if collapsed == collapsed_offset:
            return i
        collapsed += 1
    return len(text)


def _is_run_continuation(text: str, i: int) -> bool:
    return i > 0 and text[i] in WHITESPACE and text[i - 1] in WHITESPACE


class _Text:
    __slots__ = ('parent', 'data', 'start', 'kept_index')

    def __init__(self, parent, data, start, kept_index):
        self.parent = parent
        self.data = data
        self.start = start
        self.kept_index = kept_index


class ChapterText:
    """A chapter's body text, with XPointer <-> offset mapping."""

    def __init__(self, xhtml: bytes):
        root = parse_chapter(xhtml)
        self._body = next(
            (el for el in root.iter() if _is_element(el) and _local(el) == 'body'), root)
        self._nodes = []    # every body text node, in document order
        self._kept = {}     # element -> its direct text children CREngine keeps
        self._starts = {}   # element -> offset of its first text
        self._pre = {}      # element -> whitespace is preserved inside it
        parts = []
        self._walk(self._body, parts, pre_above=False)
        self.text = ''.join(parts)
        self._node_starts = [n.start for n in self._nodes]

    def _walk(self, el, parts, pre_above):
        pre = pre_above or _local(el) in PRE_TAGS
        self._pre[el] = pre
        self._starts[el] = self._end()
        if _local(el) in SKIP_TAGS:
            return
        drops_leading_blank = _local(el) in BLOCK_TAGS and not pre
        children = [('text', el.text)] if el.text else []
        for child in el:
            children.append(('node', child))
            if child.tail:
                children.append(('text', child.tail))
        kept = []
        for i, (kind, value) in enumerate(children):
            if kind == 'node':
                if _is_element(value):
                    self._walk(value, parts, pre)
                continue
            keep = not (drops_leading_blank and i == 0 and value.strip() == '')
            node = _Text(el, value, self._end(), len(kept) + 1 if keep else None)
            if keep:
                kept.append(node)
            self._nodes.append(node)
            parts.append(value)
        self._kept[el] = kept

    def _end(self) -> int:
        if not self._nodes:
            return 0
        last = self._nodes[-1]
        return last.start + len(last.data)

    def offset_of(self, xp: XPointer) -> Optional[int]:
        """Offset into .text that xp points at, or None if its path doesn't resolve."""
        el = self._body
        for tag, index in xp.steps:
            same = [c for c in el if _is_element(c) and _local(c) == tag]
            i = index or 1
            if i > len(same):
                return None
            el = same[i - 1]
        if xp.text_index is None:
            return self._starts.get(el)
        kept = self._kept.get(el, [])
        if xp.text_index > len(kept):
            return None
        node = kept[xp.text_index - 1]
        offset = xp.offset or 0
        raw = min(offset, len(node.data)) if self._pre[el] else _to_raw(node.data, offset)
        return node.start + raw

    def xpointer_at(self, fragment: int, offset: int) -> str:
        """The XPointer CREngine would give the character at offset."""
        if not self._nodes:
            return f'/body/DocFragment[{fragment}]/body'
        # The text node holding the character at offset.
        node = self._nodes[max(0, bisect.bisect_right(self._node_starts, offset) - 1)]
        raw = min(max(offset - node.start, 0), len(node.data))
        base = self._path(fragment, node.parent)
        if node.kept_index is None:
            return base
        collapsed = raw if self._pre[node.parent] else _to_collapsed(node.data, raw)
        if len(self._kept[node.parent]) <= 1:
            return f'{base}/text().{collapsed}'
        return f'{base}/text()[{node.kept_index}].{collapsed}'

    def _path(self, fragment: int, el) -> str:
        steps = []
        while el is not None and el is not self._body:
            parent = el.getparent()
            tag = _local(el)
            same = [c for c in parent if _is_element(c) and _local(c) == tag]
            steps.append(tag if len(same) == 1 else f'{tag}[{same.index(el) + 1}]')
            el = parent
        return f'/body/DocFragment[{fragment}]/body' + ''.join('/' + s for s in reversed(steps))
