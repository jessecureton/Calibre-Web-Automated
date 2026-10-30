# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Profile and admin settings for highlight sync.

A user keeps a Readwise token and a Readest password here. Readest's BookOrbit client
sends `X-Auth-Key: md5(password typed into Readest)` (BookOrbitForm.tsx hashes the
text as typed, nothing trimmed), so `User.highlight_sync_key_md5` is the lowercase hex
md5 of a generated password. The password itself is shown once and never stored.
"""
import hashlib
import re
import secrets
from typing import Optional

from sqlalchemy import func

from .. import settings
from ... import ub
from .store import Highlight, HighlightDevice, mark_kobo_import_done

_TOKEN_PREFIX = re.compile(r"^(?:token|bearer)\s+", re.IGNORECASE)


def section_visible() -> bool:
    """The highlight-sync settings show while sync is on, and after it was ever on."""
    return settings.is_highlight_sync_enabled() or settings.highlight_sync_ever_enabled()


def new_sync_password(user) -> str:
    """Give `user` a new Readest password, store only its md5, and return the password. The caller commits."""
    password = secrets.token_urlsafe(18)
    user.highlight_sync_key_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
    return password


def clean_readwise_token(raw: Optional[str]) -> str:
    """A pasted Readwise token without its whitespace or a `Token `/`Bearer ` prefix (empty if blank)."""
    return _TOKEN_PREFIX.sub("", (raw or "").strip()).strip()


def apply_readwise_form(user, form) -> None:
    """
    Update `user.readwise_token` from the profile form. A blank field keeps the token;
    the "remove" checkbox clears it (and wins over a typed token).
    """
    if form.get("remove_readwise_token") == "on":
        user.readwise_token = None
        return
    token = clean_readwise_token(form.get("readwise_token"))
    if token:
        user.readwise_token = token


def admin_summary(session, user_id: int) -> dict:
    """What the admin user-edit page shows about the user's devices and held deletions."""
    device = session.query(HighlightDevice).filter_by(user_id=user_id).first()
    held = session.query(func.count(Highlight.id)).filter(
        Highlight.user_id == user_id, Highlight.readest_held.is_(True)).scalar()
    return {
        "readest_device_id": device.readest_device_id if device and device.readest_device_id else None,
        "kobo_firmware": device.kobo_firmware if device and device.kobo_firmware else None,
        "kobo_import_done": bool(device and device.kobo_import_done),
        "held_count": held or 0,
    }


def reset_readest_device(session, user_id: int) -> bool:
    """Forget the user's recorded Readest device so the next Readest to connect becomes it. The caller commits."""
    device = session.query(HighlightDevice).filter_by(user_id=user_id).first()
    if device is None or not device.readest_device_id:
        return False
    device.readest_device_id = None
    return True


def apply_held_deletions(session, user_id: int) -> int:
    """Apply every Readest deletion held for the user; returns how many highlights were deleted. The caller commits."""
    from .bookorbit import release_held_deletions  # bookorbit -> kosync -> kobo -> web -> profile
    return release_held_deletions(session, user_id)


def mark_no_kobo_highlights(session, user_id: int) -> None:
    """The admin says the user's Kobo holds no highlights to import: open the Kobo gate. The caller commits."""
    mark_kobo_import_done(session, user_id)
