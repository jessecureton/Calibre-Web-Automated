# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Applies the position bridge's decisions to CWA's Kobo and KOReader sync state.

- A Kobo move becomes a kosync_progress record that KOReader sync clients read.
- A KOReader sync move becomes a full KoboSpan location on the user's Kobo
  bookmark, with a bumped reading-state timestamp so the Kobo's next sync
  receives it.
Each function commits its own changes.
"""

import os
from datetime import datetime
from typing import Callable, Optional, Tuple

from ... import logger
from ... import ub
from ..models import KOSyncProgress
from .book import EpubBook, content_digest
from .state import Decision, PositionBridgeState, decide_kobo, decide_kosync, parse_kobo_time
from .translate import kobo_to_xpointer, xpointer_to_kobo

log = logger.create()

KOSYNC_DEVICE = "Kobo (via CWA)"
KOSYNC_DEVICE_ID = "cwa-position-bridge"

FilesLookup = Callable[[int], Tuple[Optional[str], Optional[str]]]


def library_files(book_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Paths of the book's library EPUB and KEPUB, each None if missing."""
    from ... import calibre_db, config
    book = calibre_db.get_book(book_id)
    if not book:
        return None, None

    def path_for(file_format):
        data = calibre_db.get_book_format(book_id, file_format)
        if not data:
            return None
        path = os.path.join(config.get_book_path(), book.path, f"{data.name}.{file_format.lower()}")
        return path if os.path.isfile(path) else None

    return path_for('EPUB'), path_for('KEPUB')


def _state(session, user_id: int, book_id: int) -> PositionBridgeState:
    row = session.query(PositionBridgeState).filter(
        PositionBridgeState.user_id == user_id,
        PositionBridgeState.book_id == book_id,
    ).one_or_none()
    if row is None:
        row = PositionBridgeState(user_id=user_id, book_id=book_id)
        session.add(row)
    return row


def _existing_state(session, user_id: int, book_id: int) -> Optional[PositionBridgeState]:
    return session.query(PositionBridgeState).filter(
        PositionBridgeState.user_id == user_id,
        PositionBridgeState.book_id == book_id,
    ).one_or_none()


def _note(user_id: int, book_id: int, side: str, decision: Decision) -> Decision:
    if decision.reason not in ("moved", "not-moved"):
        log.info(f"[position-bridge] user={user_id} book={book_id} side={side} "
                 f"reason={decision.reason}")
    return decision


def _stale(row: Optional[PositionBridgeState], kepub_path: str) -> bool:
    return bool(row and row.kobo_copy_digest and row.kobo_copy_digest != content_digest(kepub_path))


def on_kobo_state(session, user_id: int, book_id: int, bookmark: dict, now: datetime,
                  files: FilesLookup = library_files) -> Decision:
    """Handle a Kobo CurrentBookmark report; forward a real move to KOReader sync."""
    location = (bookmark or {}).get("Location") or {}
    if location.get("Type") != "KoboSpan" or not location.get("Value") or not location.get("Source"):
        return Decision(False, "not-moved")
    epub_path, kepub_path = files(book_id)
    if not epub_path or not kepub_path:
        return _note(user_id, book_id, "kobo", Decision(False, "no-files"))
    if _stale(_existing_state(session, user_id, book_id), kepub_path):
        return _note(user_id, book_id, "kobo", Decision(False, "stale-kobo-copy"))

    epub, kepub = EpubBook(epub_path), EpubBook(kepub_path)
    source = kepub.resolve(location["Source"]) or location["Source"]
    key = f"{source}#{location['Value']}"
    read_at = parse_kobo_time(bookmark.get("LastModified"), now)
    translated = kobo_to_xpointer(epub, kepub, source, location["Value"])
    row = _state(session, user_id, book_id)
    decision = decide_kobo(row, key, read_at, translated)
    if decision.reason != "not-moved":
        row.kobo_received = key
        row.kobo_received_at = read_at
    if decision.forward:
        progress = session.query(KOSyncProgress).filter(
            KOSyncProgress.user_id == user_id,
            KOSyncProgress.document == str(book_id),
        ).first()
        if progress is None:
            progress = KOSyncProgress(user_id=user_id, document=str(book_id))
            session.add(progress)
        progress.progress = translated
        progress.percentage = float(bookmark.get("ProgressPercent") or 0)
        progress.device = KOSYNC_DEVICE
        progress.device_id = KOSYNC_DEVICE_ID
        progress.timestamp = now
        row.kosync_sent = translated
    session.commit()
    return _note(user_id, book_id, "kobo", decision)


def _reading_state(session, user_id: int, book_id: int):
    book_read = session.query(ub.ReadBook).filter(
        ub.ReadBook.user_id == user_id,
        ub.ReadBook.book_id == book_id,
    ).one_or_none()
    if book_read is None:
        book_read = ub.ReadBook(user_id=user_id, book_id=book_id)
        session.add(book_read)
    if book_read.kobo_reading_state is None:
        state = ub.KoboReadingState(user_id=user_id, book_id=book_id)
        state.current_bookmark = ub.KoboBookmark()
        state.statistics = ub.KoboStatistics()
        book_read.kobo_reading_state = state
    return book_read.kobo_reading_state


def on_kosync_progress(session, user_id: int, book_id: int, xpointer: str, percentage: float,
                       now: datetime, files: FilesLookup = library_files) -> Decision:
    """Handle a KOReader sync progress report; forward a real move to the Kobo."""
    epub_path, kepub_path = files(book_id)
    if not epub_path or not kepub_path:
        return _note(user_id, book_id, "kosync", Decision(False, "no-files"))
    if _stale(_existing_state(session, user_id, book_id), kepub_path):
        return _note(user_id, book_id, "kosync", Decision(False, "stale-kobo-copy"))

    location = xpointer_to_kobo(EpubBook(epub_path), EpubBook(kepub_path), xpointer)
    row = _state(session, user_id, book_id)
    decision = decide_kosync(row, xpointer, location.key() if location else None)
    if decision.reason != "not-moved":
        row.kosync_received = xpointer
        row.kosync_received_at = now
    if decision.forward:
        state = _reading_state(session, user_id, book_id)
        bookmark = state.current_bookmark
        bookmark.location_source = location.source
        bookmark.location_type = "KoboSpan"
        bookmark.location_value = location.span
        bookmark.progress_percent = percentage
        bookmark.content_source_progress_percent = location.chapter_fraction * 100
        session.flush()
        # Nickel resumes at a pushed location only when LastModified moves and
        # PriorityTimestamp equals it.
        state.last_modified = now
        state.priority_timestamp = now
        row.kobo_sent = location.key()
    session.commit()
    return _note(user_id, book_id, "kosync", decision)


def record_kobo_download(session, user_id: int, book_id: int, kepub_path: str) -> None:
    """Remember which KEPUB content this user's Kobo was served."""
    row = _state(session, user_id, book_id)
    row.kobo_copy_digest = content_digest(kepub_path)
    session.commit()
