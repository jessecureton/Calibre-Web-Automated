# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the position bridge's setting and state table."""

import sqlite3
import sys
import types

import pytest

from cps.progress_syncing.position_bridge.state import ensure_position_bridge_table
from cps.progress_syncing.settings import is_position_bridge_enabled


def _fake_cwa_db(monkeypatch, settings=None, error=None):
    class FakeCWADB:
        @property
        def cwa_settings(self):
            if error:
                raise error
            return settings

    module = types.ModuleType("cwa_db")
    module.CWA_DB = FakeCWADB
    monkeypatch.setitem(sys.modules, "cwa_db", module)


@pytest.mark.unit
class TestIsPositionBridgeEnabled:
    def test_bridge_requires_koreader_sync(self, monkeypatch):
        _fake_cwa_db(monkeypatch, {"koreader_sync_enabled": 0, "kobo_kosync_position_bridge": 1})
        assert is_position_bridge_enabled() is False

    def test_enabled_when_both_on(self, monkeypatch):
        _fake_cwa_db(monkeypatch, {"koreader_sync_enabled": 1, "kobo_kosync_position_bridge": 1})
        assert is_position_bridge_enabled() is True

    def test_off_by_default(self, monkeypatch):
        _fake_cwa_db(monkeypatch, {"koreader_sync_enabled": 1})
        assert is_position_bridge_enabled() is False

    def test_fails_closed(self, monkeypatch):
        _fake_cwa_db(monkeypatch, error=RuntimeError("no db"))
        assert is_position_bridge_enabled() is False


