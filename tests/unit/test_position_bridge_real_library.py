# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Round trips on real books: point POSITION_BRIDGE_REAL_LIBRARY at a directory of
<id>.epub / <id>.kepub pairs copied from a CWA library (never committed).
"""

import glob
import os

import pytest

from cps.progress_syncing.position_bridge.book import EpubBook
from cps.progress_syncing.position_bridge.kepub import SpanStream
from cps.progress_syncing.position_bridge.translate import kobo_to_xpointer, xpointer_to_kobo

REAL = os.environ.get("POSITION_BRIDGE_REAL_LIBRARY")
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not REAL, reason="POSITION_BRIDGE_REAL_LIBRARY not set"),
]


def pairs():
    for kepub in sorted(glob.glob(os.path.join(REAL or "", "*.kepub"))):
        yield os.path.basename(kepub)[: -len(".kepub")], kepub[: -len(".kepub")] + ".epub", kepub


@pytest.mark.parametrize("book_id,epub_path,kepub_path", list(pairs()) if REAL else [])
def test_real_library_roundtrip(book_id, epub_path, kepub_path):
    epub, kepub = EpubBook(epub_path), EpubBook(kepub_path)
    misses, checked = [], 0
    for path in kepub.spine:
        stream = SpanStream(kepub.chapter(path))
        ends = [s for _, s in stream.spans[1:]] + [len(stream.text)]
        for i, ((span, start), end) in enumerate(zip(stream.spans, ends)):
            if i % 10 or not stream.text[start:end].strip():
                continue  # empty or whitespace-only spans hold no position of their own
            checked += 1
            xp = kobo_to_xpointer(epub, kepub, path, span)
            back = xp and xpointer_to_kobo(epub, kepub, xp)
            if not back or back.span != span:
                context = stream.text[start:start + 60].replace("\n", " ")
                misses.append(f"{path}#{span} -> {xp} -> {back and back.span}: {context!r}")
    assert checked > 0
    assert misses == [], f"{book_id}: {len(misses)}/{checked} misses:\n" + "\n".join(misses[:10])


def test_readest_position_for_dont_be_evil():
    epub_path = os.path.join(REAL, "316.epub")
    if not os.path.exists(epub_path):
        pytest.skip("book 316 not in the sample")
    epub, kepub = EpubBook(epub_path), EpubBook(os.path.join(REAL, "316.kepub"))
    # The position Readest sent for Don't Be Evil on 2026-10-28.
    loc = xpointer_to_kobo(epub, kepub, "/body/DocFragment[6]/body/section/p[19]/text()[1].180")
    assert loc is not None
    assert os.path.basename(loc.source) == os.path.basename(epub.spine[5])
