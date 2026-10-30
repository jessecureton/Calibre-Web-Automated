# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Import highlights from a Kobo's own database (KoboReader.sqlite).

`read_kobo_highlights` reads a database copy, `plan_import` sorts its highlights
into what is new, already in CWA, recovered from an older copy, or unplaceable,
and `apply_import` writes the new and recovered ones. A plan writes nothing, an
import never deletes or edits anything CWA already holds, and importing twice
adds nothing the second time (a highlight is known by its Kobo BookmarkID, even
if it has since been deleted in CWA).

An imported row is built by the same code as one the device would PATCH, so
the two are identical. The older copy of a database holds the pre-migration
sideloaded renditions of books, whose positions mean nothing in the library's
KEPUBs: its highlights are matched to a library book by title and author and
placed by finding their text in the book.

No Flask here; the admin page is in cps/admin.py. Callers own the transaction:
`apply_import` flushes but never commits.
"""

import json
import os
import re
import shutil
import secrets
import sqlite3
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from ... import logger
from ..position_bridge.service import library_files
from .kobo_side import _KOBO_FIELDS, _Books, _has_range, _parse_kobo_time, fields_from_entry
from .ranges import count_text_occurrences, locate_text_range
from .store import Highlight, upsert_from_side

log = logger.create()

SPAN_PATH = "span#kobo"
STASH_TTL = 3600
STASH_ROOT = os.path.join(tempfile.gettempdir(), "cwa-highlight-import")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# Shorter passages are only ever matched whole; longer ones may sit inside another highlight.
_CONTAINED_MIN = 20

REASONS = {
    "no_book": "no library book has this id",
    "no_location": "not a KEPUB location",
    "unknown_book": "book not identified in the library",
    "ambiguous_book": "more than one library book matches",
    "no_files": "book files unavailable",
    "no_text": "text not found in the book",
    "ambiguous_text": "text occurs more than once in the book",
}
BUCKETS = ("new", "already_present", "contained_in_existing", "recovered", "unmatched")


class KoboDatabaseError(ValueError):
    """The file is not a Kobo database with a Bookmark table."""


class CwaBook(NamedTuple):
    id: int
    title: str
    authors: tuple


@dataclass
class PlanItem:
    """One Kobo highlight and what the plan makes of it. `fields` is set for new and recovered ones."""
    row: dict
    book_id: Optional[int] = None
    title: str = ""
    fields: Optional[dict] = None
    reason: Optional[str] = None


@dataclass
class ImportPlan:
    new: list = field(default_factory=list)
    already_present: list = field(default_factory=list)
    contained_in_existing: list = field(default_factory=list)  # older copy's text inside an existing highlight's
    recovered: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)

    def counts(self) -> dict:
        return {name: len(getattr(self, name)) for name in BUCKETS}

    def summary(self) -> list:
        """Per-book counts, by title, with a count per reason for the unmatched: never any highlight text."""
        books: dict = {}
        for bucket in BUCKETS:
            for item in getattr(self, bucket):
                title = item.title or item.row.get("title") or "(unknown book)"
                entry = books.setdefault(title, {**dict.fromkeys(BUCKETS, 0), "reasons": {}})
                entry[bucket] += 1
                if bucket == "unmatched" and item.reason:
                    entry["reasons"][item.reason] = entry["reasons"].get(item.reason, 0) + 1
        return [{"title": title, **counts} for title, counts in sorted(books.items(), key=lambda kv: kv[0].casefold())]


# --- reading -------------------------------------------------------------------------------

def chapter_from_content_id(content_id: Optional[str]) -> Optional[str]:
    """
    The KEPUB zip path of a Kobo ContentID's chapter, or None if it has none.

    `<VolumeID>!ops!xhtml/ch01a.html` is `ops/xhtml/ch01a.html`: everything
    after the first `!`, the remaining `!` read as `/`. An empty OPF-dir part
    (`<uuid>!!path`) gives just `path`.
    """
    if not content_id or "!" not in content_id:
        return None
    parts = [part for part in content_id.split("!", 1)[1].split("!") if part]
    return "/".join(parts) or None


def old_chapter_from_content_id(content_id: Optional[str]) -> Optional[str]:
    """The chapter path in an older rendition's ContentID: `...epub#(7)OEBPS/x.xhtml` or `...!OEBPS!x.xhtml`."""
    if not content_id:
        return None
    if "#" in content_id:
        return re.sub(r"^\(\d+\)", "", content_id.split("#", 1)[1]) or None
    return chapter_from_content_id(content_id)


