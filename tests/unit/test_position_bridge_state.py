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
        "kosync_received", "kosync_received_at", "kosync_sent",
    }

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