@pytest.mark.unit
class TestEnsurePositionBridgeTable:
    EXPECTED = {
        "id", "user_id", "book_id", "kobo_copy_digest",
        "kobo_received", "kobo_received_at", "kobo_sent",
        "kosync_received", "kosync_received_at", "kosync_sent", "kobo_sent_percent",
        "kosync_sent_percent",
    }

    def test_adds_columns_to_a_position_bridge_1_table(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        conn.execute("""CREATE TABLE position_bridge_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL, kobo_copy_digest TEXT, kobo_received TEXT,
            kobo_received_at TIMESTAMP, kobo_sent TEXT, kosync_received TEXT,
            kosync_received_at TIMESTAMP, kosync_sent TEXT, kobo_sent_percent REAL,
            UNIQUE (user_id, book_id))""")
        conn.execute("INSERT INTO position_bridge_state (user_id, book_id, kobo_sent) VALUES (3, 1, 'x')")
        ensure_position_bridge_table(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(position_bridge_state)")}
        assert columns == self.EXPECTED
        assert conn.execute("SELECT kobo_sent FROM position_bridge_state").fetchall() == [("x",)]

    def test_ensure_table_idempotent(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        ensure_position_bridge_table(conn)
        ensure_position_bridge_table(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(position_bridge_state)")}
        assert columns == self.EXPECTED

        unique = [
            row for row in conn.execute("PRAGMA index_list(position_bridge_state)") if row[2]
        ]
        unique_columns = [
            [c[2] for c in conn.execute(f"PRAGMA index_info({row[1]})")] for row in unique
        ]
        assert ["user_id", "book_id"] in unique_columns

    def test_user_delete_cascades(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO user (id) VALUES (3)")
        ensure_position_bridge_table(conn)
        conn.execute("INSERT INTO position_bridge_state (user_id, book_id) VALUES (3, 316)")
        conn.execute("DELETE FROM user WHERE id = 3")
        assert conn.execute("SELECT COUNT(*) FROM position_bridge_state").fetchone()[0] == 0


@pytest.mark.unit
def test_setting_defaults_off_in_cwa_db(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("CWA_DB_PATH", str(tmp_path))
    scripts = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
    monkeypatch.syspath_prepend(os.path.abspath(scripts))
    monkeypatch.delitem(sys.modules, "cwa_db", raising=False)
    from cwa_db import CWA_DB

    settings = CWA_DB().cwa_settings
    assert settings["kobo_kosync_position_bridge"] == 0


from datetime import datetime, timedelta, timezone

from cps.progress_syncing.position_bridge.state import (
    PositionBridgeState,
    decide_kobo,
    decide_kosync,
    parse_kobo_time,
)

NOW = datetime(2026, 10, 28, 18, 0, 0, tzinfo=timezone.utc)
K1 = "OEBPS/ch6.xhtml#kobo.19.1"
K2 = "OEBPS/ch6.xhtml#kobo.24.3"
K_EARLIER = "OEBPS/ch2.xhtml#kobo.4.1"
X1 = "/body/DocFragment[6]/body/section/p[19]/text().0"
X2 = "/body/DocFragment[6]/body/section/p[24]/text().11"


def row(**fields):
    return PositionBridgeState(user_id=3, book_id=316, **fields)


@pytest.mark.unit
class TestDecideKobo:
    def test_first_report_with_no_row(self):
        assert decide_kobo(None, K1, NOW, X1).forward is True

    def test_open_is_not_move(self):
        d = decide_kobo(row(kobo_sent=K1), K1, NOW, X1)
        assert (d.forward, d.reason) == (False, "not-moved")

    def test_repeat_of_last_received_is_not_move(self):
        d = decide_kobo(row(kobo_received=K1), K1, NOW, X1)
        assert (d.forward, d.reason) == (False, "not-moved")

    def test_late_kobo_report_not_forwarded(self):
        r = row(kosync_received=X2, kosync_received_at=NOW)
        d = decide_kobo(r, K1, NOW - timedelta(minutes=5), X1)
        assert (d.forward, d.reason) == (False, "older-than-other-move")

    def test_drift_tolerated(self):
        r = row(kosync_received=X2, kosync_received_at=NOW)
        d = decide_kobo(r, K1, NOW - timedelta(seconds=30), X1)
        assert (d.forward, d.reason) == (True, "moved")

    def test_naive_stored_time_is_utc(self):
        r = row(kosync_received=X2, kosync_received_at=NOW.replace(tzinfo=None))
        d = decide_kobo(r, K1, NOW - timedelta(minutes=5), X1)
        assert d.reason == "older-than-other-move"

    def test_untranslatable(self):
        d = decide_kobo(row(), K1, NOW, None)
        assert (d.forward, d.reason) == (False, "untranslatable")

    def test_already_there(self):
        d = decide_kobo(row(kosync_received=X1), K1, NOW, X1)
        assert (d.forward, d.reason) == (False, "already-there")

    def test_landing_is_not_move(self):
        d = decide_kobo(row(kobo_sent=K2), K1, NOW, X1, landing=True)
        assert (d.forward, d.reason) == (False, "landing")

    def test_not_moved_wins_over_landing(self):
        d = decide_kobo(row(kobo_sent=K1), K1, NOW, X1, landing=True)
        assert d.reason == "not-moved"

    def test_backward_move_forwarded(self):
        r = row(kobo_received=K2, kosync_sent=X2)
        d = decide_kobo(r, K_EARLIER, NOW, "/body/DocFragment[2]/body/p[4]/text().0")
        assert (d.forward, d.reason) == (True, "moved")


@pytest.mark.unit
class TestDecideKosync:
    def test_first_report_with_no_row(self):
        assert decide_kosync(None, X1, K1).forward is True

    def test_open_is_not_move(self):
        d = decide_kosync(row(kosync_sent=X1), X1, K1)
        assert (d.forward, d.reason) == (False, "not-moved")

    def test_untranslatable(self):
        d = decide_kosync(row(), X1, None)
        assert (d.forward, d.reason) == (False, "untranslatable")

    def test_already_there(self):
        d = decide_kosync(row(kobo_sent=K1), X1, K1)
        assert (d.forward, d.reason) == (False, "already-there")

    def test_moved(self):
        d = decide_kosync(row(kosync_received=X1, kobo_sent=K1), X2, K2)
        assert (d.forward, d.reason) == (True, "moved")

    def test_landing_is_not_move(self):
        d = decide_kosync(row(kosync_sent=X2), X1, K1, landing=True)
        assert (d.forward, d.reason) == (False, "landing")


@pytest.mark.unit
class TestParseKoboTime:
    def test_z_suffix(self):
        assert parse_kobo_time("2026-10-28T15:28:33Z", NOW) == datetime(2026, 10, 28, 15, 28, 33, tzinfo=timezone.utc)

    def test_fractional_seconds(self):
        got = parse_kobo_time("2026-10-28T15:28:33.4439690Z", NOW)
        assert got.replace(microsecond=0) == datetime(2026, 10, 28, 15, 28, 33, tzinfo=timezone.utc)

    def test_offset(self):
        got = parse_kobo_time("2026-10-28T10:28:33-05:00", NOW)
        assert got == datetime(2026, 10, 28, 15, 28, 33, tzinfo=timezone.utc)

    def test_kobo_time_falls_back_to_now(self):
        assert parse_kobo_time(None, NOW) == NOW
        assert parse_kobo_time("garbage", NOW) == NOW
