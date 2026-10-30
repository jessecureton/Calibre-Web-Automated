# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
The Readest side of highlight sync: CWA answers as a BookOrbit server, so
Readest's existing BookOrbit client exchanges highlights with it both ways.

The contract is Readest's client (readest-app/src/services/bookorbit), copied
here rather than BookOrbit's server:

- BookOrbitClient.ts: every call is under /api/v1/koreader with headers
  X-Auth-User and X-Auth-Key (md5 of the key the user typed). Every POST body
  also carries deviceId, deviceModel, pluginVersion and deviceTime. A non-2xx
  answer's JSON `message` is what Readest reports. `connect()` needs
  GET /users/auth to answer a JSON object; GET /plugin/version is optional and
  its `capabilities` turn on bookmark sync, which CWA doesn't offer. The ack's
  answer is parsed as JSON, so it must be a JSON body.
- notesPass.ts: a pass match-checks the open book and stops unless the answer's
  `matches` has an entry with the exact hash sent. The exchange then goes out in
  chunks of 50 changes: only the first chunk has `keysComplete: true` and the
  full key list; later chunks have no keys. `unmatched` and `results` must be
  arrays, and `more: true` makes Readest ask again with nothing new. Readest
  advances its watermark only after a clean pass, so any error repeats the pass.
- annotationExchange.ts: `keys` holds every live note that has an XPointer, so a
  known key absent from a complete list is a deletion. `changes` are notes
  created or edited since the watermark. Readest reads `toApply.add`, `.edit` and
  `.delete` (all three must be arrays) and acks each add and edit by serverId and
  version, and each delete by serverId. An add becomes a note whose `xpointer0`
  is the add's pos0 and whose identity datetime is the add's `datetime`, so
  Readest's key for it from then on is md5(datetime|pos0) with the pos0 CWA
  sent. A corrected pos0 in the ack is not written to the note
  (annotationExchange.ts:112-139), so CWA ignores it for the key. An edit is
  matched by `key` alone and changes only style, colour, text and note; a key
  Readest doesn't hold is acked `failed`.
- noteMapping.ts: key = md5(f"{datetime}|{pos0}"), datetimes are UTC
  "YYYY-MM-DD HH:MM:SS", colours are KOReader names (mapped to the Readest names
  the store keeps), and a note's empty fields are left out of its change.

What CWA adds on top:

- Off unless `kobo_readest_highlight_sync` is on: every route answers 404, the
  same as an unknown URL, so a server without the setting looks as it did.
- One Readest install per user: the first deviceId is recorded; any other gets
  409 with a message.
- Deletions: a highlight is deleted by its key's absence from a complete list
  only once Readest has listed that key (in a complete list, or as a change)
  since CWA recorded it. A key recorded from an add ack that Readest then
  doesn't list (the add landed on a Readest tombstone of the same position,
  which stays deleted: useBookOrbitNotesSync.ts:139-147) is dropped and the
  highlight left undelivered and logged, never deleted.
- Mass deletions are held: if one complete key list drops all of a book's
  live Readest-known highlights (at least 2), or more than half of them when
  there are more than 3, nothing is deleted, the counts are logged and the
  highlights are marked held. A single deletion always goes through. The exchange is still answered normally (no deletes, and no edits for
  keys Readest didn't list), so Readest's pass completes and doesn't repeat.
  The hold is sticky: held highlights are left out of later deletion counts
  until their keys reappear (which lifts the hold) or an admin releases them
  with `release_held_deletions`, which applies the deletion.
- Adds wait for Readest's changes: while a pass may still bring changes (a
  complete list naming keys CWA doesn't know beyond this request's changes, or
  a later chunk carrying changes), adds are withheld and `more` is true, so
  Readest sends one empty exchange after its last chunk, which carries them.
  Otherwise an add could duplicate a passage Readest's later chunk merges.
- An add whose ack was lost is recovered by its key (the minted datetime and the
  xp0 CWA sent), not by text, from Readest's key list or its echo. A highlight
  deleted between an offer and its ack still gets its key, so the delete is
  offered next.