_ROW = """
    SELECT b.BookmarkID, b.VolumeID, b.ContentID, b.StartContainerPath, b.StartOffset,
           b.EndContainerPath, b.EndOffset, b.Text, b.Annotation, b.DateCreated, b.DateModified,
           b.ChapterProgress, {title}, {author}
    FROM Bookmark b {join}
    WHERE b.Type = 'highlight' AND {visible} AND TRIM(COALESCE(b.Text, '')) <> ''
    ORDER BY b.DateCreated, b.BookmarkID
"""
# Nickel stores Hidden as the text 'false'/'true'; older or hand-made copies use 0/1.
_VISIBLE = "LOWER(CAST(COALESCE(b.Hidden, 0) AS TEXT)) NOT IN ('1', 'true')"



def _connect(db_path: str) -> sqlite3.Connection:
    uri = Path(os.path.abspath(db_path)).as_uri()
    try:
        con = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError:
        # A copy left in WAL mode without its -wal file can't be opened by the plain read-only mode.
        con = sqlite3.connect(f"{uri}?immutable=1", uri=True)
    return con


def read_kobo_highlights(db_path: str) -> list:
    """
    The highlights in a KoboReader.sqlite, oldest first, each joined to its book's title and author.

    Only `Type='highlight'` rows with text, and not hidden. Raises
    KoboDatabaseError if the file isn't a Kobo database. The file is opened read-only.
    """
    try:
        con = _connect(db_path)
    except sqlite3.DatabaseError as e:
        raise KoboDatabaseError("not a SQLite database") from e
    try:
        tables = {name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "Bookmark" not in tables:
            raise KoboDatabaseError("no Bookmark table")
        if "content" in tables:
            sql = _ROW.format(title="c.Title", author="c.Attribution", visible=_VISIBLE,
                              join="LEFT JOIN content c ON c.ContentID = b.VolumeID AND c.ContentType = '6'")
        else:
            sql = _ROW.format(title="NULL", author="NULL", join="", visible=_VISIBLE)
        try:
            fetched = con.execute(sql).fetchall()
        except sqlite3.DatabaseError as e:
            raise KoboDatabaseError(f"unreadable Bookmark table ({type(e).__name__})") from e
    except sqlite3.DatabaseError as e:
        raise KoboDatabaseError("not a SQLite database") from e
    finally:
        con.close()
    rows, seen = [], set()
    for (bookmark_id, volume, content, start_path, start_offset, end_path, end_offset, text, note, created,
         modified, progress, title, author) in fetched:
        if bookmark_id in seen:  # a book listed twice in content must not double its highlights
            continue
        seen.add(bookmark_id)
        rows.append({
            "bookmark_id": bookmark_id, "volume_id": volume, "content_id": content,
            "start_path": start_path, "start_offset": start_offset,
            "end_path": end_path, "end_offset": end_offset,
            "text": text, "note": note if (note or "").strip() else None,
            "created": _parse_kobo_time(created), "modified": _parse_kobo_time(modified),
            "chapter_progress": progress, "title": title, "author": author,
        })
    return rows


# --- library lookups -----------------------------------------------------------------------

def _cwa_book(book) -> CwaBook:
    return CwaBook(book.id, book.title, tuple(a.name for a in book.authors))


def book_by_uuid(book_uuid: str) -> Optional[CwaBook]:
    """The library book with this uuid (metadata.db `books.uuid`), or None."""
    from ... import calibre_db
    book = calibre_db.get_book_by_uuid(book_uuid)
    return _cwa_book(book) if book else None


def all_books() -> list:
    """Every library book, for matching by title and author."""
    from sqlalchemy.orm import joinedload
    from ... import calibre_db, db
    calibre_db.ensure_session()
    return [_cwa_book(b) for b in calibre_db.session.query(db.Books).options(joinedload(db.Books.authors)).all()]


# --- matching ------------------------------------------------------------------------------

def _words(text: Optional[str]) -> str:
    """Lower-case words of text with accents, punctuation and spacing dropped."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", plain.casefold()).split())


def _main_title(title: Optional[str]) -> str:
    return _words((title or "").split(":", 1)[0])


def _author_matches(attribution: Optional[str], authors: tuple) -> bool:
    wanted = set(_words(attribution).split())
    if not wanted:
        return False
    return wanted == set(_words(" ".join(authors)).split()) or \
        any(wanted == set(_words(a).split()) for a in authors)


class _Catalog:
    """The library's books by title, to find the one a Kobo title and author name."""

    def __init__(self, books):
        self._books = list(books)
        self._by_main: dict = {}
        for book in self._books:
            self._by_main.setdefault(_main_title(book.title), []).append(book)

    def find(self, title: Optional[str], author: Optional[str]):
        """(book, None), or (None, reason)."""
        if not title or not author:
            return None, "unknown_book"
        candidates = [b for b in self._by_main.get(_main_title(title), ()) if _author_matches(author, b.authors)]
        exact = [b for b in candidates if _words(b.title) == _words(title)]
        found = exact or candidates
        if not found:
            return None, "unknown_book"
        return (found[0], None) if len(found) == 1 else (None, "ambiguous_book")


def _text_present(key: str, known: list) -> Optional[str]:
    """"equal" if a known text is the same, "contained" if one holds the other, else None."""
    contained = False
    for other in known:
        if key == other:
            return "equal"
        if min(len(key), len(other)) >= _CONTAINED_MIN and (key in other or other in key):
            contained = True
    return "contained" if contained else None


def _entry(row: dict, chapter: str, progress, start_path, start_char, end_path, end_char) -> dict:
    """The row as the wire annotation `apply_kobo_patch` would have been sent for it."""
    return {
        "id": row["bookmark_id"], "type": "note" if row["note"] else "highlight",
        "highlightedText": row["text"], "noteText": row["note"],
        "location": {"span": {
            "chapterFilename": chapter, "chapterProgress": progress,
            "startPath": start_path, "startChar": start_char, "endPath": end_path, "endChar": end_char}},
    }


def _stamp(fields: dict, row: dict) -> dict:
    created = row["created"]
    modified = row["modified"] or created
    if created:
        fields["created_at"] = created
    if modified:
        fields["updated_at"] = modified
    return fields


def _is_span_path(path) -> bool:
    return isinstance(path, str) and path.startswith(SPAN_PATH)


# --- planning ------------------------------------------------------------------------------

def plan_import(session, user_id: int, rows, older_rows=(), *,
                find_book: Optional[Callable] = None, catalog: Optional[Callable] = None,
                files: Optional[Callable] = None) -> ImportPlan:
    """
    Sort Kobo highlights into an ImportPlan for a user, writing nothing.

    Each row's `VolumeID` names a library book by uuid, and its Kobo location
    and `BookmarkID` are kept as the device holds them (new). One already in the
    user's highlights by BookmarkID, deleted or not, is `already_present`. One
    that can't be placed in a library book is `unmatched`, with a reason.

    `older_rows` are an older database's: each is matched to a library book by
    title and author, is `already_present` if its text equals one of the book's
    highlights (the main rows that are new or already present, or the user's,
    deleted or not), or `contained_in_existing` if it holds or sits inside one
    (20 characters or more). Otherwise it is placed by finding its text in the
    book: in the older rendition's chapter if the library KEPUB has that file
    and the text is in it, else anywhere in the book. It is `recovered` only if
    the text occurs exactly once there; a repeated passage is `unmatched`
    (`ambiguous_text`), never guessed between its copies.

    `find_book`, `catalog` and `files` default to the library's lookups.
    """
    find_book = find_book or book_by_uuid
    files = files or library_files
    plan = ImportPlan()
    opened: dict = {}
    volumes: dict = {}
    main_texts: dict = {}  # book_id -> normalized texts of the main database's rows
    known: dict = {}       # book_id -> normalized texts the book already has, only read for an older copy

    def books_for(book_id):
        if book_id not in opened:
            epub_path, kepub_path = files(book_id)
            opened[book_id] = _Books(session, user_id, book_id, epub_path, kepub_path)
        return opened[book_id]

    def known_texts(book_id):
        if book_id not in known:
            known[book_id] = main_texts.get(book_id, []) + [
                _words(t) for (t,) in session.query(Highlight.text).filter(
                    Highlight.user_id == user_id, Highlight.book_id == book_id)]
        return known[book_id]

    def in_cwa(kobo_id):
        return session.query(Highlight.id).filter(
            Highlight.user_id == user_id, Highlight.kobo_id == kobo_id).first() is not None

    main_ids = set()
    for row in rows:
        main_ids.add(row["bookmark_id"])
        volume = row["volume_id"]
        if volume not in volumes:
            volumes[volume] = find_book(volume)
        book = volumes[volume]
        if book is None:
            plan.unmatched.append(PlanItem(row, reason=REASONS["no_book"]))
            continue
        if in_cwa(row["bookmark_id"]):
            main_texts.setdefault(book.id, []).append(_words(row["text"]))
            plan.already_present.append(PlanItem(row, book.id, book.title))
            continue
        chapter = chapter_from_content_id(row["content_id"])
        if not chapter or not (_is_span_path(row["start_path"]) and _is_span_path(row["end_path"])):
            plan.unmatched.append(PlanItem(row, book.id, book.title, reason=REASONS["no_location"]))
            continue
        entry = _entry(row, chapter, row["chapter_progress"], row["start_path"], row["start_offset"],
                       row["end_path"], row["end_offset"])
        fields = _stamp(fields_from_entry(books_for(book.id), entry), row)
        main_texts.setdefault(book.id, []).append(_words(row["text"]))
        plan.new.append(PlanItem(row, book.id, book.title, fields))

    older_rows = list(older_rows)
    if older_rows:
        cat = _Catalog((catalog or all_books)())
        matched: dict = {}
        for row in older_rows:
            key = (row["title"], row["author"])
            if key not in matched:
                matched[key] = cat.find(*key)
            book, why = matched[key]
            if book is None:
                plan.unmatched.append(PlanItem(row, reason=REASONS[why]))
                continue
            if row["bookmark_id"] in main_ids or in_cwa(row["bookmark_id"]):
                plan.already_present.append(PlanItem(row, book.id, book.title))
                continue
            present = _text_present(_words(row["text"]), known_texts(book.id))
            if present:
                bucket = plan.already_present if present == "equal" else plan.contained_in_existing
                bucket.append(PlanItem(row, book.id, book.title))
                continue
            opened_books = books_for(book.id).open()
            if opened_books is None:
                plan.unmatched.append(PlanItem(row, book.id, book.title, reason=REASONS["no_files"]))
                continue
            kepub = opened_books[1]
            progress = row["chapter_progress"] or 0.0
            try:
                chapter = old_chapter_from_content_id(row["content_id"])
                if chapter is not None and not count_text_occurrences(kepub, chapter, row["text"]):
                    chapter = None  # the chapter is not there, or the text isn't in it: the whole book
                occurrences = count_text_occurrences(kepub, chapter, row["text"])
                found = locate_text_range(kepub, chapter, row["text"], progress) if occurrences == 1 else None
            except Exception as e:
                log.warning(f"[highlight-sync] older highlight {row['bookmark_id']}: could not be placed "
                            f"({type(e).__name__})")
                occurrences, found = 0, None
            if occurrences > 1:  # never guess between copies of a repeated passage
                plan.unmatched.append(PlanItem(row, book.id, book.title, reason=REASONS["ambiguous_text"]))
                continue
            if found is None:
                plan.unmatched.append(PlanItem(row, book.id, book.title, reason=REASONS["no_text"]))
                continue
            entry = _entry(row, found.chapter, found.chapter_progress, found.start_path, found.start_char,
                           found.end_path, found.end_char)
            fields = _stamp(fields_from_entry(books_for(book.id), entry), row)
            known_texts(book.id).append(_words(row["text"]))
            plan.recovered.append(PlanItem(row, book.id, book.title, fields))
    return plan


def apply_import(session, user_id: int, plan: ImportPlan, now) -> int:
    """
    Store a plan's new and recovered highlights for the user, returning how many.

    A highlight whose BookmarkID the user's store already holds (even deleted)
    is skipped, whatever the plan says, so a stale plan or a second run adds
    nothing and never overwrites an edit or a delete. A highlight CWA already
    holds under another id (the same passage from Readest) is linked to, as
    with a Kobo PATCH, keeping its content. Flushes; the caller commits.
    """
    stored = 0
    for item in [*plan.new, *plan.recovered]:
        kobo_id = item.row["bookmark_id"]
        if session.query(Highlight.id).filter(
                Highlight.user_id == user_id, Highlight.kobo_id == kobo_id).first() is not None:
            continue
        row = upsert_from_side(session, user_id, item.book_id, "import", kobo_id, item.fields, now)
        if not _has_range(row) and all(item.fields.get(name) is not None for name in _KOBO_FIELDS[:5]):
            # Merged into a highlight that is newer: the device still holds this one at this location.
            for name in _KOBO_FIELDS:
                setattr(row, name, item.fields.get(name))
            session.flush()
        stored += 1
    if stored:
        log.info(f"[highlight-sync] user={user_id}: imported {stored} highlights from a Kobo database")
    return stored


# --- keeping an upload for the confirm step -------------------------------------------------

class Stash(NamedTuple):
    token: str
    user_id: int
    main_path: str
    older_path: Optional[str]


def _stash_dir(token) -> Optional[str]:
    if not isinstance(token, str) or not _TOKEN.match(token):
        return None
    return os.path.join(STASH_ROOT, token)


def save_stash(main, older, user_id: int, now: Optional[float] = None) -> str:
    """Keep uploaded database files (objects with `.save(path)`) for the confirm step; returns a token."""
    os.makedirs(STASH_ROOT, mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(24)
    directory = _stash_dir(token)
    os.mkdir(directory, 0o700)
    try:
        main.save(os.path.join(directory, "main.sqlite"))
        if older is not None:
            older.save(os.path.join(directory, "older.sqlite"))
        with open(os.path.join(directory, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"user_id": user_id, "created": time.time() if now is None else now}, f)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return token


def load_stash(token, now: Optional[float] = None) -> Optional[Stash]:
    """The kept files for a token, or None if there are none or they are over an hour old (then deleted)."""
    directory = _stash_dir(token)
    if directory is None or not os.path.isdir(directory):
        return None
    try:
        with open(os.path.join(directory, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        age = (time.time() if now is None else now) - float(meta["created"])
        user_id = int(meta["user_id"])
    except (OSError, ValueError, KeyError, TypeError):
        age, user_id = STASH_TTL + 1, None
    if age > STASH_TTL:
        shutil.rmtree(directory, ignore_errors=True)
        return None
    older = os.path.join(directory, "older.sqlite")
    return Stash(token, user_id, os.path.join(directory, "main.sqlite"), older if os.path.isfile(older) else None)


def discard_stash(token) -> None:
    directory = _stash_dir(token)
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)


def purge_expired(now: Optional[float] = None) -> None:
    """Delete every kept upload over an hour old."""
    try:
        names = os.listdir(STASH_ROOT)
    except OSError:
        return
    for name in names:
        load_stash(name, now)
