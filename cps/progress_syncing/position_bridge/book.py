# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""EPUB/KEPUB access for the position bridge: spine order, chapters, digests."""

import hashlib
import os
import posixpath
import threading
import zipfile
from typing import Optional

from lxml import etree

from .kepub import SpanStream
from .xpointer import ChapterText

HTML_SUFFIXES = ('.xhtml', '.html', '.htm')

_digest_cache = {}
_digest_lock = threading.Lock()


class EpubBook:
    """An EPUB or KEPUB file: its spine and chapter files."""

    def __init__(self, path: str):
        self.path = path
        self._parsed = {}
        with zipfile.ZipFile(path) as z:
            self._names = set(z.namelist())
            container = etree.fromstring(z.read('META-INF/container.xml'))
            opf_path = container.find('.//{*}rootfile').get('full-path')
            opf = etree.fromstring(z.read(opf_path))
        self._opf_dir = posixpath.dirname(opf_path)
        manifest = {
            item.get('id'): item.get('href')
            for item in opf.iterfind('.//{*}manifest/{*}item')
        }
        # DocFragment[i] in a CREngine XPointer is spine[i - 1].
        self.spine = [
            self._join(manifest[ref.get('idref')])
            for ref in opf.iterfind('.//{*}spine/{*}itemref')
            if ref.get('idref') in manifest
        ]

    def _join(self, href: str) -> str:
        return posixpath.normpath(posixpath.join(self._opf_dir, href.split('#')[0]))

    def chapter(self, zip_path: str) -> bytes:
        with zipfile.ZipFile(self.path) as z:
            return z.read(zip_path)

    def chapter_text(self, zip_path: str) -> ChapterText:
        """The chapter as ChapterText, parsed once per EpubBook."""
        key = ('text', zip_path)
        if key not in self._parsed:
            self._parsed[key] = ChapterText(self.chapter(zip_path))
        return self._parsed[key]

    def span_stream(self, zip_path: str) -> SpanStream:
        """The chapter as a KEPUB SpanStream, parsed once per EpubBook."""
        key = ('spans', zip_path)
        if key not in self._parsed:
            self._parsed[key] = SpanStream(self.chapter(zip_path))
        return self._parsed[key]

    def resolve(self, source: str) -> Optional[str]:
        """Zip path for source: exact, relative to the OPF, or a unique basename."""
        if not source:
            return None
        if source in self._names:
            return source
        relative = self._join(source)
        if relative in self._names:
            return relative
        base = posixpath.basename(source)
        matches = [n for n in self._names if posixpath.basename(n) == base]
        return matches[0] if len(matches) == 1 else None


def content_digest(path: str) -> str:
    """sha256 over the chapter files (name and bytes), ignoring the OPF and other files."""
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns, stat.st_size)
    with _digest_lock:
        if key in _digest_cache:
            return _digest_cache[key]
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as z:
        for name in sorted(n for n in z.namelist() if n.lower().endswith(HTML_SUFFIXES)):
            digest.update(name.encode('utf-8') + b'\0')
            digest.update(z.read(name))
            digest.update(b'\0')
    with _digest_lock:
        _digest_cache[key] = digest.hexdigest()
    return _digest_cache[key]