- A change whose key is new but whose datetime belongs to exactly one live
  Readest-known highlight whose key has dropped out of the complete list is
  that highlight moved in Readest. It keeps its row and its Kobo id, and its
  stored Kobo range is cleared, so the Kobo is served the moved highlight under
  the same id. A change found by its key is never a move (the key is
  md5(datetime|pos0)): its XPointers are not taken, since Readest may spell
  the passage differently from the ones CWA computed from the Kobo's range,
  and the Kobo's range stays. So an end-only adjustment in Readest is not
  carried to the other side.
- A highlight Readest couldn't apply isn't offered again until it changes.
- Progress (GET/PUT /syncs/progress) runs kosync's own handlers, authenticated
  with these headers, so the position bridge sees Readest's progress.
"""

import hashlib
import hmac
import importlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import Blueprint, abort, g, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ... import constants, csrf, logger, ub
from .. import settings
from .store import (
    CONTENT_FIELDS, Highlight, HighlightDevice, _utc, delete_from_side, upsert_from_side,
)

# The protocols package re-exports the kosync blueprint under the module's name.
kosync_protocol = importlib.import_module("..protocols.kosync", __package__)

log = logger.create()

bookorbit = Blueprint("bookorbit", __name__, url_prefix="/api/v1/koreader")
if csrf:
    csrf.exempt(bookorbit)

PLUGIN_VERSION = "cwa-1"
DEVICE_CONFLICT = "Highlight sync supports one Readest device per user"
KO_DATETIME = "%Y-%m-%d %H:%M:%S"
MAX_HASH_LENGTH = 255

# noteMapping.ts KO_TO_READEST_COLOR; the store keeps Readest's names.
KO_TO_READEST_COLOR = {
    "yellow": "yellow", "red": "red", "green": "green", "blue": "blue", "purple": "violet",
    "orange": "#ff8800", "cyan": "#00bcd4", "olive": "#808000", "gray": "#9e9e9e",
}
READEST_TO_KO_COLOR = {readest: ko for ko, readest in KO_TO_READEST_COLOR.items()}

KOBO_RANGE_FIELDS = ("kobo_chapter", "kobo_start_path", "kobo_start_char",
                     "kobo_end_path", "kobo_end_char", "kobo_chapter_progress")


class BadRequest(Exception):
    pass


def bookorbit_key(datetime_: str, pos: str) -> str:
    """noteMapping.ts bookOrbitKey."""
    return hashlib.md5(f"{datetime_}|{pos}".encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ko_datetime(value: Optional[datetime]) -> Optional[str]:
    return _utc(value).strftime(KO_DATETIME) if value else None


def _parse_ko_datetime(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, KO_DATETIME).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _error(status: int, message: str):
    return jsonify({"message": message}), status


def _is_mass_deletion(missing: int, known: int) -> bool:
    """
    A pass's deletions of a book's live Readest-known highlights that are held: all
    of them when there are at least 2, or more than half of them when there are
    more than 3. A single deletion always goes through.
    """
    if missing <= 1:
        return False
    return (missing >= known and known >= 2) or (known > 3 and missing * 2 > known)


# --- request gate -----------------------------------------------------------------

def _authenticate() -> Optional[ub.User]:
    username = request.headers.get("X-Auth-User", "")
    key = request.headers.get("X-Auth-Key", "")
    if not username or not key:
        return None
    user = ub.session.query(ub.User).filter(func.lower(ub.User.name) == username.lower()).first()
    if user is None or not user.highlight_sync_key_md5:
        return None
    if not hmac.compare_digest(key.strip().lower().encode("utf-8"),
                               user.highlight_sync_key_md5.strip().lower().encode("utf-8")):
        return None
    return user


def _check_device(user_id: int, device_id):
    """None if `device_id` is the user's Readest device (recording it if it is the first), else a response."""
    if not isinstance(device_id, str) or not device_id:
        return _error(400, "deviceId is required")
    session = ub.session
    for _attempt in range(2):
        device = session.query(HighlightDevice).filter_by(user_id=user_id).first()
        if device is not None and device.readest_device_id:
            if device.readest_device_id == device_id:
                return None
            log.warning(f"[highlight-sync] user={user_id}: refused a second Readest device")
            return _error(409, DEVICE_CONFLICT)
        if device is None:
            device = HighlightDevice(user_id=user_id)
            session.add(device)
        device.readest_device_id = device_id
        try:
            session.commit()
            log.info(f"[highlight-sync] user={user_id}: recorded the Readest device")
            return None
        except IntegrityError:
            session.rollback()  # another request made the row first: read it again
    return _error(409, DEVICE_CONFLICT)


