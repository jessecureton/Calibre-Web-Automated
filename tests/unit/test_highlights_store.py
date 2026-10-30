# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the Kobo <-> Readest highlight store and its settings."""

import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import ub
from cps.progress_syncing.highlights.store import (
    Highlight,
    HighlightBookState,
    HighlightDevice,
    KoboOpaqueAnnotation,
    delete_from_side,
    ensure_highlight_tables,
    live_for_book,
    upsert_from_side,
)
from cps.progress_syncing.settings import (
    apply_highlight_sync_ever,
    highlight_sync_ever_enabled,
    is_highlight_sync_enabled,
    tested_kobo_firmware,
)

USER, BOOK = 3, 1
MIN = timedelta(minutes=1)
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
T1 = T0 + 5 * MIN
T2 = T0 + 10 * MIN

P0 = "/body/DocFragment[6]/body/section/p[2]/text().0"
P1 = "/body/DocFragment[6]/body/section/p[2]/text().14"
P0B = "/body/DocFragment[6]/body/section/p[2]/text().3"
P1B = "/body/DocFragment[6]/body/section/p[2]/text().20"
OTHER_CHAPTER = "/body/DocFragment[9]/body/section/p[2]/text().0"

EXPECTED = {
    "highlight": {
        "id", "user_id", "book_id", "text", "note", "color",
        "created_at", "updated_at", "deleted_at", "origin",
        "kobo_id", "kobo_chapter", "kobo_start_path", "kobo_start_char",
        "kobo_end_path", "kobo_end_char", "kobo_chapter_progress",
        "xp0", "xp1", "readest_key", "readest_datetime",
        "readest_version", "readest_failed_version", "version", "readwise_id", "readwise_dirty",
    },
    "highlight_book_state": {
        "id", "user_id", "book_id", "kobo_etag_version", "kobo_delivered_version",
    },
    "highlight_device": {"id", "user_id", "kobo_firmware", "readest_device_id"},
    "kobo_opaque_annotation": {"id", "user_id", "book_id", "kobo_id", "raw", "updated_at", "deleted_at"},
}


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _unique_column_sets(conn, table):
    unique = [row for row in conn.execute(f"PRAGMA index_list({table})") if row[2]]
    return [[c[2] for c in conn.execute(f"PRAGMA index_info({row[1]})")] for row in unique]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="jcureton", email="j@example.test"))
    s.commit()
    yield s
    s.close()


def etag(session, book=BOOK):
    state = session.query(HighlightBookState).filter_by(user_id=USER, book_id=book).one()
    return state.kobo_etag_version


