# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Per-user, per-book state for the Kobo <-> KOReader sync position bridge.

For each side (kobo, kosync) it remembers the last position received from that
side and the last position sent to it, which is what tells a real move apart
from a device merely reporting where it already was.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint

from ... import logger
from ...ub import Base as AppBase
from ..models import _execute_sql_with_retry

log = logger.create()


class PositionBridgeState(AppBase):
    """
    Fields:
        kobo_copy_digest: content digest of the KEPUB last served to this user's Kobo
        kobo_received: last Kobo position received, "<zip path>#<kobo span id>"
        kobo_received_at: when the reading behind kobo_received happened
        kobo_sent: last position pushed to the Kobo, same form as kobo_received
        kobo_sent_percent: the book percentage pushed with kobo_sent
        kobo_prev_sent: the push kobo_sent replaced, while the Kobo may still report landing on it
        kosync_received: last XPointer received over KOReader sync
        kosync_received_at: when kosync_received arrived
        kosync_sent: last XPointer written for KOReader sync clients
        kosync_sent_percent: the book percentage written with kosync_sent
    """
    __tablename__ = 'position_bridge_state'
    __table_args__ = (UniqueConstraint('user_id', 'book_id'),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    book_id = Column(Integer, nullable=False)
    kobo_copy_digest = Column(String)
    kobo_received = Column(String)
    kobo_received_at = Column(DateTime)
    kobo_sent = Column(String)
    kosync_received = Column(String)
    kosync_received_at = Column(DateTime)
    kosync_sent = Column(String)
    kobo_sent_percent = Column(Float)
    kosync_sent_percent = Column(Float)
    kobo_prev_sent = Column(String)

    def __repr__(self):
        return f'<PositionBridgeState user={self.user_id} book={self.book_id}>'


def ensure_position_bridge_table(conn):
    """Create the position_bridge_state table in app.db if it doesn't exist."""
    try:
        is_sqlalchemy = hasattr(conn, 'execute') and hasattr(conn.execute.__self__, 'dialect')

        def execute_sql(sql):
            return _execute_sql_with_retry(conn, sql, is_sqlalchemy)

        execute_sql("""
            CREATE TABLE IF NOT EXISTS position_bridge_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                kobo_copy_digest TEXT,
                kobo_received TEXT,
                kobo_received_at TIMESTAMP,
                kobo_sent TEXT,
                kosync_received TEXT,
                kosync_received_at TIMESTAMP,
                kosync_sent TEXT,
                kobo_sent_percent REAL,
                kosync_sent_percent REAL,
                kobo_prev_sent TEXT,
                UNIQUE (user_id, book_id),
                FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
            )
        """)
        # Tables created by earlier position-bridge builds predate these columns.
        for column in ("kosync_sent_percent REAL", "kobo_prev_sent TEXT"):
            try:
                execute_sql(f"ALTER TABLE position_bridge_state ADD COLUMN {column}")
            except Exception:
                pass  # already there
        conn.commit()
    except Exception as e:
        log.error(f"Could not create position_bridge_state table: {e}")


# A Kobo report older than the latest KOReader sync move by more than this is
# recorded but not forwarded. Tolerates modest Kobo clock drift.
DRIFT = timedelta(seconds=60)

# A reader opened at a pushed position reports the start of the page holding it,
# which can be a little earlier. A report at most this many characters before the
# position just sent, in the same chapter, is that landing rather than a move.
LANDING_CHARS = 4000


@dataclass(frozen=True)
class Decision:
    forward: bool
    reason: str
    # one of "moved", "not-moved", "older-than-other-move", "already-there",
    # "untranslatable", "stale-kobo-copy", "no-files"


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; CWA stores them in UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def decide_kobo(row: Optional[PositionBridgeState], key: str, read_at: datetime,
                translated: Optional[str], landing: bool = False) -> Decision:
    """Whether a Kobo position report ("<zip path>#<span>") is a move to forward."""
    if row is not None and key in (row.kobo_received, row.kobo_sent):
        return Decision(False, "not-moved")
    if row is not None and row.kosync_received_at is not None \
            and _utc(read_at) < _utc(row.kosync_received_at) - DRIFT:
        return Decision(False, "older-than-other-move")
    if landing:
        return Decision(False, "landing")
    if translated is None:
        return Decision(False, "untranslatable")
    if row is not None and translated in (row.kosync_received, row.kosync_sent):
        return Decision(False, "already-there")
    return Decision(True, "moved")


def decide_kosync(row: Optional[PositionBridgeState], xpointer: str,
                  translated_key: Optional[str], landing: bool = False) -> Decision:
    """Whether a KOReader sync position report is a move to forward to the Kobo."""
    if row is not None and xpointer in (row.kosync_received, row.kosync_sent):
        return Decision(False, "not-moved")
    if landing:
        return Decision(False, "landing")
    if translated_key is None:
        return Decision(False, "untranslatable")
    if row is not None and translated_key in (row.kobo_received, row.kobo_sent):
        return Decision(False, "already-there")
    return Decision(True, "moved")


def parse_kobo_time(value: Optional[str], now: datetime) -> datetime:
    """A Kobo timestamp ("2026-10-28T15:28:33Z", fractional or offset forms) in UTC, else now."""
    if not value:
        return now
    text = value.strip().replace('Z', '+00:00')
    # Kobo sends up to 7 fractional digits; fromisoformat takes at most 6.
    text = re.sub(r'(\.\d{6})\d+', r'\1', text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return now
    return _utc(parsed).astimezone(timezone.utc)