@bookorbit.before_request
def _gate():
    if not settings.is_highlight_sync_enabled():
        abort(404)
    user = _authenticate()
    if user is None:
        return _error(401, "Unauthorized")
    g.sync_user = user  # also what kosync's handlers use when progress is delegated
    if request.method == "POST":
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _error(400, "Invalid request body")
        g.bookorbit_body = body
        return _check_device(user.id, body.get("deviceId"))
    return None


def _books_list(body) -> list:
    books = body.get("books")
    if not isinstance(books, list) or not all(
            isinstance(b, dict) and isinstance(b.get("hash"), str) for b in books):
        raise BadRequest("books must be a list of objects with a hash")
    return books


def _book_id(hash_: str, cache: dict) -> Optional[int]:
    if hash_ not in cache:
        book_id = None
        if hash_ and len(hash_) <= MAX_HASH_LENGTH:
            book_id = kosync_protocol.get_book_by_checksum(hash_)[0]
        cache[hash_] = book_id
    return cache[hash_]


# --- discovery --------------------------------------------------------------------

@bookorbit.route("/users/auth", methods=["GET"])
def users_auth():
    return jsonify({"authorized": "OK"})


@bookorbit.route("/plugin/version", methods=["GET"])
def plugin_version():
    return jsonify({"pluginVersion": PLUGIN_VERSION, "serverVersion": constants.INSTALLED_VERSION,
                    "capabilities": []})


@bookorbit.route("/plugin/match-check", methods=["POST"])
def match_check():
    body = g.bookorbit_body
    hashes = body.get("hashes")
    if not isinstance(hashes, list):
        hashes = [b.get("hash") for b in body.get("books") or [] if isinstance(b, dict)]
    matches, seen, cache = [], set(), {}
    for hash_ in hashes:
        if not isinstance(hash_, str) or hash_ in seen:
            continue
        seen.add(hash_)
        book_id = _book_id(hash_, cache)
        if book_id is not None:
            matches.append({"hash": hash_, "bookId": book_id, "bookFileId": book_id})
    return jsonify({"matches": matches, "libraryVersion": 1})


# --- annotation exchange ----------------------------------------------------------

def _readest_color(ko_color) -> Optional[str]:
    return KO_TO_READEST_COLOR.get(ko_color) if isinstance(ko_color, str) else None


# What a highlight without a colour (the Kobo's, with colour off) is sent as: Readest
# shows a note without one in red. Its echo is taken as no colour (see _apply_change).
DEFAULT_KO_COLOR = "yellow"


def _ko_color(readest_color: Optional[str], default: str = "") -> str:
    # "" for no colour or one KOReader has no name for: an edit then leaves the
    # note's own colour in Readest (a custom one Readest never sent, say).
    if not readest_color:
        return default
    return READEST_TO_KO_COLOR.get(readest_color, "")


def _moved_row(session, user_id: int, book_id: int, datetime_: str, keys: set) -> Optional[Highlight]:
    """The one live Readest-known highlight with this identity datetime whose key left the list."""
    candidates = [row for row in session.query(Highlight).filter(
        Highlight.user_id == user_id, Highlight.book_id == book_id,
        Highlight.readest_datetime == datetime_, Highlight.readest_key.isnot(None),
        Highlight.deleted_at.is_(None)).all() if row.readest_key not in keys]
    return candidates[0] if len(candidates) == 1 else None


def _offered_row(session, user_id: int, book_id: int, key: str) -> Optional[Highlight]:
    """
    A keyless highlight CWA offered Readest under this key (its minted datetime and
    its xp0), whose ack never arrived. Matched by key, not text, and deleted rows
    included, so the recovery can't land on another copy of the passage or bring
    a deleted highlight back.
    """
    for row in session.query(Highlight).filter(
            Highlight.user_id == user_id, Highlight.book_id == book_id,
            Highlight.readest_key.is_(None), Highlight.readest_datetime.isnot(None),
            Highlight.xp0.isnot(None)).order_by(Highlight.id):
        if bookorbit_key(row.readest_datetime, row.xp0) == key:
            return row
    return None