@pytest.mark.unit
class TestTables:
    def test_ensure_tables_idempotent_and_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        ensure_highlight_tables(conn)
        ensure_highlight_tables(conn)
        for table, expected in EXPECTED.items():
            assert _columns(conn, table) == expected
        assert ["user_id", "kobo_id"] in _unique_column_sets(conn, "highlight")
        assert ["user_id", "book_id"] in _unique_column_sets(conn, "highlight_book_state")
        assert ["user_id"] in _unique_column_sets(conn, "highlight_device")
        assert ["user_id", "kobo_id"] in _unique_column_sets(conn, "kobo_opaque_annotation")

    def test_orm_models_match_the_ensured_columns(self, session):
        engine_columns = {
            table: {c.name for c in model.__table__.columns}
            for table, model in (
                ("highlight", Highlight),
                ("highlight_book_state", HighlightBookState),
                ("highlight_device", HighlightDevice),
                ("kobo_opaque_annotation", KoboOpaqueAnnotation),
            )
        }
        assert engine_columns == EXPECTED

    def test_adds_missing_columns_to_an_older_table(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        conn.execute("""CREATE TABLE highlight (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL, text TEXT, kobo_id TEXT,
            UNIQUE (user_id, kobo_id))""")
        conn.execute("INSERT INTO highlight (user_id, book_id, text) VALUES (3, 1, 'kept')")
        ensure_highlight_tables(conn)
        assert _columns(conn, "highlight") == EXPECTED["highlight"]
        row = conn.execute("SELECT text, version, readwise_dirty FROM highlight").fetchone()
        assert row == ("kept", 1, 0)

    def test_user_delete_cascades(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO user (id) VALUES (3)")
        ensure_highlight_tables(conn)
        conn.execute("INSERT INTO highlight (user_id, book_id) VALUES (3, 1)")
        conn.execute("INSERT INTO highlight_book_state (user_id, book_id) VALUES (3, 1)")
        conn.execute("INSERT INTO highlight_device (user_id) VALUES (3)")
        conn.execute("INSERT INTO kobo_opaque_annotation (user_id, book_id, kobo_id, raw) VALUES (3, 1, 'd', '{}')")
        conn.execute("DELETE FROM user WHERE id = 3")
        for table in EXPECTED:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    def test_user_has_the_per_user_highlight_columns(self):
        user = ub.User()
        assert user.readwise_token is None and user.highlight_sync_key_md5 is None
        assert {"readwise_token", "highlight_sync_key_md5"} <= {
            c.name for c in ub.User.__table__.columns
        }

    def test_migration_adds_the_user_columns_to_an_older_app_db(self):
        engine = create_engine("sqlite://")
        ub.Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE user DROP COLUMN readwise_token")
            conn.exec_driver_sql("ALTER TABLE user DROP COLUMN highlight_sync_key_md5")
        s = sessionmaker(bind=engine)()
        ub.migrate_user_table(engine, s)
        with engine.connect() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user)")}
        assert {"readwise_token", "highlight_sync_key_md5"} <= columns
        s.close()


@pytest.mark.unit
class TestUpsert:
    def test_upsert_creates_then_edit_wins_only_if_newer(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy families", "updated_at": T0}, T0)
        assert h.version == 1 and h.origin == "kobo" and h.kobo_id == "k-1"
        assert h.readwise_dirty is True
        upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                         {"note": "old", "updated_at": T0 - MIN}, T1)
        assert h.note is None and h.version == 1
        upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                         {"note": "new", "updated_at": T1}, T1)
        assert h.note == "new" and h.version == 2
        assert etag(session) == 2

    def test_equal_timestamp_does_not_apply(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy families", "updated_at": T0}, T0)
        upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"note": "tie", "updated_at": T0}, T1)
        assert h.note is None and h.version == 1

    def test_naive_stored_timestamps_compare_as_utc(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy families", "updated_at": T0}, T0)
        session.commit()
        session.expire_all()  # reloads naive datetimes from SQLite
        upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                         {"note": "old", "updated_at": T0 - MIN}, T1)
        assert h.note is None
        upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                         {"note": "new", "updated_at": T1}, T1)
        assert h.note == "new"

    def test_unchanged_content_with_newer_time_does_not_bump(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy families", "note": "n", "updated_at": T0}, T0)
        upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                         {"text": "Happy families", "note": "n", "updated_at": T1}, T1)
        assert h.version == 1 and etag(session) == 1

    def test_create_defaults_timestamps_to_now(self, session):
        h = upsert_from_side(session, USER, BOOK, "readest", "r-key", {"text": "x"}, T1)
        assert h.created_at == T1 and h.updated_at == T1 and h.origin == "readest"
        assert h.readest_key == "r-key" and h.kobo_id is None

    def test_import_side_uses_the_kobo_id(self, session):
        h = upsert_from_side(session, USER, BOOK, "import", "bm-1", {"text": "x"}, T0)
        assert h.origin == "import" and h.kobo_id == "bm-1" and h.readest_key is None

    def test_origin_is_set_on_create_only(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "x"}, T0)
        upsert_from_side(session, USER, BOOK, "readest", "r-1", {"note": "n", "updated_at": T1}, T1)
        assert h.origin == "kobo"

    def test_unknown_side_rejected(self, session):
        with pytest.raises(ValueError):
            upsert_from_side(session, USER, BOOK, "kindle", "x", {"text": "x"}, T0)

    def test_identity_fields_are_not_taken_from_fields(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "x", "kobo_id": "evil", "id": 99, "readwise_id": "7"}, T0)
        assert h.kobo_id == "k-1" and h.id != 99 and h.readwise_id is None

    def test_kobo_fields_stored_verbatim(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {
            "text": "x", "kobo_start_path": "span#kobo\\.1\\.1", "kobo_start_char": 3,
            "kobo_end_path": "span#kobo\\.1\\.2", "kobo_end_char": 9,
            "kobo_chapter": "ch1.xhtml", "kobo_chapter_progress": 0.25, "color": "yellow",
        }, T0)
        assert (h.kobo_start_path, h.kobo_end_char, h.kobo_chapter_progress, h.color) == (
            "span#kobo\\.1\\.1", 9, 0.25, "yellow")


