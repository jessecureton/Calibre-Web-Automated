# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the position bridge's effects on Kobo and KOReader sync state."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import ub
from cps.progress_syncing.models import KOSyncProgress
from cps.progress_syncing.position_bridge.book import EpubBook
from cps.progress_syncing.position_bridge.service import (
    on_kobo_state,
    on_kosync_progress,
    record_kobo_download,
)
from cps.progress_syncing.position_bridge.state import PositionBridgeState
from cps.progress_syncing.position_bridge.xpointer import parse_xpointer

from .position_bridge_books import CH1, CH2, kepubify_like, write_book

USER, BOOK = 3, 1
NOW = datetime(2026, 10, 28, 18, 0, 0, tzinfo=timezone.utc)
CH2_PATH = "OEBPS/text/ch2.xhtml"


@pytest.fixture
def books(tmp_path):
    epub = write_book(tmp_path / "b.epub", {"ch1.xhtml": CH1, "ch2.xhtml": CH2})
    kepub = write_book(
        tmp_path / "b.kepub", {"ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(CH2)}
    )
    return epub, kepub


@pytest.fixture
def files(books):
    return lambda book_id: books if book_id == BOOK else (None, None)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="jcureton", email="j@example.test"))
    s.commit()
    yield s
    s.close()


def bookmark(span, source=CH2_PATH, percent=42.0, when="2026-10-28T17:59:00Z"):
    return {
        "LastModified": when,
        "ProgressPercent": percent,
        "ContentSourceProgressPercent": 10.0,
        "Location": {"Source": source, "Type": "KoboSpan", "Value": span},
    }


def kosync_rows(session):
    return session.query(KOSyncProgress).filter(KOSyncProgress.user_id == USER).all()


def kobo_bookmark(session):
    book_read = session.query(ub.ReadBook).filter(
        ub.ReadBook.user_id == USER, ub.ReadBook.book_id == BOOK).one_or_none()
    return book_read and book_read.kobo_reading_state


def resolve_text(epub_path, xpointer):
    epub = EpubBook(epub_path)
    xp = parse_xpointer(xpointer)
    chapter = epub.chapter_text(epub.spine[xp.fragment - 1])
    return chapter.text[chapter.offset_of(xp):]


@pytest.mark.unit
class TestKoboToKosync:
    def test_kobo_move_writes_kosync_progress(self, session, files, books):
        d = on_kobo_state(session, USER, BOOK, bookmark("kobo.2.1"), NOW, files)
        assert (d.forward, d.reason) == (True, "moved")
        [row] = kosync_rows(session)
        assert row.document == str(BOOK)
        assert row.device == "Kobo (via CWA)" and row.device_id == "cwa-position-bridge"
        assert row.percentage == 42.0
        assert resolve_text(books[0], row.progress).startswith("Everything was in confusion")
        state = session.query(PositionBridgeState).one()
        assert state.kobo_received == f"{CH2_PATH}#kobo.2.1"
        assert state.kosync_sent == row.progress

    def test_non_span_location_is_ignored(self, session, files):
        bm = bookmark("kobo.2.1")
        bm["Location"]["Type"] = "Unknown"
        assert on_kobo_state(session, USER, BOOK, bm, NOW, files).reason == "not-moved"
        assert kosync_rows(session) == []
        assert session.query(PositionBridgeState).count() == 0

    def test_missing_format_is_noop(self, session, books):
        only_epub = lambda book_id: (books[0], None)
        d = on_kobo_state(session, USER, BOOK, bookmark("kobo.2.1"), NOW, only_epub)
        assert (d.forward, d.reason) == (False, "no-files")
        assert kosync_rows(session) == []
        d = on_kosync_progress(session, USER, BOOK, "/body/DocFragment[2]/body/p[2]/text().0",
                               40.0, NOW, only_epub)
        assert d.reason == "no-files"
        assert kobo_bookmark(session) is None

    def test_stale_kobo_copy(self, session, files, tmp_path):
        other = write_book(tmp_path / "other.kepub", {"ch1.xhtml": kepubify_like(CH2)})
        record_kobo_download(session, USER, BOOK, other)
        d = on_kobo_state(session, USER, BOOK, bookmark("kobo.2.1"), NOW, files)
        assert (d.forward, d.reason) == (False, "stale-kobo-copy")
        assert kosync_rows(session) == []

    def test_matching_kobo_copy_is_fine(self, session, files, books):
        record_kobo_download(session, USER, BOOK, books[1])
        assert on_kobo_state(session, USER, BOOK, bookmark("kobo.2.1"), NOW, files).forward

    def test_late_kobo_report_recorded_not_forwarded(self, session, files):
        on_kosync_progress(session, USER, BOOK, "/body/DocFragment[2]/body/p[4]/text().0",
                           80.0, NOW, files)
        d = on_kobo_state(session, USER, BOOK,
                          bookmark("kobo.1.1", when="2026-10-28T17:50:00Z"), NOW, files)
        assert d.reason == "older-than-other-move"
        assert session.query(PositionBridgeState).one().kobo_received == f"{CH2_PATH}#kobo.1.1"
        assert kosync_rows(session) == []


@pytest.mark.unit
class TestKosyncToKobo:
    XP = "/body/DocFragment[2]/body/p[4]/text().4"  # inside "The wife had discovered..."

    def test_kosync_move_pushes_full_location(self, session, files):
        before = NOW - timedelta(hours=1)
        d = on_kosync_progress(session, USER, BOOK, self.XP, 80.0, NOW, files)
        assert (d.forward, d.reason) == (True, "moved")
        state = kobo_bookmark(session)
        bm = state.current_bookmark
        assert (bm.location_source, bm.location_type, bm.location_value) == \
            (CH2_PATH, "KoboSpan", "kobo.4.1")
        assert bm.progress_percent == 80.0
        assert 0 < bm.content_source_progress_percent < 100
        assert state.last_modified == state.priority_timestamp
        assert state.last_modified.replace(tzinfo=timezone.utc) > before
        assert session.query(PositionBridgeState).one().kobo_sent == f"{CH2_PATH}#kobo.4.1"

    def test_round_trip_is_quiet(self, session, files):
        on_kobo_state(session, USER, BOOK, bookmark("kobo.2.1"), NOW, files)
        [row] = kosync_rows(session)
        d = on_kosync_progress(session, USER, BOOK, row.progress, 42.0,
                               NOW + timedelta(minutes=1), files)
        assert d.reason == "not-moved"
        assert kobo_bookmark(session) is None

    def test_kobo_reporting_pushed_position_is_quiet(self, session, files):
        on_kosync_progress(session, USER, BOOK, self.XP, 80.0, NOW, files)
        d = on_kobo_state(session, USER, BOOK,
                          bookmark("kobo.4.1", when="2026-10-28T18:05:00Z"), NOW, files)
        assert d.reason == "not-moved"
        assert kosync_rows(session) == []

    def test_untranslatable_never_writes_percentage(self, session, files):
        d = on_kosync_progress(session, USER, BOOK, "/body/DocFragment[99]/body/p/text().0",
                               55.0, NOW, files)
        assert (d.forward, d.reason) == (False, "untranslatable")
        assert kobo_bookmark(session) is None