def _change_key(ann) -> Optional[str]:
    if isinstance(ann, dict) and isinstance(ann.get("datetime"), str) and isinstance(ann.get("pos0"), str):
        return bookorbit_key(ann["datetime"], ann["pos0"])
    return None


def _apply_change(session, user_id: int, book_id: int, ann, keys: set, complete: bool,
                  now: datetime) -> bool:
    """Store one KoAnnotation from Readest; False if it can't be used."""
    if not isinstance(ann, dict):
        return False
    datetime_, pos0, pos1 = ann.get("datetime"), ann.get("pos0"), ann.get("pos1") or None
    created = _parse_ko_datetime(datetime_)
    if (ann.get("posFormat", "xpointer") != "xpointer" or created is None
            or not isinstance(pos0, str) or not pos0 or not (pos1 is None or isinstance(pos1, str))):
        return False
    updated = _parse_ko_datetime(ann.get("datetimeUpdated")) or created
    key = bookorbit_key(datetime_, pos0)
    note = ann.get("note")
    fields = {
        "note": note if isinstance(note, str) and note else None,
        "color": _readest_color(ann.get("color")),
        "xp0": pos0, "xp1": pos1, "readest_datetime": datetime_,
        "created_at": created, "updated_at": updated,
    }
    if isinstance(ann.get("text"), str) and ann["text"]:
        fields["text"] = ann["text"]

    row = session.query(Highlight).filter_by(user_id=user_id, readest_key=key).first()
    if row is not None and row.book_id != book_id:
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: a Readest key already belongs "
                    f"to highlight {row.id} in book {row.book_id}; change ignored")
        return False
    if row is None:
        row = _offered_row(session, user_id, book_id, key)
        if row is not None:
            log.info(f"[highlight-sync] user={user_id} book={book_id}: highlight {row.id} recovered "
                     f"from an add whose ack was lost")
            row.readest_key = key
            session.flush()
    moved = False
    if row is None and complete:
        row = _moved_row(session, user_id, book_id, datetime_, keys)
        if row is not None:
            log.info(f"[highlight-sync] user={user_id} book={book_id}: highlight {row.id} moved in Readest")
            row.readest_key = key
            moved = True
            session.flush()
    if moved:
        # Moved in Readest: the Kobo's stored range is stale. Keep kobo_id, so the
        # Kobo is served the moved highlight under the same id.
        fields.update({name: None for name in KOBO_RANGE_FIELDS})
    elif row is not None and row.xp0:
        # Found by its key, md5(datetime|pos0): not moved. Readest's XPointers may
        # spell the same passage differently from the ones CWA holds (computed from
        # the Kobo's range, for a merged highlight), so they are not taken, and the
        # Kobo's range stays.
        del fields["xp0"], fields["xp1"]
    if row is not None and row.color is None and fields["color"] == KO_TO_READEST_COLOR[DEFAULT_KO_COLOR]:
        fields["color"] = None  # the yellow CWA sends for "no colour", echoed back
    row = upsert_from_side(session, user_id, book_id, "readest", key, fields, now)
    row.readest_seen, row.readest_held = True, False
    if row.deleted_at is None and all(getattr(row, name) == value for name, value in fields.items()
                                      if name in CONTENT_FIELDS):
        row.readest_version = max(row.readest_version or 0, row.version)  # Readest holds this content
    return True