@pytest.mark.unit
class TestMerge:
    def test_same_passage_from_both_sides_merges(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy  families", "xp0": P0, "xp1": P1, "updated_at": T0}, T0)
        b = upsert_from_side(session, USER, BOOK, "readest", "r-key",
                             {"text": "happy families", "xp0": P0B, "xp1": P1B, "updated_at": T0}, T1)
        assert a.id == b.id and b.kobo_id == "k-1" and b.readest_key == "r-key"
        assert session.query(Highlight).count() == 1

    def test_merge_applies_a_newer_edit(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy families", "xp0": P0, "xp1": P1, "updated_at": T0}, T0)
        upsert_from_side(session, USER, BOOK, "readest", "r-key",
                         {"text": "Happy families", "note": "mine", "xp0": P0B, "xp1": P1B,
                          "updated_at": T1}, T1)
        assert a.note == "mine" and a.version == 2 and a.origin == "kobo"

    def test_same_text_in_another_chapter_does_not_merge(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy families", "xp0": P0, "xp1": P1}, T0)
        b = upsert_from_side(session, USER, BOOK, "readest", "r-key",
                             {"text": "Happy families", "xp0": OTHER_CHAPTER}, T0)
        assert a.id != b.id

    def test_different_text_does_not_merge(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1",
                             {"text": "Happy families", "xp0": P0}, T0)
        b = upsert_from_side(session, USER, BOOK, "readest", "r-key",
                             {"text": "Unhappy families", "xp0": P0B}, T0)
        assert a.id != b.id

    def test_rows_without_xpointers_merge_on_text_alone(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "Happy families"}, T0)
        b = upsert_from_side(session, USER, BOOK, "readest", "r-key", {"text": "happy  FAMILIES"}, T0)
        assert a.id == b.id

    def test_one_row_lacking_xpointer_does_not_merge(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "Happy families"}, T0)
        b = upsert_from_side(session, USER, BOOK, "readest", "r-key",
                             {"text": "Happy families", "xp0": P0}, T0)
        assert a.id != b.id

    def test_never_merges_onto_a_row_that_already_has_that_sides_id(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "Same"}, T0)
        b = upsert_from_side(session, USER, BOOK, "kobo", "k-2", {"text": "Same"}, T0)
        assert a.id != b.id

    def test_never_merges_across_users_or_books(self, session):
        session.add(ub.User(id=4, name="other", email="o@example.test"))
        session.commit()
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "Same"}, T0)
        b = upsert_from_side(session, 4, BOOK, "readest", "r-1", {"text": "Same"}, T0)
        c = upsert_from_side(session, USER, 2, "readest", "r-2", {"text": "Same"}, T0)
        assert len({a.id, b.id, c.id}) == 3

    def test_deleted_rows_are_not_merge_candidates(self, session):
        a = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "Same"}, T0)
        delete_from_side(session, USER, BOOK, "kobo", "k-1", T1)
        b = upsert_from_side(session, USER, BOOK, "readest", "r-1", {"text": "Same"}, T2)
        assert a.id != b.id


