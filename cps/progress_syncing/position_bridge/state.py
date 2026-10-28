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

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

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
        kosync_received: last XPointer received over KOReader sync
        kosync_received_at: when kosync_received arrived
        kosync_sent: last XPointer written for KOReader sync clients
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
                UNIQUE (user_id, book_id),
                FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
            )
        """)
        conn.commit()
    except Exception as e:
        log.error(f"Could not create position_bridge_state table: {e}")
