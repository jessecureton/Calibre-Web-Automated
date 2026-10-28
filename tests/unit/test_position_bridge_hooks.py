# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for how the position bridge hooks into CWA's request handlers."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import ub
from cps.progress_syncing import position_bridge
from cps.progress_syncing.position_bridge import run_bridge
from cps.progress_syncing.protocols.kosync import update_book_read_status


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(position_bridge, "is_position_bridge_enabled", lambda: True)


@pytest.mark.unit
class TestRunBridge:
    def test_hook_errors_never_fail_request(self, enabled, monkeypatch, caplog):
        rolled_back = []
        monkeypatch.setattr(ub, "session", SimpleNamespace(rollback=lambda: rolled_back.append(1)))

        def boom(*args, **kwargs):
            raise RuntimeError("translation exploded")

        assert run_bridge(boom, 1, 2) is None
        assert rolled_back == [1]

    def test_calls_through_when_enabled(self, enabled):
        calls = []
        run_bridge(lambda *a, **k: calls.append((a, k)), 1, x=2)
        assert calls == [((1,), {"x": 2})]

    def test_disabled_bridge_is_inert(self, monkeypatch):
        monkeypatch.setattr(position_bridge, "is_position_bridge_enabled", lambda: False)
        calls = []
        run_bridge(lambda *a: calls.append(a), 1)
        assert calls == []


@pytest.mark.unit
class TestUpdateBookReadStatus:
    @pytest.fixture
    def session(self, monkeypatch):
        engine = create_engine("sqlite://")
        ub.Base.metadata.create_all(engine)
        s = sessionmaker(bind=engine)()
        s.add(ub.User(id=3, name="jcureton", email="j@example.test"))
        state = ub.KoboReadingState(user_id=3, book_id=1)
        state.current_bookmark = ub.KoboBookmark(progress_percent=10.0)
        state.statistics = ub.KoboStatistics()
        s.add(ub.ReadBook(user_id=3, book_id=1, read_status=ub.ReadBook.STATUS_UNREAD,
                          kobo_reading_state=state))
        s.commit()
        monkeypatch.setattr(ub, "session", s)
        yield s
        s.close()

    def _bookmark(self, session):
        return session.query(ub.KoboBookmark).one()

    def test_touch_kobo_bookmark_false(self, session):
        before = self._bookmark(session).last_modified
        update_book_read_status(SimpleNamespace(id=3), 1, 55.0, touch_kobo_bookmark=False)
        session.commit()
        bookmark = self._bookmark(session)
        assert bookmark.progress_percent == 10.0
        assert bookmark.last_modified == before
        assert session.query(ub.ReadBook).one().read_status == ub.ReadBook.STATUS_IN_PROGRESS

    def test_default_still_updates_bookmark(self, session):
        update_book_read_status(SimpleNamespace(id=3), 1, 55.0)
        session.commit()
        assert self._bookmark(session).progress_percent == 55.0

    def test_new_record_without_touch_has_no_percentage(self, session):
        update_book_read_status(SimpleNamespace(id=3), 2, 55.0, touch_kobo_bookmark=False)
        session.commit()
        read = session.query(ub.ReadBook).filter(ub.ReadBook.book_id == 2).one()
        assert read.kobo_reading_state.current_bookmark.progress_percent is None

    def test_touch_false_leaves_reading_state_alone(self, session):
        update_book_read_status(SimpleNamespace(id=3), 1, 55.0, touch_kobo_bookmark=False)
        session.commit()
        state = session.query(ub.KoboReadingState).one()
        before = (state.last_modified, state.priority_timestamp)
        update_book_read_status(SimpleNamespace(id=3), 1, 56.0, touch_kobo_bookmark=False)
        session.commit()
        session.refresh(state)
        assert (state.last_modified, state.priority_timestamp) == before
