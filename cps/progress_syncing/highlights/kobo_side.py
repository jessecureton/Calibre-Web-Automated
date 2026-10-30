# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
The Kobo side of highlight sync: what the Kobo's reading-services calls mean
for the highlight store, and what CWA answers them. No Flask here; the routes
are in cps/readingservices.py.

The Kobo treats a book's annotation GET as the whole truth: anything it holds
that the answer lacks is deleted on the device. So a GET answer includes every
live highlight the device may hold, and a highlight that can't be placed on the
device is withheld, never approximated.

Callers own the transaction: these functions flush but never commit.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from ... import logger
from .. import settings
from ..position_bridge.book import EpubBook
from ..position_bridge.service import FilesLookup, _existing_state, _stale, library_files
from .ranges import KoboRange, kobo_range_to_xpointers, xpointers_to_kobo_range
from .store import (
    Highlight,
    HighlightBookState,
    HighlightDevice,
    _find_merge_candidate,
    _utc,
    delete_from_side,
    live_for_book,
    upsert_from_side,
)

log = logger.create()

# No highlightColor has been seen from the Sage on 4.38, so colour stays out of
# both directions until a Kobo is seen sending it.
KOBO_SENDS_COLOR = False
# Readest colour names -> Kobo's highlight palette (reports/highlights-research/kobo.md §2.3).
# Kobo has no purple, so violet and custom hex colours are left to the device default.
READEST_TO_KOBO_COLOR = {
    "yellow": "#F6F3B3",
    "red": "#E8AFCF",
    "blue": "#B2E1E8",
    "green": "#C6E09E",
}
KOBO_TO_READEST_COLOR = {hex_.upper(): name for name, hex_ in READEST_TO_KOBO_COLOR.items()}

# The Kobo annotation types that are highlights; "dogear" (a bookmark) is not.
ANNOTATION_TYPES = ("highlight", "note")

_FIRMWARE = re.compile(r"Kobo Touch [0-9A-Za-z]+/(\d+(?:\.\d+)+)")
_FRACTION = re.compile(r"(\.\d{6})\d+")
_KOBO_FIELDS = ("kobo_chapter", "kobo_start_path", "kobo_start_char",
                "kobo_end_path", "kobo_end_char", "kobo_chapter_progress")


def highlight_routing_active() -> bool:
    """Whether CWA answers the Kobo's highlight calls: the setting is on, or was ever on."""
    return settings.is_highlight_sync_enabled() or settings.highlight_sync_ever_enabled()


def delivers_to(firmware: Optional[str]) -> bool:
    """Whether CWA sends new highlights to a Kobo on this firmware (setting on, firmware verified)."""
    return bool(firmware) and settings.is_highlight_sync_enabled() and \
        firmware in settings.tested_kobo_firmware()


def parse_kobo_firmware(user_agent: Optional[str]) -> Optional[str]:
    """The firmware version in a Kobo User-Agent (`... (Kobo Touch 0383/4.38.23852)`), or None."""
    match = _FIRMWARE.search(user_agent or "")
    return match.group(1) if match else None


def record_kobo_firmware(session, user_id: int, user_agent: Optional[str]) -> Optional[str]:
    """Store the firmware in the User-Agent on the user's HighlightDevice; returns it, or None."""
    firmware = parse_kobo_firmware(user_agent)
    if firmware is None:
        return None
    device = session.query(HighlightDevice).filter_by(user_id=user_id).first()
    if device is None:
        session.add(HighlightDevice(user_id=user_id, kobo_firmware=firmware))
    elif device.kobo_firmware != firmware:
        device.kobo_firmware = firmware
    else:
        return firmware
    session.flush()
    return firmware


def kobo_etag(version: int, pending: bool = False) -> str:
    """The annotation ETag CWA gives the Kobo; a pending one is never current, so the book is named again."""
    return f'W/"cwa-{version}-pending"' if pending else f'W/"cwa-{version}"'


# Served when the version itself could not be read.
FALLBACK_ETAG = 'W/"cwa-pending"'


def _book_version(session, user_id: int, book_id: int) -> int:
    state = session.query(HighlightBookState).filter_by(user_id=user_id, book_id=book_id).first()
    return (state.kobo_etag_version or 0) if state else 0


