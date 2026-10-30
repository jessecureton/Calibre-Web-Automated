# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Readwise outbox tests. HTTP is a hand-written fake with the shapes Readwise documents
(https://readwise.io/api_deets): POST /highlights/ answers a list of books carrying
`modified_highlights`, the export answers {count, nextPageCursor, results}, the books
list answers {count, next, results}.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import ub
from cps.progress_syncing.highlights import readwise
from cps.progress_syncing.highlights.readwise import BookInfo, backfill_skip_existing, push_dirty
from cps.progress_syncing.highlights.store import (
    Highlight, HighlightBookState, delete_from_side, upsert_from_side,
)

pytestmark = pytest.mark.unit

USER, BOOK, OTHER_BOOK = 3, 1, 2
TOKEN = "tok-SECRET-0123456789"
UUID = "7c0a3f3e-1111-4222-8333-444455556666"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

P = "/body/DocFragment[{frag}]/body/section/p[{n}]/text().{off}"


def xp(frag, n, off=0):
    return P.format(frag=frag, n=n, off=off)


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeHttp:
    """Records every call; `routes` maps (method, path) to a response or a callable."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.next_id = 9000

    def _do(self, method, url, headers=None, json=None, params=None, timeout=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append({"method": method, "path": "/" + path.split("?")[0], "url": url,
                           "headers": headers, "json": json, "params": params})
        route = self.routes.get((method, "/" + path.split("?")[0]))
        if route is None:
            route = self.default(method, "/" + path.split("?")[0], json)
        return route(self.calls[-1]) if callable(route) else route

    def default(self, method, path, body):
        if method == "post" and path == "/api/v2/highlights/":
            def create(call):
                ids = []
                for _ in call["json"]["highlights"]:
                    self.next_id += 1
                    ids.append(self.next_id)
                return FakeResponse(200, [{"id": 555, "title": "T", "modified_highlights": ids}])
            return create
        if method == "patch":
            return FakeResponse(200, {"id": 1})
        if method == "delete":
            return FakeResponse(204)
        return FakeResponse(200, {"count": 0, "next": None, "nextPageCursor": None, "results": []})

    def get(self, url, **kw):
        return self._do("get", url, **kw)

    def post(self, url, **kw):
        return self._do("post", url, **kw)

    def patch(self, url, **kw):
        return self._do("patch", url, **kw)

    def delete(self, url, **kw):
        return self._do("delete", url, **kw)

    def of(self, method, path=None):
        return [c for c in self.calls if c["method"] == method and (path is None or c["path"] == path)]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(readwise, "_retry_not_before", {})
    monkeypatch.delenv("READWISE_API_BASE", raising=False)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="jcureton", email="j@example.test", readwise_token=TOKEN))
    s.add(ub.User(id=USER + 1, name="notoken", email="n@example.test"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def user(session):
    return session.get(ub.User, USER)


@pytest.fixture
def http():
    return FakeHttp()


BOOKS = {BOOK: BookInfo("The Test Book", "Ann Author, Bob Writer", UUID),
         OTHER_BOOK: BookInfo("Other Book", "Cy Pher", "uuid-other")}


def lookup(book_id):
    return BOOKS.get(book_id)


def add(session, kobo_id, text, book=BOOK, note=None, xp0=None, at=T0, **extra):
    fields = {"text": text, "note": note, "xp0": xp0 or xp(2, 1), "updated_at": at, "created_at": at, **extra}
    row = upsert_from_side(session, USER, book, "kobo", kobo_id, fields, at)
    session.commit()
    return row


def mark_done(session, book=BOOK):
    """Skip the backfill: the book has been checked against Readwise already."""
    state = readwise._book_state(session, USER, book)
    state.readwise_backfill_done = True
    session.commit()


def test_new_highlight_posted_with_locator_and_id_stored(session, user, http):
    row = add(session, "k1", "A passage", note="my note", color="yellow", xp0=xp(4, 2, 10))
    mark_done(session)
    result = push_dirty(session, user, http, book_info=lookup)
    (call,) = http.of("post", "/api/v2/highlights/")
    assert call["url"] == "https://readwise.io/api/v2/highlights/"
    assert call["headers"] == {"Authorization": f"Token {TOKEN}"}
    (sent,) = call["json"]["highlights"]
    assert sent == {
        "text": "A passage", "title": "The Test Book", "author": "Ann Author, Bob Writer",
        "source_type": "cwa", "category": "books", "note": "my note",
        "location_type": "order", "location": 1,
        "highlighted_at": "2026-10-01T12:00:00Z",
        "highlight_url": f"cwa://{UUID}/highlight/{row.id}", "color": "yellow"}
    session.refresh(row)
    assert row.readwise_id == "9001" and row.readwise_dirty is False
    assert (result.created, result.failed, result.rate_limited) == (1, 0, False)
    assert not http.of("get")  # a lone create needs no export to learn its id


def test_location_is_position_in_book_not_creation_order(session, user, http):
    late = add(session, "k1", "late", xp0=xp(9, 1, 0), at=T0)
    early = add(session, "k2", "early", xp0=xp(2, 7, 5), at=T0 + timedelta(hours=1))
    mid = add(session, "k3", "mid", xp0=xp(2, 7, 40), at=T0 + timedelta(hours=2))
    mark_done(session)
    http.routes[("post", "/api/v2/highlights/")] = FakeResponse(
        200, [{"id": 555, "modified_highlights": [1, 2, 3]}])
    http.routes[("get", "/api/v2/export/")] = lambda call: FakeResponse(200, {
        "count": 1, "nextPageCursor": None, "results": [{"user_book_id": 555, "highlights": [
            {"id": 100 + h.id, "text": h.text, "url": f"cwa://{UUID}/highlight/{h.id}"}
            for h in (late, early, mid)]}]})
    push_dirty(session, user, http, book_info=lookup)
    sent = {h["text"]: h["location"] for h in http.of("post")[0]["json"]["highlights"]}
    assert sent == {"early": 1, "mid": 2, "late": 3}
    assert len(http.of("post")) == 1  # one POST for the whole book
    for h in (late, early, mid):
        session.refresh(h)
        assert h.readwise_id == str(100 + h.id) and not h.readwise_dirty
    export = http.of("get", "/api/v2/export/")[0]
    assert export["params"]["ids"] == "555"


def test_batch_posts_one_request_per_book(session, user, http):
    add(session, "k1", "one")
    add(session, "k2", "two")
    add(session, "k3", "elsewhere", book=OTHER_BOOK)
    mark_done(session)
    mark_done(session, OTHER_BOOK)
    http.routes[("get", "/api/v2/export/")] = FakeResponse(200, {"count": 0, "nextPageCursor": None, "results": []})
    push_dirty(session, user, http, book_info=lookup)
    posts = http.of("post")
    assert [len(p["json"]["highlights"]) for p in posts] == [2, 1]
    assert [p["json"]["highlights"][0]["title"] for p in posts] == ["The Test Book", "Other Book"]


def test_edit_patches_and_delete_deletes_only_cwa_created(session, user, http):
    mark_done(session)
    created = add(session, "k1", "made here", note="n")
    created.readwise_id, created.readwise_dirty = "7001", False
    unsent = add(session, "k2", "never sent")
    unsent.readwise_dirty = False
    session.commit()

    # edit the created one, delete both
    upsert_from_side(session, USER, BOOK, "kobo", "k1",
                     {"text": "made here", "note": "edited", "updated_at": T0 + timedelta(hours=1)},
                     T0 + timedelta(hours=1))
    session.commit()
    result = push_dirty(session, user, http, book_info=lookup)
    (patch,) = http.of("patch")
    assert patch["path"] == "/api/v2/highlights/7001/"
    assert patch["json"]["note"] == "edited" and patch["json"]["text"] == "made here"
    assert result.updated == 1 and not http.of("delete")

    delete_from_side(session, USER, BOOK, "kobo", "k1", T0 + timedelta(hours=2))
    delete_from_side(session, USER, BOOK, "kobo", "k2", T0 + timedelta(hours=2))
    session.commit()
    http.calls.clear()
    result = push_dirty(session, user, http, book_info=lookup)
    assert [(c["method"], c["path"]) for c in http.calls] == [("delete", "/api/v2/highlights/7001/")]
    assert result.deleted == 1
    for row in session.query(Highlight).all():
        session.refresh(row)
        assert row.readwise_dirty is False
    assert session.query(Highlight).filter_by(kobo_id="k2").one().readwise_id is None


def test_order_is_deletes_then_edits_then_creates(session, user, http):
    mark_done(session)
    gone = add(session, "k1", "gone")
    gone.readwise_id, gone.readwise_dirty = "1", False
    edited = add(session, "k2", "edited")
    edited.readwise_id, edited.readwise_dirty = "2", False
    session.commit()
    add(session, "k3", "fresh")
    delete_from_side(session, USER, BOOK, "kobo", "k1", T0 + timedelta(hours=1))
    upsert_from_side(session, USER, BOOK, "kobo", "k2", {"text": "edited again", "updated_at": T0 + timedelta(hours=1)},
                     T0 + timedelta(hours=1))
    session.commit()
    push_dirty(session, user, http, book_info=lookup)
    assert [c["method"] for c in http.calls if c["method"] != "get"] == ["delete", "patch", "post"]


def test_backfill_skips_passages_already_in_readwise(session, user, http):
    have = add(session, "k1", "Already   in READWISE", xp0=xp(2, 1))
    other = add(session, "k2", "Only here", xp0=xp(2, 2))
    http.routes[("get", "/api/v2/books/")] = FakeResponse(200, {"count": 2, "next": None, "results": [
        {"id": 41, "title": "Unrelated", "author": "Someone", "category": "books"},
        {"id": 42, "title": "the test book", "author": "ANN AUTHOR,  Bob Writer", "category": "books"}]})
    http.routes[("get", "/api/v2/export/")] = FakeResponse(200, {"count": 1, "nextPageCursor": None, "results": [
        {"user_book_id": 42, "highlights": [
            {"id": 8, "text": "already in readwise", "is_deleted": False, "url": None},
            {"id": 9, "text": "Only here", "is_deleted": True}]}]})
    assert backfill_skip_existing(session, user, http, book_info=lookup) == 1
    assert http.of("get", "/api/v2/export/")[0]["params"]["ids"] == "42"
    session.refresh(have)
    session.refresh(other)
    assert have.readwise_dirty is False and have.readwise_id is None
    assert other.readwise_dirty is True  # the Readwise copy was deleted, so it still goes
    state = session.query(HighlightBookState).filter_by(user_id=USER, book_id=BOOK).one()
    assert state.readwise_backfill_done is True


def test_push_runs_backfill_first_and_only_once_per_book(session, user, http):
    have = add(session, "k1", "Same passage")
    add(session, "k2", "New passage", xp0=xp(2, 2))
    http.routes[("get", "/api/v2/books/")] = FakeResponse(200, {"count": 1, "next": None, "results": [
        {"id": 42, "title": "The Test Book", "author": "Ann Author, Bob Writer"}]})
    http.routes[("get", "/api/v2/export/")] = FakeResponse(200, {"count": 1, "nextPageCursor": None, "results": [
        {"user_book_id": 42, "highlights": [{"id": 8, "text": "same passage"}]}]})
    result = push_dirty(session, user, http, book_info=lookup)
    (post,) = http.of("post")
    assert [h["text"] for h in post["json"]["highlights"]] == ["New passage"]
    assert result.backfill_skipped == 1 and result.created == 1
    session.refresh(have)
    assert have.readwise_id is None and not have.readwise_dirty

    # a later highlight in the same book: no listing again
    add(session, "k3", "Later one", xp0=xp(2, 3))
    http.calls.clear()
    push_dirty(session, user, http, book_info=lookup)
    assert not http.of("get")
    assert len(http.of("post")) == 1


def test_backfill_with_no_matching_readwise_book_still_marks_done(session, user, http):
    add(session, "k1", "x")
    http.routes[("get", "/api/v2/books/")] = FakeResponse(200, {"count": 0, "next": None, "results": []})
    assert backfill_skip_existing(session, user, http, book_info=lookup) == 0
    assert not http.of("get", "/api/v2/export/")
    assert session.query(HighlightBookState).filter_by(book_id=BOOK).one().readwise_backfill_done is True


def test_failed_backfill_pushes_nothing_for_that_book(session, user, http):
    row = add(session, "k1", "x")
    http.routes[("get", "/api/v2/books/")] = FakeResponse(500, None)
    result = push_dirty(session, user, http, book_info=lookup)
    assert not http.of("post")
    session.refresh(row)
    assert row.readwise_dirty is True
    assert not session.query(HighlightBookState).filter_by(book_id=BOOK, readwise_backfill_done=True).all()
    assert result.created == 0


def test_429_reschedules_without_losing_items(session, user, http, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(readwise, "_now", lambda: clock[0])
    mark_done(session)
    rows = [add(session, f"k{n}", f"text {n}", xp0=xp(2, n)) for n in range(3)]
    http.routes[("post", "/api/v2/highlights/")] = FakeResponse(429, None, {"Retry-After": "120"})
    result = push_dirty(session, user, http, book_info=lookup)
    assert result.rate_limited is True and result.created == 0
    assert readwise._retry_not_before[USER] == 1120.0
    for row in rows:
        session.refresh(row)
        assert row.readwise_dirty is True and row.readwise_id is None

    # before the time: no requests at all
    http.calls.clear()
    clock[0] = 1119.0
    assert push_dirty(session, user, http, book_info=lookup).rate_limited is True
    assert http.calls == []

    # after it: everything goes out
    clock[0] = 1121.0
    http.routes.pop(("post", "/api/v2/highlights/"))
    http.routes[("get", "/api/v2/export/")] = lambda call: FakeResponse(200, {
        "count": 1, "nextPageCursor": None, "results": [{"user_book_id": 555, "highlights": [
            {"id": 100 + r.id, "text": r.text, "url": f"cwa://{UUID}/highlight/{r.id}"} for r in rows]}]})
    push_dirty(session, user, http, book_info=lookup)
    for row in rows:
        session.refresh(row)
        assert row.readwise_dirty is False and row.readwise_id == str(100 + row.id)


def test_429_without_header_defaults_to_60_seconds(session, user, http, monkeypatch):
    monkeypatch.setattr(readwise, "_now", lambda: 50.0)
    mark_done(session)
    add(session, "k1", "x")
    http.routes[("post", "/api/v2/highlights/")] = FakeResponse(429, None)
    push_dirty(session, user, http, book_info=lookup)
    assert readwise._retry_not_before[USER] == 110.0


def test_never_sleeps(session, user, http, monkeypatch):
    monkeypatch.setattr(readwise.time, "sleep", lambda s: pytest.fail("the outbox must not sleep"))
    mark_done(session)
    add(session, "k1", "x")
    http.routes[("post", "/api/v2/highlights/")] = FakeResponse(429, None, {"Retry-After": "5"})
    push_dirty(session, user, http, book_info=lookup)


def test_server_error_leaves_items_dirty_and_stops(session, user, http, caplog):
    mark_done(session)
    mark_done(session, OTHER_BOOK)
    a = add(session, "k1", "text a")
    b = add(session, "k2", "text b", book=OTHER_BOOK)
    http.routes[("post", "/api/v2/highlights/")] = FakeResponse(503, None)
    with caplog.at_level(logging.DEBUG):
        result = push_dirty(session, user, http, book_info=lookup)
    assert len(http.of("post")) == 1  # a 5xx would repeat for the rest: stop
    assert result.failed == 1 and result.created == 0
    for row in (a, b):
        session.refresh(row)
        assert row.readwise_dirty is True
    assert "503" in caplog.text and f"[{a.id}]" in caplog.text
    assert "text a" not in caplog.text


def test_connection_error_leaves_items_dirty(session, user, http):
    mark_done(session)
    row = add(session, "k1", "x")

    def boom(call):
        raise requests.ConnectionError("down")
    http.routes[("post", "/api/v2/highlights/")] = boom
    push_dirty(session, user, http, book_info=lookup)
    session.refresh(row)
    assert row.readwise_dirty is True


def test_patch_404_and_delete_404(session, user, http):
    mark_done(session)
    edited = add(session, "k1", "a")
    edited.readwise_id, edited.readwise_dirty = "11", True
    gone = add(session, "k2", "b", xp0=xp(2, 2))
    gone.readwise_id, gone.readwise_dirty = "12", True
    gone.deleted_at = T0
    session.commit()
    http.routes[("patch", "/api/v2/highlights/11/")] = FakeResponse(404, None)
    http.routes[("delete", "/api/v2/highlights/12/")] = FakeResponse(404, None)
    push_dirty(session, user, http, book_info=lookup)
    for row in (edited, gone):
        session.refresh(row)
        assert row.readwise_dirty is False
    assert edited.readwise_id == "11" and gone.readwise_id is None
    assert not http.of("post")


def test_deleted_without_readwise_id_makes_no_request(session, user, http):
    row = add(session, "k1", "x")
    delete_from_side(session, USER, BOOK, "kobo", "k1", T0 + timedelta(hours=1))
    session.commit()
    push_dirty(session, user, http, book_info=lookup)
    assert http.calls == []
    session.refresh(row)
    assert row.readwise_dirty is False


def test_post_deduped_by_readwise_keeps_no_id(session, user, http):
    """An exact re-post is ignored by Readwise (modified_highlights empty); the export shows
    the passage with someone else's url, so it is not ours to edit or delete later."""
    mark_done(session)
    row = add(session, "k1", "Known passage")
    http.routes[("post", "/api/v2/highlights/")] = FakeResponse(200, [{"id": 555, "modified_highlights": []}])
    http.routes[("get", "/api/v2/export/")] = FakeResponse(200, {"count": 1, "nextPageCursor": None, "results": [
        {"user_book_id": 555, "highlights": [{"id": 3, "text": "known passage", "url": None}]}]})
    push_dirty(session, user, http, book_info=lookup)
    session.refresh(row)
    assert row.readwise_id is None and row.readwise_dirty is False


def test_unresolved_id_stays_dirty_and_is_reposted_idempotently(session, user, http):
    mark_done(session)
    row = add(session, "k1", "x")
    add(session, "k2", "y", xp0=xp(2, 2))
    http.routes[("post", "/api/v2/highlights/")] = FakeResponse(200, [{"id": 555, "modified_highlights": [1, 2]}])
    http.routes[("get", "/api/v2/export/")] = FakeResponse(500, None)
    push_dirty(session, user, http, book_info=lookup)
    session.refresh(row)
    assert row.readwise_dirty is True and row.readwise_id is None


def test_empty_text_is_not_sent(session, user, http):
    mark_done(session)
    row = add(session, "k1", "   ")
    push_dirty(session, user, http, book_info=lookup)
    assert not http.of("post")
    session.refresh(row)
    assert row.readwise_dirty is False


def test_no_token_no_requests(session, http):
    other = session.get(ub.User, USER + 1)
    add(session, "k1", "x")
    assert push_dirty(session, other, http, book_info=lookup) == readwise.PushResult()
    assert backfill_skip_existing(session, other, http, book_info=lookup) == 0
    assert http.calls == []


def test_api_base_comes_from_environment(session, user, http, monkeypatch):
    monkeypatch.setenv("READWISE_API_BASE", "http://127.0.0.1:9999/")
    mark_done(session)
    add(session, "k1", "x")
    push_dirty(session, user, http, book_info=lookup)
    assert http.of("post")[0]["url"] == "http://127.0.0.1:9999/api/v2/highlights/"


def test_book_missing_from_library_is_not_sent(session, user, http):
    mark_done(session, 99)
    add(session, "k1", "x", book=99)
    result = push_dirty(session, user, http, book_info=lookup)
    assert not http.of("post") and result.created == 0


def test_token_never_logged(session, user, http, caplog):
    mark_done(session)
    add(session, "k1", "A secret passage", note="a private note")
    add(session, "k2", "second passage", xp0=xp(2, 2))
    gone = add(session, "k3", "third passage", xp0=xp(2, 3))
    gone.readwise_id = "5"
    session.commit()
    delete_from_side(session, USER, BOOK, "kobo", "k3", T0 + timedelta(hours=1))
    session.commit()
    http.routes[("delete", "/api/v2/highlights/5/")] = FakeResponse(500, None)
    with caplog.at_level(logging.DEBUG):
        push_dirty(session, user, http, book_info=lookup)
        http.routes[("delete", "/api/v2/highlights/5/")] = FakeResponse(429, None, {"Retry-After": "9"})
        readwise._retry_not_before.clear()
        push_dirty(session, user, http, book_info=lookup)
        readwise._retry_not_before.clear()
        http.routes[("post", "/api/v2/highlights/")] = lambda call: (_ for _ in ()).throw(
            requests.ConnectionError(f"failed with Authorization: Token {TOKEN}"))
        http.routes[("delete", "/api/v2/highlights/5/")] = FakeResponse(204)
        push_dirty(session, user, http, book_info=lookup)
    assert caplog.records
    for text in (TOKEN, "A secret passage", "a private note", "second passage", "third passage"):
        assert text not in caplog.text


def test_schedule_registers_job_only_when_enabled(monkeypatch):
    from cps import schedule
    from cps.progress_syncing import settings

    class FakeScheduler:
        def __init__(self):
            self.jobs = []

        def schedule_task(self, task, user=None, name=None, hidden=False, trigger=None):
            self.jobs.append((task, name, hidden, trigger))

    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: False)
    off = FakeScheduler()
    schedule._schedule_readwise_highlights(off, timezone.utc)
    assert off.jobs == []

    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: True)
    on = FakeScheduler()
    schedule._schedule_readwise_highlights(on, timezone.utc)
    (task, name, hidden, trigger), = on.jobs
    assert name == "readwise highlights" and trigger.interval == timedelta(minutes=5)
    assert task().name == "Readwise highlights"


def test_task_pushes_each_user_with_a_token_and_respects_the_setting(monkeypatch):
    from cps import db
    from cps.progress_syncing import settings
    from cps.tasks import readwise_highlights

    pushed = []

    class FakeCalibre:
        def __init__(self, *args, **kwargs):
            self.session = type("S", (), {"close": lambda self: None})()

    class FakeSession:
        def query(self, *a):
            return self

        def filter(self, *a):
            return self

        def all(self):
            return [type("U", (), {"id": 1, "readwise_token": "t"})()]

        def rollback(self):
            pass

        def remove(self):
            pass

    monkeypatch.setattr(ub, "get_new_session_instance", lambda: FakeSession())
    monkeypatch.setattr(db, "CalibreDB", FakeCalibre)
    monkeypatch.setattr(readwise, "push_dirty",
                        lambda session, user, http=None, book_info=None: pushed.append((user.id, callable(book_info))))

    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: False)
    task = readwise_highlights.TaskReadwiseHighlights()
    task.run(None)
    assert pushed == []

    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: True)
    task = readwise_highlights.TaskReadwiseHighlights()
    task.run(None)
    assert pushed == [(1, True)]
    assert task.error is None or task.error == ""