def _apply_key_list(session, user_id: int, book_id: int, keys: set, now: datetime) -> set:
    """
    Reconcile a book with Readest's complete key list. Returns the listed keys
    CWA holds no highlight for.

    - A keyless highlight offered under a listed key (lost ack) gets its key.
    - Listed keys are seen, and a hold on them is lifted.
    - A live key recorded from an add ack but not listed (the add landed on a
      Readest tombstone, which stays deleted) is marked failed: the key is
      dropped and the highlight left undelivered, not deleted.
    - Seen, unheld keys missing from the list are Readest's deletions, applied
      unless they are a mass deletion, which is held until the keys reappear or
      an admin releases it (`release_held_deletions`).
    """
    rows = session.query(Highlight).filter(
        Highlight.user_id == user_id, Highlight.book_id == book_id).order_by(Highlight.id).all()
    known_keys = {row.readest_key for row in rows if row.readest_key}
    for row in rows:
        if row.readest_key is None and row.readest_datetime and row.xp0:
            key = bookorbit_key(row.readest_datetime, row.xp0)
            if key in keys and key not in known_keys and session.query(Highlight.id).filter(
                    Highlight.user_id == user_id, Highlight.readest_key == key).first() is None:
                log.info(f"[highlight-sync] user={user_id} book={book_id}: highlight {row.id} recovered "
                         f"from an add whose ack was lost")
                row.readest_key = key
                known_keys.add(key)
    unknown = keys - known_keys

    failed, candidates = [], []
    for row in rows:
        if not row.readest_key:
            continue
        if row.readest_key in keys:
            row.readest_seen, row.readest_held = True, False
        elif row.deleted_at is None and not row.readest_seen:
            failed.append(row.id)
            row.readest_key, row.readest_version = None, None
            row.readest_failed_version = row.version
            continue
        if row.deleted_at is None and row.readest_seen and not row.readest_held:
            candidates.append(row)
    if failed:
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: Readest acked but doesn't list "
                    f"highlights {failed}; left undelivered")
    session.flush()

    missing = [row for row in candidates if row.readest_key not in keys]
    if not missing:
        return unknown
    if _is_mass_deletion(len(missing), len(candidates)):
        for row in missing:
            row.readest_held = True
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: held a Readest deletion of "
                    f"{len(missing)} of {len(candidates)} highlights")
        return unknown
    for row in missing:
        delete_from_side(session, user_id, book_id, "readest", row.readest_key, now)
        row.readest_version = max(row.readest_version or 0, row.version)
    log.info(f"[highlight-sync] user={user_id} book={book_id}: Readest deleted "
             f"{[row.id for row in missing]}")
    return unknown


def release_held_deletions(session, user_id: int, book_id: Optional[int] = None,
                           now: Optional[datetime] = None) -> int:
    """
    Apply the user's held Readest deletions (in one book, or all books): each held
    highlight still live is deleted as Readest asked, and every hold is cleared.
    Returns how many highlights were deleted. The caller commits.
    """
    now = now or _now()
    query = session.query(Highlight).filter(Highlight.user_id == user_id, Highlight.readest_held.is_(True))
    if book_id is not None:
        query = query.filter(Highlight.book_id == book_id)
    deleted = 0
    for row in query.all():
        row.readest_held = False
        if row.deleted_at is None and row.readest_key:
            delete_from_side(session, user_id, row.book_id, "readest", row.readest_key, now)
            row.readest_version = max(row.readest_version or 0, row.version)
            deleted += 1
    session.flush()
    log.info(f"[highlight-sync] user={user_id} book={book_id}: released {deleted} held Readest deletion(s)")
    return deleted


def _mint_datetime(session, row: Highlight, now: datetime) -> str:
    """A Readest identity datetime for a highlight Readest hasn't seen: its creation time, made unique."""
    moment = _utc(row.created_at or now).replace(microsecond=0)
    for _attempt in range(3600):
        datetime_ = moment.strftime(KO_DATETIME)
        taken = session.query(Highlight.id).filter(
            Highlight.user_id == row.user_id, Highlight.id != row.id,
            Highlight.readest_key == bookorbit_key(datetime_, row.xp0)).first()
        if taken is None:
            break
        moment += timedelta(seconds=1)
    row.readest_datetime = datetime_  # identity, not content: no version bump
    return datetime_


def _add_entry(row: Highlight, datetime_: str) -> dict:
    """types.ts ExchangeAddEntry."""
    return {
        "serverId": row.id, "version": row.version,
        "datetime": datetime_, "datetimeUpdated": _ko_datetime(row.updated_at),
        "drawer": "lighten", "color": _ko_color(row.color, DEFAULT_KO_COLOR),
        "text": row.text or "", "note": row.note or None, "chapter": None,
        "posFormat": "xpointer", "pos0": row.xp0, "pos1": row.xp1 or None, "pageno": None,
    }