def _parse_kobo_time(value) -> Optional[datetime]:
    """clientLastModifiedUtc as an aware UTC datetime; None when empty or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(r"\1", value.strip())
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _utc(parsed)


def _kobo_time(value: Optional[datetime]) -> str:
    return _utc(value or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _location(entry: dict) -> dict:
    """The kobo_* fields of a Kobo annotation, paths verbatim."""
    span = ((entry.get("location") or {}).get("span")) or {}
    return {
        "kobo_chapter": span.get("chapterFilename"),
        "kobo_start_path": span.get("startPath"),
        "kobo_start_char": _int(span.get("startChar")),
        "kobo_end_path": span.get("endPath"),
        "kobo_end_char": _int(span.get("endChar")),
        "kobo_chapter_progress": _float(span.get("chapterProgress")),
    }


def _complete(location: dict) -> bool:
    """Whether a location has both ends (chapter progress is optional)."""
    return all(location.get(name) is not None for name in _KOBO_FIELDS[:5])


def _has_range(row) -> bool:
    return _complete({name: getattr(row, name) for name in _KOBO_FIELDS})


class _Books:
    """The library files of one book, opened once and only when a translation needs them."""

    def __init__(self, session, user_id: int, book_id: int, epub_path: Optional[str],
                 kepub_path: Optional[str]):
        self._session, self._user_id, self._book_id = session, user_id, book_id
        self.epub_path, self.kepub_path = epub_path, kepub_path
        self._opened = None

    def open(self):
        """(epub, kepub) EpubBooks, or None if a file is missing or the Kobo holds another copy."""
        if self._opened is None:
            self._opened = False
            if self.epub_path and self.kepub_path:
                state = _existing_state(self._session, self._user_id, self._book_id)
                try:
                    if not _stale(state, self.kepub_path):
                        self._opened = (EpubBook(self.epub_path), EpubBook(self.kepub_path))
                except Exception as e:  # an unreadable file: nothing can be translated
                    log.warning(f"[highlight-sync] user={self._user_id} book={self._book_id}: "
                                f"book files unusable ({type(e).__name__})")
        return self._opened or None


def _kobo_xpointers(books: _Books, location: dict, kobo_id: str):
    """(xp0, xp1), (None, None) if untranslatable, or None if the files can't be used."""
    opened = books.open()
    if opened is None:
        return None
    if not _complete(location):
        return None, None
    r = KoboRange(location["kobo_chapter"], location["kobo_start_path"], location["kobo_start_char"],
                  location["kobo_end_path"], location["kobo_end_char"],
                  location["kobo_chapter_progress"] or 0.0)
    try:
        xps = kobo_range_to_xpointers(opened[0], opened[1], r)
    except Exception as e:
        log.warning(f"[highlight-sync] kobo highlight {kobo_id}: range translation failed ({type(e).__name__})")
        xps = None
    return xps or (None, None)


def apply_kobo_patch(session, user_id: int, book_id: int, body: dict, now: datetime,
                     files: FilesLookup = library_files) -> None:
    """
    Store a Kobo annotations PATCH for one book.

    Each `updatedAnnotations` entry is upserted by its Kobo id, with its location
    kept verbatim and XPointers computed from it (None when untranslatable, or
    when the Kobo holds a different KEPUB than the library's). An empty or
    unreadable `clientLastModifiedUtc` is unknown: a create is dated `now`, and an
    edit to an existing highlight never wins. Entries of other types (a dogear)
    are skipped. Each `deletedAnnotationIds` entry is marked deleted.
    """
    body = body or {}
    updated = [e for e in (body.get("updatedAnnotations") or []) if isinstance(e, dict)]
    deleted = [i for i in (body.get("deletedAnnotationIds") or []) if isinstance(i, str) and i]
    books = None
    stored = untranslated = 0
    for entry in updated:
        kobo_id = entry.get("id")
        if not isinstance(kobo_id, str) or not kobo_id:
            log.warning(f"[highlight-sync] user={user_id} book={book_id}: skipped a Kobo annotation without an id")
            continue
        if entry.get("type", "highlight") not in ANNOTATION_TYPES:
            # A dogear (bookmark) is not a highlight; the store has no place for it.
            log.warning(f"[highlight-sync] user={user_id} book={book_id}: skipped Kobo annotation "
                        f"{kobo_id} of type {str(entry.get('type'))[:20]!r}")
            continue
        if books is None:
            epub_path, kepub_path = files(book_id)
            books = _Books(session, user_id, book_id, epub_path, kepub_path)
        location = _location(entry)
        note = entry.get("noteText") or None
        fields = {"text": entry.get("highlightedText"), "note": note, **location}
        if KOBO_SENDS_COLOR and isinstance(entry.get("highlightColor"), str):
            color = KOBO_TO_READEST_COLOR.get(entry["highlightColor"].upper())
            if color:
                fields["color"] = color

        existing = session.query(Highlight).filter(
            Highlight.user_id == user_id, Highlight.kobo_id == kobo_id).first()
        moved = existing is None or existing.xp0 is None or \
            any(getattr(existing, name) != location[name] for name in _KOBO_FIELDS)
        if moved:
            xps = _kobo_xpointers(books, location, kobo_id)
            fields["xp0"], fields["xp1"] = xps if xps else (None, None)
            untranslated += fields["xp0"] is None

        modified = _parse_kobo_time(entry.get("clientLastModifiedUtc"))
        if modified is None:
            anchor = existing or _find_merge_candidate(session, user_id, book_id, "kobo", fields)
            if anchor is not None and anchor.updated_at is not None:
                modified = _utc(anchor.updated_at)  # equal, so never newer than the stored row
        if modified is not None:
            fields["updated_at"] = modified

        row = upsert_from_side(session, user_id, book_id, "kobo", kobo_id, fields, now)
        if not _has_range(row) and _complete(location):
            # A merge, or an edit that didn't win: the device still holds this
            # highlight at this location, so a later GET must be able to serve it.
            for name in _KOBO_FIELDS:
                setattr(row, name, location[name])
            session.flush()
        stored += 1

    for kobo_id in deleted:
        delete_from_side(session, user_id, book_id, "kobo", kobo_id, now)
    if stored or deleted:
        log.info(f"[highlight-sync] user={user_id} book={book_id}: kobo patch "
                 f"updated={stored} deleted={len(deleted)} untranslated={untranslated}")


