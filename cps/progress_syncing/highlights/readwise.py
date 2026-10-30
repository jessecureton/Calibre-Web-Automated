# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Readwise outbox: copies every highlight CWA keeps to the user's Readwise account.

Readwise is an archive here, never a source. Per user with a `readwise_token`,
`push_dirty` sends the `readwise_dirty` highlights, in this order: deletes, edits,
creates.

- create: one `POST /api/v2/highlights/` per book (chunks of 100). The ids Readwise
  made are read from `modified_highlights`, or, when that cannot be matched to our
  highlights, from the book's export by `highlight_url`.
- edit (a highlight with a `readwise_id`): `PATCH /api/v2/highlights/<id>/`.
- delete: `DELETE /api/v2/highlights/<id>/`, and only for a highlight CWA created in
  Readwise, i.e. one with a recorded `readwise_id`.

Before the first push of a book, `backfill_skip_existing` lists what Readwise already
holds for it and marks local highlights with the same passage as done, so a book
already imported another way is not doubled. It runs once per book
(`HighlightBookState.readwise_backfill_done`).

The outbox never sleeps. On a 429 it records `Retry-After` and stops; runs before that
time do nothing. Any other failure leaves the highlight dirty, to be retried by the
next run. The token is only ever sent as a header; logs carry status codes, highlight
ids and counts, never the token or highlight text.

Environment: `READWISE_API_BASE` (default `https://readwise.io`).
"""

import os
import re
import time
from dataclasses import dataclass
from typing import NamedTuple, Optional

import requests

from ... import logger
from .store import Highlight, HighlightBookState, _normalize, _utc

log = logger.create()

DEFAULT_API_BASE = "https://readwise.io"
DEFAULT_RETRY_AFTER = 60
REQUEST_TIMEOUT = 30
CREATE_CHUNK = 100
MAX_PAGES = 50

# Readwise field limits (https://readwise.io/api_deets)
MAX_TEXT, MAX_NOTE, MAX_TITLE, MAX_AUTHOR = 8191, 8191, 511, 1024

# PATCH accepts these colours; CWA holds Readest colour names, so map the ones that exist.
_READWISE_COLORS = {"yellow": "yellow", "blue": "blue", "pink": "pink", "orange": "orange",
                    "green": "green", "purple": "purple", "violet": "purple"}

# user id -> epoch seconds before which that user's outbox does nothing (a 429's Retry-After)
_retry_not_before: dict = {}


class BookInfo(NamedTuple):
    title: str
    author: str
    uuid: str


@dataclass
class PushResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    backfill_skipped: int = 0
    quarantined: int = 0
    failed: int = 0
    rate_limited: bool = False


class _RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class _Stop(Exception):
    """Ends this user's run: the failure would repeat for every remaining request."""


def _now() -> float:
    return time.time()


