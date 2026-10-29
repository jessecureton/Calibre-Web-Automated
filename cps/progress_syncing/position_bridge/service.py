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
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from ... import logger
from ... import ub
from ..models import KOSyncProgress
from .book import EpubBook, content_digest
from .state import (
    LANDING_CHARS,
    Decision,
    PositionBridgeState,
    _utc,
    decide_kobo,
    decide_kosync,
    parse_kobo_time,
)
from .translate import KoboLocation, kobo_to_xpointer, xpointer_to_kobo
from .xpointer import parse_xpointer

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


def _lands_before(sent: Optional[int], reported: Optional[int]) -> bool:
    return sent is not None and reported is not None and 0 <= sent - reported <= LANDING_CHARS


def _kobo_landing(row: Optional[PositionBridgeState], kepub: EpubBook, source: str, span: str) -> bool:
    """Whether the Kobo reports the page start around the span we just pushed to it."""
    if row is None or not row.kobo_sent or row.kosync_received_at is None:
        return False
    if row.kobo_received_at is not None and _utc(row.kobo_received_at) > _utc(row.kosync_received_at):
        return False  # the Kobo has really moved since the push
    sent_source, _, sent_span = row.kobo_sent.partition('#')
    if sent_source != source:
        return False
    stream = kepub.span_stream(source)
    return _lands_before(stream.offset_of(sent_span), stream.offset_of(span))


def _kosync_landing(row: Optional[PositionBridgeState], epub: EpubBook, xpointer: str) -> bool:
    """Whether the KOReader sync client reports the page start around the position we just sent."""
    if row is None or not row.kosync_sent or row.kobo_received_at is None:
        return False
    if row.kosync_received_at is not None and _utc(row.kosync_received_at) > _utc(row.kobo_received_at):
        return False  # the client has really moved since the send
    try:
        sent, reported = parse_xpointer(row.kosync_sent), parse_xpointer(xpointer)
    except ValueError:
        return False
    if sent.fragment != reported.fragment or not 0 < sent.fragment <= len(epub.spine):
        return False
    chapter = epub.chapter_text(epub.spine[sent.fragment - 1])
    return _lands_before(chapter.offset_of(sent), chapter.offset_of(reported))


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
    landing = _kobo_landing(row, kepub, source, location["Value"])
    decision = decide_kobo(row, key, read_at, translated, landing)
    if decision.reason not in ("not-moved", "landing"):
        row.kobo_received = key
    if decision.forward or decision.reason == "older-than-other-move":
        row.kobo_received_at = read_at  # only real readings date a move
    if decision.forward:
        percentage = float(bookmark.get("ProgressPercent") or 0)
        _write_kosync(session, user_id, book_id, translated, percentage, now)
        row.kosync_sent = translated
        row.kosync_sent_percent = percentage
    elif _push_pending(row, decision, key):
        # HandleStateRequest just stored this report over a push the Kobo hasn't
        # received; put the push back so the Kobo's next sync still gets it.
        pushed = _sent_location(kepub, row.kobo_sent)
        if pushed is not None:
            _push_to_kobo(session, user_id, book_id, pushed, row.kobo_sent_percent)
            log.info(f"[position-bridge] user={user_id} book={book_id} side=kobo "
                     f"kept pushed position {row.kobo_sent} over {decision.reason} report")
    session.commit()
    return _note(user_id, book_id, "kobo", decision)


def _push_pending(row: PositionBridgeState, decision: Decision, key: str) -> bool:
    """Whether a declined Kobo report would overwrite a newer push the Kobo hasn't taken."""
    if not row.kobo_sent or key == row.kobo_sent or row.kosync_received_at is None:
        return False
    if row.kobo_received_at is not None and _utc(row.kobo_received_at) > _utc(row.kosync_received_at):
        return False  # the Kobo has really moved since the push
    return decision.reason == "older-than-other-move" or \
        (decision.reason == "not-moved" and key == row.kobo_received)


