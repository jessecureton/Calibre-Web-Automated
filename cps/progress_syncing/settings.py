# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Helpers for KOReader sync feature flags."""

import os
import sqlite3
import sys
import time

# Access CWA_DB from scripts path (consistent with existing patterns)
sys.path.insert(1, '/app/calibre-web-automated/scripts/')


def _read_row_directly(columns):
    """Read the columns of the settings row with one query. CWA_DB() reconciles the
    whole cwa.db schema each time it is created, which is too heavy for every
    download/sync request."""
    db_file = _cwa_db_file()
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(f"SELECT {', '.join(columns)} FROM cwa_settings LIMIT 1").fetchone()
    finally:
        con.close()


def _read_setting_directly(columns=("koreader_sync_enabled",)):
    """Read the flags with one query; True only if all are set."""
    row = _read_row_directly(columns)
    return bool(row) and all(row)


def is_koreader_sync_enabled() -> bool:
    """Return True if KOReader sync is enabled in CWA settings."""
    try:
        return _read_setting_directly()
    except sqlite3.Error:
        pass  # missing file/column (e.g. before first CWA_DB init): use the full path below
    try:
        from cwa_db import CWA_DB
        settings = CWA_DB().cwa_settings
        return bool(settings.get('koreader_sync_enabled', 0))
    except Exception:
        # Fail closed to avoid unexpected DB writes when setting is missing
        return False


def is_position_bridge_enabled() -> bool:
    """Return True if Kobo <-> KOReader sync position translation is enabled.

    The bridge does nothing unless KOReader sync itself is enabled.
    """
    try:
        return _read_setting_directly(("koreader_sync_enabled", "kobo_kosync_position_bridge"))
    except sqlite3.Error:
        pass  # missing file/column (e.g. before CWA_DB adds the setting): use the full path below
    try:
        from cwa_db import CWA_DB
        settings = CWA_DB().cwa_settings
        return bool(settings.get('koreader_sync_enabled', 0)) and \
            bool(settings.get('kobo_kosync_position_bridge', 0))
    except Exception:
        return False


def is_highlight_sync_enabled() -> bool:
    """Return True if Kobo <-> Readest highlight sync is enabled in CWA settings."""
    try:
        return _read_setting_directly(("kobo_readest_highlight_sync",))
    except sqlite3.Error:
        pass  # missing file/column (e.g. before CWA_DB adds the setting): use the full path below
    try:
        from cwa_db import CWA_DB
        return bool(CWA_DB().cwa_settings.get('kobo_readest_highlight_sync', 0))
    except Exception:
        return False


# cwa.db paths whose `kobo_readest_highlight_sync_ever` has been read as 1. The flag
# never goes back to 0, so once seen it is never read again and can't fail.
_ever_seen = set()


class SettingsUnreadable(Exception):
    """A setting could not be read at all (as distinct from reading 0)."""


def _cwa_db_file() -> str:
    return os.path.join(os.environ.get("CWA_DB_PATH", "/config"), "cwa.db")


def highlight_sync_ever_enabled(session=None) -> bool:
    """Return True if highlight sync has ever been saved as on.

    It stays on after the setting is turned off, so that highlights already synced
    keep being served and are not mistaken for deletions. Once read as on it is
    remembered for the life of the process.

    If cwa.db can't be read at all (not a clean 0), it counts as on when app.db's
    `highlight_book_state` has any rows: those exist only once highlight sync ran.
    Raises SettingsUnreadable when app.db can't be checked either.
    """
    db_file = _cwa_db_file()
    if db_file in _ever_seen:
        return True
    try:
        ever = _read_highlight_sync_ever()
    except SettingsUnreadable as unreadable:
        try:
            ever = _app_db_has_highlight_state(session)
        except Exception as e:
            raise SettingsUnreadable(f"cwa.db and app.db both unreadable ({type(e).__name__})") from unreadable
    if ever:
        _ever_seen.add(db_file)
    return ever


def _read_highlight_sync_ever() -> bool:
    """The stored flag; raises SettingsUnreadable if neither cwa.db read works."""
    try:
        return _read_setting_directly(("kobo_readest_highlight_sync_ever",))
    except sqlite3.Error:
        pass  # missing file or column: the full path below
    try:
        from cwa_db import CWA_DB
        return bool(CWA_DB().cwa_settings.get('kobo_readest_highlight_sync_ever', 0))
    except Exception as e:
        raise SettingsUnreadable(type(e).__name__) from e


def _app_db_has_highlight_state(session=None) -> bool:
    """Whether app.db has any highlight_book_state row (False if the table doesn't exist)."""
    from sqlalchemy import text
    if session is None:
        from .. import ub
        session = ub.session
    try:
        return session.execute(text("SELECT 1 FROM highlight_book_state LIMIT 1")).first() is not None
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        if "no such table" in str(e):
            return False
        raise


def warm_highlight_sync_ever(session=None, attempts: int = 3, delay: float = 0.5) -> bool:
    """
    Read "ever on" at startup, retrying a failed read, so the process remembers it
    before the first Kobo request needs it. False if it still can't be read.
    """
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
        try:
            ever = _read_highlight_sync_ever()
        except SettingsUnreadable:
            continue
        if ever:
            _ever_seen.add(_cwa_db_file())
        return ever
    try:
        return highlight_sync_ever_enabled(session)  # cwa.db still unreadable: app.db decides
    except SettingsUnreadable:
        return False


def _parse_firmware_list(value) -> set:
    return {part.strip() for part in (value or "").split(",") if part.strip()}


def tested_kobo_firmware() -> set:
    """Kobo firmware versions the admin has verified for highlight delivery."""
    try:
        row = _read_row_directly(("highlight_sync_kobo_firmware",))
        return _parse_firmware_list(row[0] if row else "")
    except sqlite3.Error:
        pass
    try:
        from cwa_db import CWA_DB
        return _parse_firmware_list(CWA_DB().cwa_settings.get('highlight_sync_kobo_firmware', ''))
    except Exception:
        return set()


def apply_highlight_sync_ever(result: dict, current_settings: dict) -> None:
    """Set `kobo_readest_highlight_sync_ever` in a settings save about to be written.

    It becomes 1 whenever the save has highlight sync on and otherwise keeps the
    stored value; a plain checkbox would reset it to 0 on every save with the
    setting off.
    """
    ever = result.get('kobo_readest_highlight_sync') or current_settings.get(
        'kobo_readest_highlight_sync_ever', 0)
    result['kobo_readest_highlight_sync_ever'] = 1 if ever else 0
