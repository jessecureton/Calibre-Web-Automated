# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Per-user highlight store shared by the Kobo and Readest sides.

One `Highlight` row per passage carries both sides' identities (`kobo_id`,
`readest_key`), so an edit or delete from either side lands on the same row.
Callers own the transaction: these functions flush but never commit.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text,
)

from ... import logger
from ...ub import Base as AppBase
from ..models import _execute_sql_with_retry

log = logger.create()

SIDES = ("kobo", "readest", "import")

# Fields a side may set on a highlight. Identity (id, user, book, kobo_id,
# readest_key, origin), versions and the Readwise bookkeeping are never taken
# from the caller's dict.
CONTENT_FIELDS = (
    "text", "note", "color",
    "kobo_chapter", "kobo_start_path", "kobo_start_char",
    "kobo_end_path", "kobo_end_char", "kobo_chapter_progress",
    "xp0", "xp1",
)
# Identity a side may pass along with its id: recorded on the row whenever that
# side's id is (create, merge, or found by it), never counted as a content change.
SIDE_IDENTITY_FIELDS = {"readest": ("readest_datetime",)}


class Highlight(AppBase):
    """
    Fields:
        origin: side that first created the row: 'kobo', 'readest' or 'import'
        color: Readest colour name, or None
        kobo_id: the Kobo BookmarkID (a uuid string), unique per user; import rows carry it too
        kobo_*: location as Nickel sent it, paths stored verbatim
        xp0, xp1: start and end XPointers into the EPUB
        readest_key, readest_datetime: Readest's identity for the highlight; the key is
            unique per user
        readest_seen: Readest has listed `readest_key` since CWA recorded it (in a
            complete key list, or as one of its changes); only such keys can be
            deleted by their absence
        readest_held: a Readest deletion of this highlight is held (mass deletion)
            until its key reappears or an admin releases it
        readest_version: the highest `version` Readest is known to hold (it sent it,
            or acked it)
        readest_failed_version: the `version` Readest last reported it could not apply;
            not offered again until the version moves
        version: bumped on every applied change
        readwise_id, readwise_dirty: Readwise export bookkeeping
        readwise_error: short reason (e.g. "http 400") Readwise refused this version of the
            highlight; it is then not dirty and is left alone until its next change, which
            clears the error
    """
    __tablename__ = 'highlight'
    __table_args__ = (
        UniqueConstraint('user_id', 'kobo_id'),
        Index('ix_highlight_user_book', 'user_id', 'book_id'),
        Index('ux_highlight_user_readest_key', 'user_id', 'readest_key', unique=True,
              sqlite_where=text('readest_key IS NOT NULL')),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    book_id = Column(Integer, nullable=False)
    text = Column(String)
    note = Column(String)
    color = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    deleted_at = Column(DateTime)
    origin = Column(String)
    kobo_id = Column(String)
    kobo_chapter = Column(String)
    kobo_start_path = Column(String)
    kobo_start_char = Column(Integer)
    kobo_end_path = Column(String)
    kobo_end_char = Column(Integer)
    kobo_chapter_progress = Column(Float)
    xp0 = Column(String)
    xp1 = Column(String)
    readest_key = Column(String)
    readest_datetime = Column(String)
    readest_version = Column(Integer)
    readest_failed_version = Column(Integer)
    readest_seen = Column(Boolean, nullable=False, default=False, server_default='0')
    readest_held = Column(Boolean, nullable=False, default=False, server_default='0')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    readwise_id = Column(String)
    readwise_dirty = Column(Boolean, nullable=False, default=False, server_default='0')
    readwise_error = Column(String)

    def __repr__(self):
        return f'<Highlight user={self.user_id} book={self.book_id} id={self.id}>'


class HighlightBookState(AppBase):
    """
    Fields:
        kobo_etag_version: bumped on every change to a book's highlights; the Kobo's ETag source
        kobo_delivered_version: the kobo_etag_version last delivered to the user's Kobo
        readwise_backfill_done: the Readwise outbox has checked this book against what
            Readwise already holds, so it never does so again
    """
    __tablename__ = 'highlight_book_state'
    __table_args__ = (UniqueConstraint('user_id', 'book_id'),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    book_id = Column(Integer, nullable=False)
    kobo_etag_version = Column(Integer, nullable=False, default=0, server_default='0')
    kobo_delivered_version = Column(Integer, nullable=False, default=0, server_default='0')
    readwise_backfill_done = Column(Boolean, nullable=False, default=False, server_default='0')


class HighlightDevice(AppBase):
    """
    Fields:
        kobo_firmware: firmware version the user's Kobo last reported
        readest_device_id: the Readest device id last seen for the user
    """
    __tablename__ = 'highlight_device'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False, unique=True)
    kobo_firmware = Column(String)
    readest_device_id = Column(String)


class KoboOpaqueAnnotation(AppBase):
    """
    A Kobo annotation that isn't a highlight CWA can place: a dogear (bookmark),
    another type, or a highlight without a complete location. Kept verbatim and
    echoed back in the Kobo's annotation GET, so the device never loses it; never
    sent to Readest or Readwise.

    Fields:
        kobo_id: the annotation's Kobo id, unique per user
        raw: the annotation object exactly as the Kobo sent it, as JSON
    """
    __tablename__ = 'kobo_opaque_annotation'
    __table_args__ = (
        UniqueConstraint('user_id', 'kobo_id'),
        Index('ix_kobo_opaque_annotation_user_book', 'user_id', 'book_id'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    book_id = Column(Integer, nullable=False)
    kobo_id = Column(String, nullable=False)
    raw = Column(String, nullable=False)
    updated_at = Column(DateTime)
    deleted_at = Column(DateTime)


# Columns after the keys, as DDL. They build the CREATE and are re-offered to
# ALTER ... ADD COLUMN, which is how tables made by an earlier build upgrade.
_HIGHLIGHT_COLUMNS = (
    "text TEXT", "note TEXT", "color TEXT",
    "created_at TIMESTAMP", "updated_at TIMESTAMP", "deleted_at TIMESTAMP",
    "origin TEXT", "kobo_id TEXT",
    "kobo_chapter TEXT", "kobo_start_path TEXT", "kobo_start_char INTEGER",
    "kobo_end_path TEXT", "kobo_end_char INTEGER", "kobo_chapter_progress REAL",
    "xp0 TEXT", "xp1 TEXT", "readest_key TEXT", "readest_datetime TEXT",
    "readest_version INTEGER", "readest_failed_version INTEGER",
    "readest_seen BOOLEAN NOT NULL DEFAULT 0", "readest_held BOOLEAN NOT NULL DEFAULT 0",
    "version INTEGER NOT NULL DEFAULT 1",
    "readwise_id TEXT", "readwise_dirty BOOLEAN NOT NULL DEFAULT 0", "readwise_error TEXT",
)
_BOOK_STATE_COLUMNS = (
    "kobo_etag_version INTEGER NOT NULL DEFAULT 0",
    "kobo_delivered_version INTEGER NOT NULL DEFAULT 0",
    "readwise_backfill_done BOOLEAN NOT NULL DEFAULT 0",
)
_DEVICE_COLUMNS = ("kobo_firmware TEXT", "readest_device_id TEXT")
_OPAQUE_COLUMNS = ("raw TEXT NOT NULL DEFAULT '{}'", "updated_at TIMESTAMP", "deleted_at TIMESTAMP")


def ensure_highlight_tables(conn):
    """Create the highlight tables in app.db if they don't exist, adding any missing columns."""
    try:
        is_sqlalchemy = hasattr(conn, 'execute') and hasattr(conn.execute.__self__, 'dialect')

        def execute_sql(sql):
            return _execute_sql_with_retry(conn, sql, is_sqlalchemy)

        def table(name, key_columns, columns, constraints):
            body = ",\n                ".join(
                ["id INTEGER PRIMARY KEY AUTOINCREMENT", *key_columns, *columns, *constraints])
            execute_sql(f"CREATE TABLE IF NOT EXISTS {name} (\n                {body}\n            )")
            for column in columns:
                try:
                    execute_sql(f"ALTER TABLE {name} ADD COLUMN {column}")
                except Exception:
                    pass  # already there

        user_fk = "FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE"
        table("highlight", ["user_id INTEGER NOT NULL", "book_id INTEGER NOT NULL"],
              _HIGHLIGHT_COLUMNS, ["UNIQUE (user_id, kobo_id)", user_fk])
        execute_sql("CREATE INDEX IF NOT EXISTS ix_highlight_user_book ON highlight (user_id, book_id)")
        table("highlight_book_state", ["user_id INTEGER NOT NULL", "book_id INTEGER NOT NULL"],
              _BOOK_STATE_COLUMNS, ["UNIQUE (user_id, book_id)", user_fk])
        table("highlight_device", ["user_id INTEGER NOT NULL UNIQUE"],
              _DEVICE_COLUMNS, [user_fk])
        table("kobo_opaque_annotation",
              ["user_id INTEGER NOT NULL", "book_id INTEGER NOT NULL", "kobo_id TEXT NOT NULL"],
              _OPAQUE_COLUMNS, ["UNIQUE (user_id, kobo_id)", user_fk])
        execute_sql("CREATE INDEX IF NOT EXISTS ix_kobo_opaque_annotation_user_book "
                    "ON kobo_opaque_annotation (user_id, book_id)")
        conn.commit()
    except Exception as e:
        log.error(f"Could not create highlight tables: {e}")
        return
    try:
        # Its own step, so a table that somehow holds a duplicate key can't stop the
        # tables above from being made.
        _execute_sql_with_retry(
            conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_highlight_user_readest_key "
                  "ON highlight (user_id, readest_key) WHERE readest_key IS NOT NULL",
            hasattr(conn, 'execute') and hasattr(conn.execute.__self__, 'dialect'))
        conn.commit()
    except Exception as e:
        log.error(f"Could not create the unique Readest key index: {e}")


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; CWA stores them in UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _normalize(text: Optional[str]) -> str:
    return " ".join((text or "").casefold().split())


_FRAGMENT = re.compile(r"DocFragment\[(\d+)\]")


def _fragment(xpointer: Optional[str]) -> Optional[str]:
    match = _FRAGMENT.search(xpointer or "")
    return match.group(1) if match else None


def _side_column(side: str) -> str:
    if side not in SIDES:
        raise ValueError(f"unknown highlight side: {side!r}")
    return "readest_key" if side == "readest" else "kobo_id"


def _find_by_side_id(session, user_id, side, side_id) -> Optional[Highlight]:
    column = _side_column(side)
    return session.query(Highlight).filter(
        Highlight.user_id == user_id, getattr(Highlight, column) == side_id).first()


def _find_merge_candidate(session, user_id, book_id, side, fields) -> Optional[Highlight]:
    column = _side_column(side)
    text = _normalize(fields.get("text"))
    if not text:
        return None
    fragment = _fragment(fields.get("xp0"))
    rows = session.query(Highlight).filter(
        Highlight.user_id == user_id, Highlight.book_id == book_id,
        Highlight.deleted_at.is_(None), getattr(Highlight, column).is_(None),
    ).order_by(Highlight.id)
    for row in rows:
        if _normalize(row.text) == text and _fragment(row.xp0) == fragment:
            return row
    return None


def _bump_book(session, user_id, book_id):
    state = session.query(HighlightBookState).filter_by(user_id=user_id, book_id=book_id).first()
    if state is None:
        state = HighlightBookState(
            user_id=user_id, book_id=book_id, kobo_etag_version=0, kobo_delivered_version=0)
        session.add(state)
    state.kobo_etag_version = (state.kobo_etag_version or 0) + 1


def _changed(session, row: Highlight) -> None:
    """Record an applied change to an existing row."""
    row.version = (row.version or 1) + 1
    row.readwise_dirty = True
    row.readwise_error = None
    _bump_book(session, row.user_id, row.book_id)


def upsert_from_side(session, user_id: int, book_id: int, side: str, side_id: str,
                     fields: dict, now: datetime) -> Highlight:
    """
    Create or update the highlight a side reports.

    `side` is "kobo", "readest" or "import". `side_id` is the Kobo BookmarkID for
    kobo and import, and the Readest key for readest. `fields` holds any of
    CONTENT_FIELDS plus optional `created_at` and `updated_at`; `updated_at`
    defaults to `now`.

    The highlight is found, in order, by:
      1. its side id (also matching a deleted row, so identity is stable);
      2. merging: a live row in the same user and book that has no id for this
         side yet, whose text is equal after casefolding and collapsing
         whitespace, and whose xp0 sits in the same `DocFragment[N]` (both rows
         without xp0 count as the same fragment; one with and one without do not
         merge). The store reads no book files, so equal text in one fragment is
         what stands in for overlapping ranges: the fragment keeps identical
         sentences in different chapters apart. The side id is then recorded on
         the row, and the row's origin stays as it was;
      3. otherwise a new row, with origin set to `side`.

    An edit to an existing row applies only if `updated_at` is strictly newer than
    the row's and its content differs. It bumps `version` and the book's
    `kobo_etag_version` and marks the row `readwise_dirty`. Identical content
    changes nothing, not even `updated_at`, whatever its timestamp: an echo of
    what CWA delivered carries the receiver's apply time, and must not outrank a
    real edit made meanwhile. A newer, different edit to a deleted row restores
    it; identical content does not. Creates always count as a change.

    The side's identity fields (SIDE_IDENTITY_FIELDS, e.g. Readest's
    `readest_datetime`) are recorded whenever the row is matched, merged or
    created, regardless of timestamps, and never count as content.
    """
    column = _side_column(side)
    updated_at = _utc(fields.get("updated_at") or now)
    content = {name: fields[name] for name in CONTENT_FIELDS if name in fields}
    identity = {name: fields[name] for name in SIDE_IDENTITY_FIELDS.get(side, ()) if name in fields}

    row = _find_by_side_id(session, user_id, side, side_id)
    if row is None:
        row = _find_merge_candidate(session, user_id, book_id, side, fields)
        if row is not None:
            setattr(row, column, side_id)
    if row is None:
        row = Highlight(
            user_id=user_id, book_id=book_id, origin=side,
            created_at=_utc(fields.get("created_at") or now), updated_at=updated_at,
            version=1, readwise_dirty=True, **{column: side_id}, **content, **identity)
        session.add(row)
        _bump_book(session, user_id, book_id)
        session.flush()
        return row

    for name, value in identity.items():
        setattr(row, name, value)
    if row.updated_at is None or updated_at > _utc(row.updated_at):
        if any(getattr(row, name) != value for name, value in content.items()):
            for name, value in content.items():
                setattr(row, name, value)
            row.updated_at = updated_at
            row.deleted_at = None
            _changed(session, row)
    session.flush()
    return row


def delete_from_side(session, user_id: int, book_id: int, side: str, side_id: str,
                     now: datetime) -> Optional[Highlight]:
    """
    Mark the side's highlight deleted at `now`; None if the side has no such row.

    Bumps `version` and the book's `kobo_etag_version` and marks the row
    `readwise_dirty`. An already deleted row is returned untouched. `updated_at`
    moves to `now`, so a later report older than the delete cannot bring it back.
    """
    row = _find_by_side_id(session, user_id, side, side_id)
    if row is None:
        return None
    if row.deleted_at is None:
        row.deleted_at = _utc(now)
        row.updated_at = _utc(now)
        _changed(session, row)
        session.flush()
    return row


def live_for_book(session, user_id: int, book_id: int) -> list:
    """The user's highlights in a book that are not deleted, oldest first."""
    return session.query(Highlight).filter(
        Highlight.user_id == user_id, Highlight.book_id == book_id,
        Highlight.deleted_at.is_(None),
    ).order_by(Highlight.created_at, Highlight.id).all()