def _write_kosync(session, user_id: int, book_id: int, xpointer: str,
                  percentage: Optional[float], now: datetime) -> None:
    progress = session.query(KOSyncProgress).filter(
        KOSyncProgress.user_id == user_id,
        KOSyncProgress.document == str(book_id),
    ).first()
    if progress is None:
        progress = KOSyncProgress(user_id=user_id, document=str(book_id))
        session.add(progress)
    progress.progress = xpointer
    progress.percentage = percentage or 0.0
    progress.device = KOSYNC_DEVICE
    progress.device_id = KOSYNC_DEVICE_ID
    progress.timestamp = now


def _sent_location(kepub: EpubBook, sent: str) -> Optional[KoboLocation]:
    source, _, span = sent.partition('#')
    stream = kepub.span_stream(source)
    offset = stream.offset_of(span)
    if offset is None:
        return None
    return KoboLocation(source, span, offset / max(len(stream.text), 1))


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


def _push_to_kobo(session, user_id: int, book_id: int, location: KoboLocation,
                  percentage: Optional[float]) -> None:
    state = _reading_state(session, user_id, book_id)
    bookmark = state.current_bookmark
    bookmark.location_source = location.source
    bookmark.location_type = "KoboSpan"
    bookmark.location_value = location.span
    bookmark.progress_percent = percentage
    bookmark.content_source_progress_percent = location.chapter_fraction * 100
    session.flush()
    # Nickel resumes at a pushed location only when LastModified moves and
    # PriorityTimestamp equals it. Stamp it now, not with the request's start, so a
    # Kobo sync that ran meanwhile can't have moved its token past the push.
    pushed_at = datetime.now(timezone.utc)
    state.last_modified = pushed_at
    state.priority_timestamp = pushed_at


def on_kosync_progress(session, user_id: int, book_id: int, xpointer: str, percentage: float,
                       now: datetime, files: FilesLookup = library_files) -> Decision:
    """Handle a KOReader sync progress report; forward a real move to the Kobo."""
    epub_path, kepub_path = files(book_id)
    if not epub_path or not kepub_path:
        return _note(user_id, book_id, "kosync", Decision(False, "no-files"))
    if _stale(_existing_state(session, user_id, book_id), kepub_path):
        return _note(user_id, book_id, "kosync", Decision(False, "stale-kobo-copy"))

    epub = EpubBook(epub_path)
    location = xpointer_to_kobo(epub, EpubBook(kepub_path), xpointer)
    row = _state(session, user_id, book_id)
    landing = _kosync_landing(row, epub, xpointer)
    decision = decide_kosync(row, xpointer, location.key() if location else None, landing)
    if decision.reason not in ("not-moved", "landing"):
        row.kosync_received = xpointer
    if decision.forward:
        row.kosync_received_at = now  # only real readings date a move
    if decision.forward:
        _push_to_kobo(session, user_id, book_id, location, percentage)
        row.kobo_sent = location.key()
        row.kobo_sent_percent = percentage
    elif _send_pending(row, decision, xpointer):
        # The PUT handler just stored this report over a Kobo move the client hasn't
        # taken; put the move back so the client's next pull still gets it.
        _write_kosync(session, user_id, book_id, row.kosync_sent, row.kosync_sent_percent,
                      datetime.now(timezone.utc))
        log.info(f"[position-bridge] user={user_id} book={book_id} side=kosync "
                 f"kept sent position {row.kosync_sent} over {decision.reason} report")
    session.commit()
    return _note(user_id, book_id, "kosync", decision)


def _send_pending(row: PositionBridgeState, decision: Decision, xpointer: str) -> bool:
    """Whether a declined KOReader sync report would overwrite a newer Kobo move the client hasn't taken."""
    if not row.kosync_sent or xpointer == row.kosync_sent or row.kobo_received_at is None:
        return False
    if row.kosync_received_at is not None and _utc(row.kosync_received_at) > _utc(row.kobo_received_at):
        return False  # the client has really moved since the send
    return decision.reason == "not-moved" and xpointer == row.kosync_received


def record_kobo_download(session, user_id: int, book_id: int, kepub_path: str) -> None:
    """Remember which KEPUB content this user's Kobo was served."""
    row = _state(session, user_id, book_id)
    row.kobo_copy_digest = content_digest(kepub_path)
    session.commit()