def _edit_entry(row: Highlight, now: datetime) -> dict:
    """types.ts ExchangeEditEntry."""
    return {
        "serverId": row.id, "version": row.version, "key": row.readest_key,
        "datetime": row.readest_datetime, "datetimeUpdated": _ko_datetime(row.updated_at or now),
        "drawer": "lighten", "color": _ko_color(row.color),
        "text": row.text or "", "note": row.note or None, "chapter": None,
    }


def _to_apply(session, user_id: int, book_id: int, keys: Optional[set], offer_adds: bool,
              now: datetime) -> tuple:
    """
    (toApply, skippedNoPosition, adds_withheld) for a book. `keys` is None when
    the list wasn't complete. With a complete list, edits go only to keys Readest
    listed: one it doesn't hold (a held deletion) would only be acked failed.
    """
    add, edit, delete, skipped, withheld = [], [], [], 0, False
    rows = session.query(Highlight).filter(
        Highlight.user_id == user_id, Highlight.book_id == book_id,
        or_(Highlight.deleted_at.is_(None), Highlight.readest_key.isnot(None)),
    ).order_by(Highlight.created_at, Highlight.id).all()
    for row in rows:
        failed = row.readest_failed_version == row.version
        if row.readest_key is None:
            if row.deleted_at is not None:
                continue
            if not row.xp0:
                skipped += 1
            elif not failed and not offer_adds:
                withheld = True
            elif not failed:
                add.append(_add_entry(row, row.readest_datetime or _mint_datetime(session, row, now)))
            continue
        if (row.readest_version or 0) >= row.version or failed:
            continue
        if row.deleted_at is not None:
            delete.append({"serverId": row.id, "key": row.readest_key,
                           "datetime": row.readest_datetime})
        elif keys is None or row.readest_key in keys:
            edit.append(_edit_entry(row, now))
    return {"add": add, "edit": edit, "delete": delete}, skipped, withheld


def _exchange_book(session, user_id: int, book_id: int, req: dict, now: datetime) -> dict:
    """
    One ExchangeBookRequest. Adds are withheld, with `more: true`, while Readest
    may still be sending changes in this pass: a complete list naming keys CWA
    doesn't know that aren't among this request's changes (they come in later
    chunks), or any later chunk that carries changes. Readest's changes may merge
    with the very highlights an early add would duplicate. `more` makes Readest
    send one empty exchange after its last chunk (notesPass.ts:132-136), which
    then carries the adds.
    """
    keys_in, changes = req.get("keys") or [], req.get("changes") or []
    if not isinstance(keys_in, list) or not isinstance(changes, list):
        raise BadRequest("keys and changes must be lists")
    keys = {k.get("k") for k in keys_in if isinstance(k, dict) and isinstance(k.get("k"), str)}
    complete = req.get("keysComplete") is True
    unusable = sum(not _apply_change(session, user_id, book_id, ann, keys, complete, now)
                   for ann in changes)
    if unusable:
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: ignored {unusable} unusable "
                    f"Readest change(s)")
    if complete:
        unknown = _apply_key_list(session, user_id, book_id, keys, now)
        offer_adds = not (unknown - {_change_key(ann) for ann in changes})
    else:
        offer_adds = not changes
    to_apply, skipped, withheld = _to_apply(
        session, user_id, book_id, keys if complete else None, offer_adds, now)
    session.commit()
    if to_apply["add"] or to_apply["edit"] or to_apply["delete"]:
        log.info(f"[highlight-sync] user={user_id} book={book_id}: offered Readest "
                 f"add={[e['serverId'] for e in to_apply['add']]} "
                 f"edit={[e['serverId'] for e in to_apply['edit']]} "
                 f"delete={[e['serverId'] for e in to_apply['delete']]}")
    return {"hash": req["hash"], "bookId": book_id, "toApply": to_apply, "more": withheld,
            "skippedNoPosition": skipped}