def _color(row) -> Optional[str]:
    if not KOBO_SENDS_COLOR or not row.color:
        return None
    return READEST_TO_KOBO_COLOR.get(row.color)


def _entry(row) -> dict:
    entry = {
        "type": "note" if row.note else "highlight",
        "id": row.kobo_id,
        "clientLastModifiedUtc": _kobo_time(row.updated_at or row.created_at),
        "highlightedText": row.text or "",
    }
    if row.note:
        entry["noteText"] = row.note
    color = _color(row)
    if color:
        entry["highlightColor"] = color
    entry["attachments"] = {}
    entry["location"] = {"span": {
        "chapterFilename": row.kobo_chapter,
        "chapterProgress": float(row.kobo_chapter_progress or 0.0),
        "startPath": row.kobo_start_path,
        "startChar": int(row.kobo_start_char),
        "endPath": row.kobo_end_path,
        "endChar": int(row.kobo_end_char),
    }}
    return entry


def _store_range(row, r: KoboRange) -> None:
    row.kobo_chapter, row.kobo_start_path, row.kobo_start_char = r.chapter, r.start_path, r.start_char
    row.kobo_end_path, row.kobo_end_char, row.kobo_chapter_progress = r.end_path, r.end_char, r.chapter_progress
    if not row.kobo_id:
        row.kobo_id = str(uuid.uuid4())


def kobo_annotation_set(session, user_id: int, book_id: int, kepub_path: Optional[str],
                        epub_path: Optional[str], translate_missing: bool = True) -> tuple:
    """
    The complete annotation GET answer for a book, and its ETag.

    Every live highlight with a stored Kobo range is served as stored. With
    `translate_missing`, a highlight without one (from Readest) gets a range from
    its XPointers, stored once with a new uuid4 `kobo_id` (no version bump: it is
    not a content change), or is withheld if it can't be translated or the Kobo
    holds a different KEPUB than the library's. Without it, nothing is written.

    The ETag is `W/"cwa-<kobo_etag_version>"`, or the pending form when a
    highlight was withheld for a reason that may pass (no translation attempted,
    files missing, other KEPUB on the device), so the book is named again later.
    """
    books = _Books(session, user_id, book_id, epub_path, kepub_path)
    entries, untranslatable, withheld = [], [], []
    for row in live_for_book(session, user_id, book_id):
        if _has_range(row) and row.kobo_id:
            entries.append(_entry(row))
            continue
        if not translate_missing:
            withheld.append(row.id)
            continue
        if _has_range(row):
            _store_range(row, KoboRange(row.kobo_chapter, row.kobo_start_path, row.kobo_start_char,
                                        row.kobo_end_path, row.kobo_end_char,
                                        row.kobo_chapter_progress or 0.0))
            entries.append(_entry(row))
            continue
        if not (row.xp0 and row.xp1):
            untranslatable.append(row.id)
            continue
        opened = books.open()
        if opened is None:
            withheld.append(row.id)
            continue
        try:
            r = xpointers_to_kobo_range(opened[0], opened[1], row.xp0, row.xp1)
        except Exception as e:
            log.warning(f"[highlight-sync] highlight {row.id}: range translation failed ({type(e).__name__})")
            r = None
        if r is None:
            untranslatable.append(row.id)
            continue
        _store_range(row, r)
        entries.append(_entry(row))
    session.flush()
    if untranslatable or withheld:
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: served {len(entries)} highlights to "
                    f"the Kobo; untranslatable={untranslatable} withheld={withheld}")
    version = _book_version(session, user_id, book_id)
    return {"annotations": entries, "nextPageOffsetToken": None}, kobo_etag(version, bool(withheld))


def kobo_changed_books(session, user_id: int, entries: list, firmware: Optional[str],
                       book_id_for: Optional[Callable[[str], Optional[int]]] = None) -> list:
    """
    The ContentIds of a checkforchanges batch whose highlights the Kobo should fetch.

    A CWA book is named when the ETag the device sent differs from
    `W/"cwa-<kobo_etag_version>"`. Nothing is named unless highlight sync is on and
    the firmware is one the admin verified. `book_id_for` maps a ContentId to a CWA
    book id, or None for a book CWA doesn't own.
    """
    if not delivers_to(firmware):
        return []
    lookup = book_id_for or _calibre_book_id
    named = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content_id = entry.get("ContentId")
        if not isinstance(content_id, str) or content_id in named:
            continue
        book_id = lookup(content_id)
        if book_id is None:
            continue
        if entry.get("etag") != kobo_etag(_book_version(session, user_id, book_id)):
            named.append(content_id)
    return named


def _calibre_book_id(content_id: str) -> Optional[int]:
    from ... import calibre_db
    book = calibre_db.get_book_by_uuid(content_id)
    return book.id if book else None
