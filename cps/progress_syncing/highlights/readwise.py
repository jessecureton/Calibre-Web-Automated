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
    return found


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

def _push_deletes(session, user, http, result):
    for row in _dirty(session, user.id).filter(Highlight.deleted_at.isnot(None)).all():
        if row.readwise_id is None:
            row.readwise_dirty = False  # never sent: nothing to delete
            session.commit()
            continue
        response = _request(http, user.readwise_token, "delete",
                            f"{_api_base()}/api/v2/highlights/{row.readwise_id}/")
        if response.status_code in (200, 202, 204, 404):  # 404: already gone
            row.readwise_id = None  # a restored highlight is created afresh
            row.readwise_dirty = False
            result.deleted += 1
            session.commit()
        else:
            _failed(result, response, row.id)


def _push_edits(session, user, http, result):
    for row in _dirty(session, user.id).filter(
            Highlight.deleted_at.is_(None), Highlight.readwise_id.isnot(None)).all():
        response = _request(http, user.readwise_token, "patch",
                            f"{_api_base()}/api/v2/highlights/{row.readwise_id}/",
                            json=_patch_payload(row))
        if response.status_code == 200:
            row.readwise_dirty = False
            result.updated += 1
            session.commit()
        elif response.status_code == 404:
            # Deleted in Readwise: it stays deleted there rather than being brought back.
            log.warning(f"Readwise no longer has the copy of highlight {row.id}; not re-sent")
            row.readwise_dirty = False
            session.commit()
        else:
            _failed(result, response, row.id)


def _created_ids(http, token, posted, response_books, rows_by_url):
    """Map each posted highlight to the id Readwise gave it (None when unknown)."""
    modified = [i for book in response_books or [] for i in (book.get("modified_highlights") or [])]
    if len(posted) == 1 and len(modified) == 1:
        return {posted[0].id: (str(modified[0]), True)}
    book_ids = [book.get("id") for book in response_books or [] if book.get("id") is not None]
    if not book_ids:
        return {}
    items = _export_highlights(http, token, book_ids)
    if items is None:
        return {}
    mapping = {}
    by_text = {}
    for item in items:
        url = item.get("url")
        if url in rows_by_url:
            mapping[rows_by_url[url].id] = (str(item["id"]), True)
        else:
            by_text.setdefault(_normalize(item.get("text")), item)
    for row in posted:
        if row.id not in mapping and _normalize(row.text) in by_text:
            # Readwise held this passage already (the POST was deduped against it): it
            # is not ours, so keep no id.
            mapping[row.id] = (None, False)
    return mapping


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
                row.readwise_dirty = False  # Readwise requires text
        session.commit()
        locations = _locations(session, user.id, book_id)
        for start in range(0, len(sendable), CREATE_CHUNK):
            chunk = sendable[start:start + CREATE_CHUNK]
            payloads = [_create_payload(info, row, locations.get(row.id, 0)) for row in chunk]
            response = _request(http, user.readwise_token, "post",
                                f"{_api_base()}/api/v2/highlights/", json={"highlights": payloads})
            if response.status_code not in (200, 201):
                log.warning(f"Readwise answered {response.status_code} for highlights "
                            f"{[row.id for row in chunk]}; left for the next run")
                result.failed += len(chunk)
                if response.status_code in (401, 403) or response.status_code >= 500:
                    raise _Stop()
                continue
            body = _json(response)
            rows_by_url = {_highlight_url(info, row): row for row in chunk}
            ids = _created_ids(http, user.readwise_token, chunk,
                               body if isinstance(body, list) else [], rows_by_url)
            for row in chunk:
                if row.id in ids:
                    readwise_id, ours = ids[row.id]
                    row.readwise_id = readwise_id
                    row.readwise_dirty = False
                    result.created += 1 if ours else 0
                    result.backfill_skipped += 0 if ours else 1
            session.commit()
            missing = [row.id for row in chunk if row.id not in ids]
            if missing:
                log.warning(f"Readwise ids not found for highlights {missing}; left for the next run")
                result.failed += len(missing)


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
    if result.created or result.updated or result.deleted or result.failed:
        log.info(f"Readwise push for user {user.id}: {result.created} created, {result.updated} "
                 f"updated, {result.deleted} deleted, {result.failed} failed")
    return result