@bookorbit.route("/plugin/annotations/exchange", methods=["POST"])
def annotations_exchange():
    session, user_id, now = ub.session, g.sync_user.id, _now()
    try:
        books = _books_list(g.bookorbit_body)
        results, unmatched, cache = [], [], {}
        for req in books:
            book_id = _book_id(req["hash"], cache)
            if book_id is None:
                unmatched.append(req["hash"])
                continue
            results.append(_exchange_book(session, user_id, book_id, req, now))
        return jsonify({"results": results, "unmatched": unmatched})
    except BadRequest as e:
        session.rollback()
        return _error(400, str(e))
    except SQLAlchemyError as e:
        session.rollback()
        log.error(f"[highlight-sync] user={user_id}: Readest exchange failed: {type(e).__name__}")
        return _error(500, "Database error")


def _ack_applied(session, user_id: int, book_id: int, entry) -> None:
    if not isinstance(entry, dict):
        return
    version = entry.get("version")
    row = session.query(Highlight).filter_by(
        id=entry.get("serverId"), user_id=user_id, book_id=book_id).first()
    if row is None or not isinstance(version, int):
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: ack for unknown highlight "
                    f"{entry.get('serverId')!r}")
        return
    if entry.get("status") != "applied":
        row.readest_failed_version = version
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: Readest could not apply "
                    f"highlight {row.id} v{version}; left undelivered")
        return
    if row.readest_key is None:
        # An add. Readest keys the note by the datetime and pos0 CWA sent; a
        # corrected pos0 in the ack never reaches the note (annotationExchange.ts).
        # A highlight deleted since the offer is keyed all the same, so the next
        # pass offers Readest the delete instead of the note coming back.
        if not row.xp0 or not row.readest_datetime:
            log.warning(f"[highlight-sync] user={user_id} book={book_id}: ack for highlight {row.id} "
                        f"that was never offered")
            return
        key = bookorbit_key(row.readest_datetime, row.xp0)
        clash = session.query(Highlight.id).filter(
            Highlight.user_id == user_id, Highlight.readest_key == key, Highlight.id != row.id).first()
        if clash is not None:
            row.readest_failed_version = version
            log.warning(f"[highlight-sync] user={user_id} book={book_id}: highlight {row.id}'s Readest "
                        f"key is held by highlight {clash[0]}; left undelivered")
            return
        row.readest_key, row.readest_seen = key, False  # seen once Readest lists it
        if entry.get("pos0") and entry["pos0"] != row.xp0:
            log.debug(f"[highlight-sync] highlight {row.id}: Readest reported a corrected position")
    row.readest_version = max(row.readest_version or 0, version)
    if row.readest_failed_version is not None and row.readest_failed_version <= version:
        row.readest_failed_version = None


def _ack_deleted(session, user_id: int, book_id: int, entry) -> None:
    if not isinstance(entry, dict):
        return
    row = session.query(Highlight).filter_by(
        id=entry.get("serverId"), user_id=user_id, book_id=book_id).first()
    if row is None:
        return
    if entry.get("status") != "applied":
        log.warning(f"[highlight-sync] user={user_id} book={book_id}: Readest could not delete "
                    f"highlight {row.id}")
        return
    if row.deleted_at is not None:
        row.readest_version = max(row.readest_version or 0, row.version)


@bookorbit.route("/plugin/annotations/exchange-ack", methods=["POST"])
def annotations_exchange_ack():
    session, user_id = ub.session, g.sync_user.id
    try:
        books, cache = _books_list(g.bookorbit_body), {}
        for book in books:
            book_id = _book_id(book["hash"], cache)
            if book_id is None:
                continue
            applied, deleted = book.get("applied") or [], book.get("deleted") or []
            if not isinstance(applied, list) or not isinstance(deleted, list):
                raise BadRequest("applied and deleted must be lists")
            for entry in applied:
                _ack_applied(session, user_id, book_id, entry)
            for entry in deleted:
                _ack_deleted(session, user_id, book_id, entry)
            session.commit()
        return jsonify({})
    except BadRequest as e:
        session.rollback()
        return _error(400, str(e))
    except SQLAlchemyError as e:
        session.rollback()
        log.error(f"[highlight-sync] user={user_id}: Readest ack failed: {type(e).__name__}")
        return _error(500, "Database error")


# --- progress ---------------------------------------------------------------------

@bookorbit.route("/syncs/progress/<document>", methods=["GET"])
def get_progress(document):
    return kosync_protocol.get_progress(document)


@bookorbit.route("/syncs/progress", methods=["PUT"])
def update_progress():
    return kosync_protocol.update_progress()