@pytest.mark.unit
class TestDelete:
    def test_delete_sets_deleted_at_and_bumps_etag(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "x"}, T0)
        assert etag(session) == 1
        gone = delete_from_side(session, USER, BOOK, "kobo", "k-1", T1)
        assert gone is h and h.deleted_at == T1
        assert h.version == 2 and h.readwise_dirty is True
        assert etag(session) == 2
        assert live_for_book(session, USER, BOOK) == []

    def test_delete_unknown_returns_none(self, session):
        assert delete_from_side(session, USER, BOOK, "kobo", "nope", T1) is None
        assert session.query(HighlightBookState).count() == 0

    def test_delete_twice_is_a_no_op(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "x"}, T0)
        delete_from_side(session, USER, BOOK, "kobo", "k-1", T1)
        delete_from_side(session, USER, BOOK, "kobo", "k-1", T2)
        assert h.deleted_at == T1 and h.version == 2 and etag(session) == 2

    def test_stale_edit_does_not_resurrect_but_newer_one_does(self, session):
        h = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "x"}, T0)
        delete_from_side(session, USER, BOOK, "kobo", "k-1", T1)
        upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"note": "late", "updated_at": T0 + MIN}, T2)
        assert h.deleted_at is not None and h.note is None
        upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"note": "back", "updated_at": T2}, T2)
        assert h.deleted_at is None and h.note == "back"

    def test_delete_by_readest_key(self, session):
        h = upsert_from_side(session, USER, BOOK, "readest", "r-1", {"text": "x"}, T0)
        assert delete_from_side(session, USER, BOOK, "readest", "r-1", T1) is h
        assert h.deleted_at == T1

    def test_live_for_book_excludes_deleted_and_scopes(self, session):
        keep = upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "a"}, T0)
        upsert_from_side(session, USER, BOOK, "kobo", "k-2", {"text": "b"}, T0)
        upsert_from_side(session, USER, 2, "kobo", "k-3", {"text": "c"}, T0)
        delete_from_side(session, USER, BOOK, "kobo", "k-2", T1)
        assert live_for_book(session, USER, BOOK) == [keep]

    def test_etag_counts_per_user_and_book(self, session):
        upsert_from_side(session, USER, BOOK, "kobo", "k-1", {"text": "a"}, T0)
        upsert_from_side(session, USER, 2, "kobo", "k-2", {"text": "b"}, T0)
        assert etag(session, BOOK) == 1 and etag(session, 2) == 1


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


def _cwa_db_file(path, **columns):
    con = sqlite3.connect(path / "cwa.db")
    con.execute(f"CREATE TABLE cwa_settings ({', '.join(columns)})")
    con.execute(f"INSERT INTO cwa_settings VALUES ({', '.join('?' for _ in columns)})",
                list(columns.values()))
    con.commit()
    con.close()