def _api_base() -> str:
    return (os.environ.get("READWISE_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def _blocked(user_id) -> bool:
    return _now() < _retry_not_before.get(user_id, 0)


def _record_retry_after(user_id, seconds):
    _retry_not_before[user_id] = _now() + seconds
    log.warning(f"Readwise rate limit hit for user {user_id}: pausing the outbox for {seconds}s")


def _retry_after_seconds(response) -> int:
    try:
        value = int(float((response.headers or {}).get("Retry-After", DEFAULT_RETRY_AFTER)))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
    return value if value > 0 else DEFAULT_RETRY_AFTER


def _request(http, token, method, url, **kwargs):
    """One API call. Raises _RateLimited on 429 and _Stop when the request cannot be made."""
    try:
        response = getattr(http, method)(
            url, headers={"Authorization": f"Token {token}"}, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as e:
        # Not the exception text: it can carry the URL.
        log.warning(f"Readwise request failed ({type(e).__name__}); will retry")
        raise _Stop() from e
    if response.status_code == 429:
        raise _RateLimited(_retry_after_seconds(response))
    return response


def _failed(result, response, highlight_id=None):
    where = f" for highlight {highlight_id}" if highlight_id is not None else ""
    log.warning(f"Readwise answered {response.status_code}{where}; left for the next run")
    result.failed += 1
    if response.status_code in (401, 403) or response.status_code >= 500:
        raise _Stop()


# --- book info ----------------------------------------------------------------

def book_info(book_id) -> Optional[BookInfo]:
    """Title, authors (joined with ", ") and uuid of a library book, from the Calibre DB."""
    from ... import calibre_db
    return book_info_from(calibre_db, book_id)


def book_info_from(calibre, book_id) -> Optional[BookInfo]:
    book = calibre.get_book(book_id)
    if book is None:
        return None
    author = ", ".join(a.name.replace("|", ",") for a in (book.authors or []))
    return BookInfo(book.title or "", author, book.uuid or "")


# --- ordering -----------------------------------------------------------------

_DOCFRAGMENT = re.compile(r"DocFragment\[(\d+)\]")
_INDEX = re.compile(r"\[(\d+)\]")
_OFFSET = re.compile(r"\.(\d+)$")


def _position_key(row: Highlight):
    """Book order: DocFragment index, then the path's indexes and text offset; id breaks ties
    and orders highlights without an xp0 after those that have one."""
    xp0 = row.xp0 or ""
    fragment = _DOCFRAGMENT.search(xp0)
    if not fragment:
        return (1, 0, (), 0, row.id)
    rest = xp0[fragment.end():]
    offset = _OFFSET.search(rest)
    return (0, int(fragment.group(1)), tuple(int(n) for n in _INDEX.findall(rest)),
            int(offset.group(1)) if offset else 0, row.id)


def _locations(session, user_id, book_id) -> dict:
    """highlight id -> 1-based position among the book's live highlights."""
    rows = session.query(Highlight).filter(
        Highlight.user_id == user_id, Highlight.book_id == book_id,
        Highlight.deleted_at.is_(None)).all()
    return {row.id: n for n, row in enumerate(sorted(rows, key=_position_key), start=1)}


# --- payloads -----------------------------------------------------------------

def _highlight_url(info: BookInfo, row: Highlight) -> str:
    return f"cwa://{info.uuid}/highlight/{row.id}"


def _color(row: Highlight) -> Optional[str]:
    return _READWISE_COLORS.get((row.color or "").lower())


def _create_payload(info: BookInfo, row: Highlight, location: int) -> dict:
    payload = {
        "text": (row.text or "")[:MAX_TEXT],
        "title": info.title[:MAX_TITLE],
        "author": info.author[:MAX_AUTHOR],
        "source_type": "cwa",
        "category": "books",
        "note": (row.note or "")[:MAX_NOTE],
        "location_type": "order",
        "location": location,
        "highlight_url": _highlight_url(info, row),
    }
    if row.created_at is not None:
        payload["highlighted_at"] = _utc(row.created_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _color(row):
        payload["color"] = _color(row)
    return payload


def _patch_payload(row: Highlight) -> dict:
    payload = {"text": (row.text or "")[:MAX_TEXT], "note": (row.note or "")[:MAX_NOTE]}
    if _color(row):
        payload["color"] = _color(row)
    return payload


# --- reading Readwise ---------------------------------------------------------

def _json(response):
    try:
        return response.json()
    except ValueError:
        return None


def _export_highlights(http, token, book_ids):
    """Every live highlight in the export of the given Readwise book ids. None on a failed
    request (the caller treats the answer as unknown)."""
    found = []
    params = {"ids": ",".join(str(i) for i in book_ids)}
    for _ in range(MAX_PAGES):
        response = _request(http, token, "get", f"{_api_base()}/api/v2/export/", params=dict(params))
        body = _json(response) if response.status_code == 200 else None
        if not isinstance(body, dict):
            log.warning(f"Readwise export answered {response.status_code}")
            return None
        for book in body.get("results") or []:
            for item in book.get("highlights") or []:
                if not item.get("is_deleted"):
                    found.append(item)
        cursor = body.get("nextPageCursor")
        if not cursor:
            return found
        params["pageCursor"] = cursor
    log.warning("Readwise export has more pages than the outbox reads; treating it as unknown")
    return None


def _readwise_books(http, token):
    """(id, normalized title, normalized author) of the account's `books` category, or None."""
    books = []
    url, params = f"{_api_base()}/api/v2/books/", {"category": "books", "page_size": 1000}
    for _ in range(MAX_PAGES):
        response = _request(http, token, "get", url, params=params)
        body = _json(response) if response.status_code == 200 else None
        if not isinstance(body, dict):
            log.warning(f"Readwise books list answered {response.status_code}")
            return None
        for item in body.get("results") or []:
            books.append((item.get("id"), _normalize(item.get("title")), _normalize(item.get("author"))))
        url, params = body.get("next"), None
        if not url:
            break
    return books


# --- state --------------------------------------------------------------------

def _book_state(session, user_id, book_id) -> HighlightBookState:
    state = session.query(HighlightBookState).filter_by(user_id=user_id, book_id=book_id).first()
    if state is None:
        state = HighlightBookState(user_id=user_id, book_id=book_id,
                                   kobo_etag_version=0, kobo_delivered_version=0)
        session.add(state)
    return state


def _dirty(session, user_id):
    return session.query(Highlight).filter(
        Highlight.user_id == user_id, Highlight.readwise_dirty.is_(True)).order_by(Highlight.id)


def _needs_backfill(session, user_id, book_id) -> bool:
    state = session.query(HighlightBookState).filter_by(user_id=user_id, book_id=book_id).first()
    if state is not None and state.readwise_backfill_done:
        return False
    return session.query(Highlight.id).filter(
        Highlight.user_id == user_id, Highlight.book_id == book_id,
        Highlight.readwise_id.isnot(None)).first() is None


def _lookup(book_info_fn):
    return book_info_fn or book_info


# --- backfill -----------------------------------------------------------------

def backfill_skip_existing(session, user, http=requests, book_info=None) -> int:
    """
    Before a book's first push, mark its dirty highlights whose passage Readwise already
    holds (equal text after casefolding and collapsing whitespace) as not dirty, leaving
    `readwise_id` NULL. Returns how many were skipped.

    The Readwise book is found by title and author in the account's books list. A book is
    marked done only when Readwise answered; on any failure it stays undone and is not
    pushed (`push_dirty` skips it), so nothing is doubled.
    """
    token = user.readwise_token
    if not token or _blocked(user.id):
        return 0
    lookup = _lookup(book_info)
    book_ids = sorted({row.book_id for row in _dirty(session, user.id).all()
                       if row.deleted_at is None and row.readwise_id is None})
    skipped = 0
    readwise_books = None
    try:
        for book_id in book_ids:
            if not _needs_backfill(session, user.id, book_id):
                continue
            info = lookup(book_id)
            if info is None:
                continue
            if readwise_books is None:
                readwise_books = _readwise_books(http, token)
                if readwise_books is None:
                    return skipped
            wanted = (_normalize(info.title), _normalize(info.author))
            matches = [rid for rid, title, author in readwise_books if (title, author) == wanted]
            existing = set()
            if matches:
                items = _export_highlights(http, token, matches)
                if items is None:
                    continue
                existing = {_normalize(item.get("text")) for item in items}
            for row in _dirty(session, user.id).filter(
                    Highlight.book_id == book_id, Highlight.deleted_at.is_(None),
                    Highlight.readwise_id.is_(None)).all():
                if _normalize(row.text) in existing:
                    row.readwise_dirty = False
                    skipped += 1
            _book_state(session, user.id, book_id).readwise_backfill_done = True
            session.commit()
    except _RateLimited as e:
        session.rollback()
        _record_retry_after(user.id, e.retry_after)
    except _Stop:
        session.rollback()
    if skipped:
        log.info(f"Readwise backfill: {skipped} highlights already in Readwise for user {user.id}")
    return skipped


# --- push ---------------------------------------------------------------------
#
# Every request is followed by a write that depends on the highlight not having changed
# meanwhile: a Kobo or Readest change made during the round trip bumps `version` and sets
# `readwise_dirty`, and that must survive. So `readwise_dirty` is only ever cleared by an
# UPDATE ... WHERE version = <version seen when the request was built>. The Readwise id
# of a create is recorded regardless (the highlight exists in Readwise either way).

def _settle(session, row_id, seen_version, **values) -> bool:
    """Apply `values` to the row only if it is still at `seen_version`. Returns whether it was."""
    changed = session.query(Highlight).filter(
        Highlight.id == row_id, Highlight.version == seen_version).update(values, synchronize_session=False)
    return bool(changed)


def _record_id(session, row_id, readwise_id):
    session.query(Highlight).filter(Highlight.id == row_id).update(
        {"readwise_id": readwise_id}, synchronize_session=False)


def _is_client_error(response) -> bool:
    """A 4xx that says this request itself is refused (not auth, timeout or rate limit)."""
    return 400 <= response.status_code < 500 and response.status_code not in (401, 403, 408, 429)


def _quarantine(session, result, row_id, seen_version, response):
    """Readwise refuses this version of the highlight: stop re-sending it until it changes."""
    log.warning(f"Readwise refused highlight {row_id} with {response.status_code}; "
                f"not retried until it changes")
    _settle(session, row_id, seen_version, readwise_dirty=False, readwise_error=f"http {response.status_code}")
    session.commit()
    result.quarantined += 1


def _push_deletes(session, user, http, result):
    for row in _dirty(session, user.id).filter(Highlight.deleted_at.isnot(None)).all():
        row_id, seen, readwise_id = row.id, row.version, row.readwise_id
        if readwise_id is None:
            _settle(session, row_id, seen, readwise_dirty=False)  # never sent: nothing to delete
            session.commit()
            continue
        response = _request(http, user.readwise_token, "delete",
                            f"{_api_base()}/api/v2/highlights/{readwise_id}/")
        if response.status_code in (200, 202, 204, 404):  # 404: already gone
            # The Readwise copy is gone whatever happened locally meanwhile; a restored
            # highlight is created afresh.
            _record_id(session, row_id, None)
            _settle(session, row_id, seen, readwise_dirty=False)
            session.commit()
            result.deleted += 1
        else:
            _failed(result, response, row_id)


def _push_edits(session, user, http, result):
    for row in _dirty(session, user.id).filter(
            Highlight.deleted_at.is_(None), Highlight.readwise_id.isnot(None)).all():
        row_id, seen, readwise_id = row.id, row.version, row.readwise_id
        payload = _patch_payload(row)
        response = _request(http, user.readwise_token, "patch",
                            f"{_api_base()}/api/v2/highlights/{readwise_id}/", json=payload)
        if response.status_code == 200:
            _settle(session, row_id, seen, readwise_dirty=False)
            session.commit()
            result.updated += 1
        elif response.status_code == 404:
            # Deleted in Readwise: it stays deleted there rather than being brought back.
            log.warning(f"Readwise no longer has the copy of highlight {row_id}; not re-sent")
            _settle(session, row_id, seen, readwise_dirty=False)
            session.commit()
        elif _is_client_error(response):
            _quarantine(session, result, row_id, seen, response)
        else:
            _failed(result, response, row_id)


def _created_ids(http, token, response_books, rows_by_url):
    """
    Which of the posted highlights Readwise now holds as CWA's own copies: {row id: readwise id}.
    Ownership is verified in the book's export: only an item whose `url` is exactly our
    `highlight_url` counts, so a highlight Readwise deduped against an existing one (same
    text, another url) is never recorded and so never edited or deleted. Returns None when
    the export could not be read (nothing is known; the rows stay dirty).
    """
    book_ids = [book.get("id") for book in response_books or [] if book.get("id") is not None]
    if not book_ids:
        return None
    items = _export_highlights(http, token, book_ids)
    if items is None:
        return None
    return {rows_by_url[item["url"]]: str(item["id"]) for item in items
            if item.get("url") in rows_by_url and item.get("id") is not None}


def _post_creates(session, user, http, result, info, rows, locations, seen):
    """
    POST `rows` (one book) and record the outcome. Returns None when Readwise accepted the
    request, else the refusing response.
    """
    payloads = [_create_payload(info, row, locations.get(row.id, 0)) for row in rows]
    rows_by_url = {p["highlight_url"]: row.id for p, row in zip(payloads, rows)}
    row_ids = [row.id for row in rows]
    response = _request(http, user.readwise_token, "post",
                        f"{_api_base()}/api/v2/highlights/", json={"highlights": payloads})
    if response.status_code not in (200, 201):
        return response
    body = _json(response)
    ids = _created_ids(http, user.readwise_token, body if isinstance(body, list) else [], rows_by_url)
    if ids is None:
        log.warning(f"Readwise ids not found for highlights {row_ids}; left for the next run")
        result.failed += len(row_ids)
        return None
    for row_id in row_ids:
        if row_id in ids:
            _record_id(session, row_id, ids[row_id])
            _settle(session, row_id, seen[row_id], readwise_dirty=False)
            result.created += 1
        else:
            log.warning(f"Highlight {row_id} was deduped against an existing Readwise highlight; "
                        f"CWA keeps no copy there")
            _settle(session, row_id, seen[row_id], readwise_dirty=False)
            result.backfill_skipped += 1
    session.commit()
    return None


def _push_creates(session, user, http, result, lookup):
    rows = _dirty(session, user.id).filter(
        Highlight.deleted_at.is_(None), Highlight.readwise_id.is_(None)).all()
    by_book = {}
    for row in rows:
        by_book.setdefault(row.book_id, []).append(row)
    for book_id, book_rows in sorted(by_book.items()):
        if _needs_backfill(session, user.id, book_id):
            continue  # not checked against Readwise yet (backfill failed or was paused)
        info = lookup(book_id)
        if info is None:
            log.warning(f"Book {book_id} is not in the library; its highlights are not sent to Readwise")
            continue
        sendable = []
        for row in book_rows:
            if (row.text or "").strip():
                sendable.append(row)
            else:
                _settle(session, row.id, row.version, readwise_dirty=False)  # Readwise requires text
        session.commit()
        locations = _locations(session, user.id, book_id)
        for start in range(0, len(sendable), CREATE_CHUNK):
            chunk = sendable[start:start + CREATE_CHUNK]
            seen = {row.id: row.version for row in chunk}
            refused = _post_creates(session, user, http, result, info, chunk, locations, seen)
            if refused is None:
                continue
            if not _is_client_error(refused):
                log.warning(f"Readwise answered {refused.status_code} for highlights "
                            f"{list(seen)}; left for the next run")
                result.failed += len(chunk)
                if refused.status_code in (401, 403) or refused.status_code >= 500:
                    raise _Stop()
                continue
            # One bad item must not hold back the rest: try each on its own, once.
            if len(chunk) == 1:
                _quarantine(session, result, chunk[0].id, seen[chunk[0].id], refused)
                continue
            for row in chunk:
                row_seen = {row.id: seen[row.id]}
                row_id = row.id
                refused = _post_creates(session, user, http, result, info, [row], locations, row_seen)
                if refused is None:
                    continue
                if _is_client_error(refused):
                    _quarantine(session, result, row_id, seen[row_id], refused)
                else:
                    _failed(result, refused, row_id)


def push_dirty(session, user, http=requests, book_info=None) -> PushResult:
    """
    Send the user's dirty highlights to Readwise (see the module docstring). Commits as it
    goes, so a failure part way keeps what was sent. `book_info(book_id)` returns a
    BookInfo or None; it defaults to reading the Calibre DB.
    """
    result = PushResult()
    if not user.readwise_token:
        return result
    if _blocked(user.id):
        result.rate_limited = True
        return result
    lookup = _lookup(book_info)
    result.backfill_skipped = backfill_skip_existing(session, user, http, lookup)
    if _blocked(user.id):
        result.rate_limited = True
        return result
    try:
        _push_deletes(session, user, http, result)
        _push_edits(session, user, http, result)
        _push_creates(session, user, http, result, lookup)
    except _RateLimited as e:
        session.rollback()
        _record_retry_after(user.id, e.retry_after)
        result.rate_limited = True
    except _Stop:
        session.rollback()
    if result.created or result.updated or result.deleted or result.failed or result.quarantined:
        log.info(f"Readwise push for user {user.id}: {result.created} created, {result.updated} "
                 f"updated, {result.deleted} deleted, {result.quarantined} quarantined, "
                 f"{result.failed} failed")
    return result