@pytest.mark.unit
class TestSettings:
    @pytest.fixture(autouse=True)
    def cwa_db_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CWA_DB_PATH", str(tmp_path))

    def test_settings_default_off_and_firmware_empty(self, tmp_path, monkeypatch):
        _cwa_db_file(tmp_path, kobo_readest_highlight_sync=0,
                     kobo_readest_highlight_sync_ever=0, highlight_sync_kobo_firmware="")
        _fake_cwa_db(monkeypatch, error=RuntimeError("CWA_DB() must not be built"))
        assert is_highlight_sync_enabled() is False
        assert highlight_sync_ever_enabled() is False
        assert tested_kobo_firmware() == set()

    def test_enabled_reads_cwa_db_directly(self, tmp_path, monkeypatch):
        _cwa_db_file(tmp_path, kobo_readest_highlight_sync=1,
                     kobo_readest_highlight_sync_ever=1,
                     highlight_sync_kobo_firmware="4.38.23171, 4.40.1 ,,")
        _fake_cwa_db(monkeypatch, error=RuntimeError("CWA_DB() must not be built"))
        assert is_highlight_sync_enabled() is True
        assert highlight_sync_ever_enabled() is True
        assert tested_kobo_firmware() == {"4.38.23171", "4.40.1"}

    def test_ever_stays_on_after_the_setting_is_turned_off(self, tmp_path, monkeypatch):
        _cwa_db_file(tmp_path, kobo_readest_highlight_sync=0,
                     kobo_readest_highlight_sync_ever=1, highlight_sync_kobo_firmware="")
        assert is_highlight_sync_enabled() is False
        assert highlight_sync_ever_enabled() is True

    def test_falls_back_to_cwa_db_before_the_columns_exist(self, tmp_path, monkeypatch):
        _cwa_db_file(tmp_path, koreader_sync_enabled=1)
        _fake_cwa_db(monkeypatch, {
            "kobo_readest_highlight_sync": 1, "kobo_readest_highlight_sync_ever": 1,
            "highlight_sync_kobo_firmware": "4.1",
        })
        assert is_highlight_sync_enabled() is True
        assert highlight_sync_ever_enabled() is True
        assert tested_kobo_firmware() == {"4.1"}

    def test_ever_is_remembered_once_read_as_on(self, tmp_path, monkeypatch):
        _cwa_db_file(tmp_path, kobo_readest_highlight_sync=1,
                     kobo_readest_highlight_sync_ever=1, highlight_sync_kobo_firmware="")
        assert highlight_sync_ever_enabled() is True
        (tmp_path / "cwa.db").unlink()
        _fake_cwa_db(monkeypatch, error=RuntimeError("no db"))
        assert highlight_sync_ever_enabled() is True
        assert is_highlight_sync_enabled() is False  # only the monotonic flag is remembered

    def test_ever_off_is_not_remembered(self, tmp_path, monkeypatch):
        _cwa_db_file(tmp_path, kobo_readest_highlight_sync=0,
                     kobo_readest_highlight_sync_ever=0, highlight_sync_kobo_firmware="")
        assert highlight_sync_ever_enabled() is False
        (tmp_path / "cwa.db").unlink()
        _cwa_db_file(tmp_path, kobo_readest_highlight_sync=1,
                     kobo_readest_highlight_sync_ever=1, highlight_sync_kobo_firmware="")
        assert highlight_sync_ever_enabled() is True

    def test_fails_closed(self, monkeypatch):
        _fake_cwa_db(monkeypatch, error=RuntimeError("no db"))
        assert is_highlight_sync_enabled() is False
        assert highlight_sync_ever_enabled() is False
        assert tested_kobo_firmware() == set()

    def test_schema_defaults(self, monkeypatch):
        import os
        scripts = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
        monkeypatch.syspath_prepend(os.path.abspath(scripts))
        monkeypatch.delitem(sys.modules, "cwa_db", raising=False)
        from cwa_db import CWA_DB

        settings = CWA_DB().cwa_settings
        assert settings["kobo_readest_highlight_sync"] == 0
        assert settings["kobo_readest_highlight_sync_ever"] == 0
        assert settings["highlight_sync_kobo_firmware"] == ""


@pytest.mark.unit
class TestEverFlag:
    def test_ever_flag_set_when_saved_on(self):
        result = {"kobo_readest_highlight_sync": 1, "kobo_readest_highlight_sync_ever": 0}
        apply_highlight_sync_ever(result, {})
        assert result["kobo_readest_highlight_sync_ever"] == 1

    def test_ever_flag_survives_a_save_with_the_setting_off(self):
        result = {"kobo_readest_highlight_sync": 0, "kobo_readest_highlight_sync_ever": 0}
        apply_highlight_sync_ever(result, {"kobo_readest_highlight_sync_ever": 1})
        assert result["kobo_readest_highlight_sync_ever"] == 1

    def test_ever_flag_stays_off_when_never_enabled(self):
        result = {"kobo_readest_highlight_sync": 0, "kobo_readest_highlight_sync_ever": 0}
        apply_highlight_sync_ever(result, {"kobo_readest_highlight_sync_ever": 0})
        assert result["kobo_readest_highlight_sync_ever"] == 0
