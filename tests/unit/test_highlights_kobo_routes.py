# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Route tests for the Kobo reading-services endpoints that serve highlight sync."""

import json
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from flask import Blueprint, Flask, g
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import kobo, readingservices, ub
from cps.progress_syncing import settings
from cps.progress_syncing.highlights import kobo_side
from cps.progress_syncing.highlights.store import (
    Highlight, HighlightBookState, HighlightDevice, KoboOpaqueAnnotation, upsert_from_side,
)
from cps.progress_syncing.position_bridge import service as bridge_service

from .position_bridge_books import CH1, CH2, kepubify_like, write_book
from .test_highlights_kobo_side import CROSS_CHAPTER, FIRMWARE, UA, annotation, ch2_xpointers, dogear_entry

REAL_EVER = settings.highlight_sync_ever_enabled
REAL_ON = settings.is_highlight_sync_enabled

USER, BOOK = 3, 1
UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"      # a CWA book
STORE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"     # a Kobo store book
KOBO_ID = "0b3a3c4e-7d5f-4c11-9f3e-2b1f0a9c8d71"
HEADERS = {"User-Agent": UA, "Authorization": "Bearer kobo-token", "Cookie": "session=abc"}
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeKobo:
    """Stands in for requests.request: records calls and answers with a canned response."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.body = b"[]"
        self.raises = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        resp = requests.Response()
        resp.status_code = self.status
        resp._content = self.body
        resp.headers["Content-Type"] = "application/json"
        return resp


class FakeWsgi:
    def __init__(self, wsgi_app, proxied=False):
        self.wsgi_app = wsgi_app
        self.is_proxied = proxied

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)


class FakeCalibre:
    def __init__(self):
        self.raises = None

    def get_book_by_uuid(self, book_uuid):
        if self.raises:
            raise self.raises
        if book_uuid == UUID:
            return SimpleNamespace(id=BOOK, title="t", identifiers=[], data=[], path="p")
        return None


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    monkeypatch.setattr(readingservices, "_known_books", {})
    monkeypatch.setattr(settings, "_ever_seen", set())
    monkeypatch.setattr(kobo_side, "_failed_patches", set())


@pytest.fixture
def app():
    app = Flask(__name__)
    web = Blueprint("web", __name__)
    web.add_url_rule("/", "index", lambda: "index")
    app.register_blueprint(web)
    app.register_blueprint(kobo.kobo)
    app.register_blueprint(readingservices.readingservices_api_v3)
    app.register_blueprint(readingservices.readingservices_userstorage)
    app.wsgi_app = FakeWsgi(app.wsgi_app)
    return app


@pytest.fixture
def cfg(monkeypatch):
    config = SimpleNamespace(
        config_hardcover_annotations_sync=False,
        config_kobo_sync=True,
        config_kobo_proxy=True,
        config_external_port=None,
    )
    monkeypatch.setattr(readingservices, "config", config)
    monkeypatch.setattr(kobo, "config", config)
    return config


@pytest.fixture
def kobo_cloud(monkeypatch):
    fake = FakeKobo()
    monkeypatch.setattr(requests, "request", fake)
    return fake


@pytest.fixture
def session(monkeypatch):
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="jcureton", email="j@example.test"))
    s.commit()
    monkeypatch.setattr(ub, "session", s)
    yield s
    s.close()


@pytest.fixture
def user(monkeypatch):
    u = SimpleNamespace(is_authenticated=True, id=USER, name="jcureton", hardcover_token=None)
    monkeypatch.setattr(readingservices, "current_user", u)
    monkeypatch.setattr(kobo, "current_user", u)
    return u


@pytest.fixture
def calibre(monkeypatch):
    fake = FakeCalibre()
    monkeypatch.setattr(readingservices, "calibre_db", fake)
    return fake


@pytest.fixture
def books(tmp_path, monkeypatch):
    epub = write_book(tmp_path / "b.epub", {"ch1.xhtml": CH1, "ch2.xhtml": CH2})
    kepub = write_book(
        tmp_path / "b.kepub", {"ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(CH2)})
    monkeypatch.setattr(bridge_service, "library_files", lambda book_id: (epub, kepub))
    return epub, kepub


@pytest.fixture
def flags(monkeypatch):
    state = {"on": True, "ever": True, "firmware": {FIRMWARE}}
    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: state["on"])
    monkeypatch.setattr(settings, "highlight_sync_ever_enabled", lambda: state["ever"])
    monkeypatch.setattr(settings, "tested_kobo_firmware", lambda: set(state["firmware"]))
    return state


@pytest.fixture
def client(app, cfg, kobo_cloud, session, user, calibre, books, flags):
    return app.test_client()


def no_proxy(monkeypatch):
    def fail(**kwargs):
        raise AssertionError(f"proxied to Kobo: {kwargs.get('url')}")
    monkeypatch.setattr(requests, "request", fail)


def patch_body(*entries, deleted=None):
    body = {"updatedAnnotations": list(entries)}
    if deleted is not None:
        body["deletedAnnotationIds"] = deleted
    return body


def etag_version(session):
    state = session.query(HighlightBookState).filter_by(user_id=USER, book_id=BOOK).first()
    return state.kobo_etag_version if state else 0


def init_resources(app, monkeypatch, proxied, kobo_proxy):
    """Call HandleInitRequest (minus the Kobo-token check) and return its Resources."""
    app.wsgi_app.is_proxied = proxied
    monkeypatch.setattr(kobo, "make_request_to_kobo_store", lambda *a, **k: SimpleNamespace(json=lambda: {
        "Resources": {"reading_services_host": "https://readingservices.kobo.com", "image_host": "x"}}))
    with app.test_request_context("/kobo/tok/v1/initialization", base_url="http://cwa.test:8083",
                                  headers={"User-Agent": UA}):
        g.auth_token = "tok"
        resp = kobo.HandleInitRequest.__wrapped__()
    return resp.get_json()["Resources"]


@pytest.mark.unit
class TestPatch:
    def test_patch_records_and_answers_204(self, client, session, monkeypatch):
        no_proxy(monkeypatch)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 204 and r.data == b""
        row = session.query(Highlight).one()
        assert (row.kobo_id, row.book_id, row.kobo_start_path) == (KOBO_ID, BOOK, r"span#kobo\.4\.1")
        assert row.xp0 is not None

    def test_empty_timestamp_create(self, client, session):
        before = datetime.now(timezone.utc)
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation(modified="")),
                     headers=HEADERS)
        row = session.query(Highlight).one()
        assert row.updated_at.replace(tzinfo=timezone.utc) >= before.replace(microsecond=0)

    def test_patch_delete_marks_deleted(self, client, session):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json={"deletedAnnotationIds": [KOBO_ID]},
                         headers=HEADERS)
        assert r.status_code == 204
        assert session.query(Highlight).one().deleted_at is not None

    def test_empty_body_is_a_no_op(self, client, session):
        r = client.patch(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 204
        assert session.query(Highlight).count() == 0

    def test_store_failure_answers_503(self, client, session, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(kobo_side, "apply_kobo_patch", boom)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 503
        assert session.query(Highlight).count() == 0

    def test_patch_records_in_safe_mode(self, client, session, flags):
        flags["on"] = False
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 204 and session.query(Highlight).count() == 1

    def test_hardcover_still_called_when_enabled(self, client, cfg, session, monkeypatch):
        cfg.config_hardcover_annotations_sync = True
        seen = []
        monkeypatch.setattr(readingservices, "process_annotation_for_sync",
                            lambda annotation, **kw: seen.append(annotation["id"]))
        monkeypatch.setattr(readingservices, "EpubProgressCalculator", lambda book: None)
        no_proxy(monkeypatch)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 204
        assert seen == [KOBO_ID]
        assert session.query(Highlight).count() == 1

    def test_hardcover_not_called_when_disabled(self, client, session, monkeypatch):
        monkeypatch.setattr(readingservices, "process_annotation_for_sync",
                            lambda **kw: pytest.fail("hardcover called"))
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 204

    @pytest.mark.parametrize("body", [
        [annotation()],
        "just a string",
        {"updatedAnnotations": [annotation(), "junk"]},
        {"updatedAnnotations": [{k: v for k, v in annotation().items() if k != "id"}]},
        {"updatedAnnotations": annotation()},
        {"deletedAnnotationIds": [KOBO_ID, None]},
    ])
    def test_unfit_body_gets_400_and_stores_nothing(self, client, session, body):
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=body, headers=HEADERS)
        assert r.status_code == 400
        assert session.query(Highlight).count() == 0
        assert session.query(KoboOpaqueAnnotation).count() == 0

    def test_unreadable_json_gets_400(self, client, session):
        r = client.patch(f"/api/v3/content/{UUID}/annotations", data=b"{not json",
                         headers={**HEADERS, "Content-Type": "application/json"})
        assert r.status_code == 400

    def test_dogear_is_stored_and_echoed(self, client, session, monkeypatch):
        r = client.patch(f"/api/v3/content/{UUID}/annotations",
                         json=patch_body(annotation(), dogear_entry()), headers=HEADERS)
        assert r.status_code == 204
        no_proxy(monkeypatch)
        body = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS).get_json()
        assert dogear_entry() in body["annotations"] and len(body["annotations"]) == 2

    def test_store_failure_log_survives_an_odd_body(self, client, session, monkeypatch):
        monkeypatch.setattr(kobo_side, "kobo_patch_error", lambda body: None)
        r = client.patch(f"/api/v3/content/{UUID}/annotations",
                         json={"updatedAnnotations": 5, "deletedAnnotationIds": "x"}, headers=HEADERS)
        assert r.status_code == 503

    def test_patch_records_firmware(self, client, session):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert session.query(HighlightDevice).one().kobo_firmware == FIRMWARE


@pytest.mark.unit
class TestGet:
    def test_get_for_cwa_book_never_proxies(self, client, monkeypatch):
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations?limit=100", headers={"If-None-Match": 'W/"x"', **HEADERS})
        assert r.status_code == 200 and r.get_json() == {"annotations": [], "nextPageOffsetToken": None}
        assert r.headers["ETag"] == 'W/"cwa-0"'

    def test_get_ignores_matching_if_none_match(self, client, session):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers={"If-None-Match": 'W/"cwa-1"', **HEADERS})
        assert r.status_code == 200 and len(r.get_json()["annotations"]) == 1

    def test_get_serves_full_set_including_readest_origin(self, client, session, books, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        upsert_from_side(session, USER, BOOK, "readest", "rk-1",
                         {"text": "The", "xp0": ch2_xpointers(books, 0, 3)[0],
                          "xp1": ch2_xpointers(books, 0, 3)[1]}, T0)
        session.commit()
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations?limit=100", headers=HEADERS)
        assert r.status_code == 200
        ids = [e["id"] for e in r.get_json()["annotations"]]
        readest = session.query(Highlight).filter_by(readest_key="rk-1").one()
        assert ids == [readest.kobo_id, KOBO_ID] or ids == [KOBO_ID, readest.kobo_id]
        assert readest.kobo_id is not None and readest.kobo_start_char == 0
        assert r.headers["ETag"] == f'W/"cwa-{etag_version(session)}"'

    def test_untranslatable_readest_highlight_omitted_and_logged(self, client, session, caplog):
        row = upsert_from_side(session, USER, BOOK, "readest", "rk-x",
                               {"text": "secret words", "xp0": CROSS_CHAPTER[0], "xp1": CROSS_CHAPTER[1]}, T0)
        session.commit()
        with caplog.at_level(logging.WARNING):
            r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200 and r.get_json()["annotations"] == []
        assert str(row.id) in caplog.text and "secret words" not in caplog.text

    def test_untested_firmware_serves_only_the_stored_set(self, client, session, books, flags):
        flags["firmware"] = set()
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        upsert_from_side(session, USER, BOOK, "readest", "rk-1",
                         {"text": "The", "xp0": ch2_xpointers(books, 0, 3)[0],
                          "xp1": ch2_xpointers(books, 0, 3)[1]}, T0)
        session.commit()
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert [e["id"] for e in r.get_json()["annotations"]] == [KOBO_ID]
        assert session.query(Highlight).filter_by(readest_key="rk-1").one().kobo_id is None

    def test_safe_mode_serves_the_stored_set(self, client, session, books, flags, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        flags["on"] = False
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200 and [e["id"] for e in r.get_json()["annotations"]] == [KOBO_ID]

    def test_exception_while_building_falls_back_to_stored_ranges(self, client, session, books, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        upsert_from_side(session, USER, BOOK, "readest", "rk-1",
                         {"text": "The", "xp0": ch2_xpointers(books, 0, 3)[0],
                          "xp1": ch2_xpointers(books, 0, 3)[1]}, T0)
        session.commit()

        def missing(book_id):
            raise FileNotFoundError("book file missing")
        monkeypatch.setattr(bridge_service, "library_files", missing)
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200
        assert [e["id"] for e in r.get_json()["annotations"]] == [KOBO_ID]
        assert r.get_json()["nextPageOffsetToken"] is None
        assert r.headers["ETag"].endswith('-pending"')
        assert session.query(Highlight).filter_by(readest_key="rk-1").one().kobo_id is None

    def test_translation_crash_is_untranslatable_not_fatal(self, client, session, books, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        upsert_from_side(session, USER, BOOK, "readest", "rk-1",
                         {"text": "The", "xp0": ch2_xpointers(books, 0, 3)[0],
                          "xp1": ch2_xpointers(books, 0, 3)[1]}, T0)
        session.commit()

        def crash(*a):
            raise KeyError("malformed chapter")
        monkeypatch.setattr(kobo_side, "xpointers_to_kobo_range", crash)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200 and [e["id"] for e in r.get_json()["annotations"]] == [KOBO_ID]

    def test_session_failure_falls_back_to_a_fresh_session(self, client, session, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        real = kobo_side.kobo_annotation_set

        def flaky(s, *a, **k):
            if s is session:
                raise RuntimeError("session broken")
            return real(s, *a, **k)
        monkeypatch.setattr(kobo_side, "kobo_annotation_set", flaky)
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200 and [e["id"] for e in r.get_json()["annotations"]] == [KOBO_ID]

    def test_total_failure_still_answers_200(self, client, session, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("no database")
        monkeypatch.setattr(kobo_side, "kobo_annotation_set", boom)
        monkeypatch.setattr(readingservices, "_FALLBACK_RETRY_DELAYS", (0, 0, 0))
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200
        assert r.get_json() == {"annotations": [], "nextPageOffsetToken": None}

    def test_get_records_firmware(self, client, session):
        client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert session.query(HighlightDevice).one().kobo_firmware == FIRMWARE

    def test_lookup_failure_for_a_known_book_serves_the_stored_set(self, client, session, calibre, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation(), dogear_entry()),
                     headers=HEADERS)
        calibre.raises = RuntimeError("calibre db locked")
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200
        assert sorted(e["id"] for e in r.get_json()["annotations"]) == sorted([KOBO_ID, dogear_entry()["id"]])
        assert r.headers["ETag"] == kobo_side.FALLBACK_ETAG

    def test_lookup_failure_for_a_never_seen_uuid_is_503(self, client, calibre, monkeypatch):
        calibre.raises = RuntimeError("calibre db locked")
        no_proxy(monkeypatch)
        assert client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS).status_code == 503

    def test_unauthenticated_get_for_cwa_book_is_not_proxied(self, client, user, monkeypatch):
        user.is_authenticated = False
        no_proxy(monkeypatch)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 401


@pytest.mark.unit
class TestCheckForChanges:
    def test_checkforchanges_names_book_only_when_etag_differs_and_firmware_tested(self, client, session, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        no_proxy(monkeypatch)
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"0"'}],
                        headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, [UUID])
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"cwa-1"'}],
                        headers=HEADERS)
        assert r.get_json() == []

    def test_first_sync_names_only_books_with_cwa_content(self, client, session, calibre, monkeypatch):
        other = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        real = calibre.get_book_by_uuid
        monkeypatch.setattr(calibre, "get_book_by_uuid",
                            lambda u: SimpleNamespace(id=7) if u == other else real(u))
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        no_proxy(monkeypatch)
        batch = [{"ContentId": UUID, "etag": 'W/"0"'}, {"ContentId": other, "etag": 'W/"0"'}]
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == [UUID]

    def test_checkforchanges_empty_when_firmware_untested(self, client, session, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        no_proxy(monkeypatch)
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"0"'}],
                        headers={**HEADERS, "User-Agent": "(Kobo Touch 0383/4.39.1)"})
        assert r.get_json() == []

    def test_checkforchanges_empty_in_safe_mode(self, client, session, flags, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        flags["on"] = False
        no_proxy(monkeypatch)
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"0"'}],
                        headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, [])

    def test_mixed_batch_proxies_only_store_entries_and_unions(self, client, session, kobo_cloud):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        kobo_cloud.body = json.dumps([STORE]).encode()
        batch = [{"ContentId": UUID, "etag": 'W/"0"'}, {"ContentId": STORE, "etag": 'W/"0"'}]
        r = client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS)
        assert r.status_code == 200 and sorted(r.get_json()) == sorted([UUID, STORE])
        (call,) = kobo_cloud.calls
        assert call["url"] == "https://readingservices.kobo.com/api/v3/content/checkforchanges"
        assert json.loads(call["data"]) == [{"ContentId": STORE, "etag": 'W/"0"'}]
        assert "Content-Length" not in call["headers"]
        assert "Cookie" not in call["headers"]

    def test_mixed_batch_answers_cwa_part_when_proxy_fails(self, client, session, kobo_cloud):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        batch = [{"ContentId": UUID, "etag": 'W/"0"'}, {"ContentId": STORE, "etag": 'W/"0"'}]
        kobo_cloud.status, kobo_cloud.body = 401, b'{"error": "unauthorized"}'
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == [UUID]
        kobo_cloud.status, kobo_cloud.body = 200, b'{"not": "a list"}'
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == [UUID]
        kobo_cloud.raises = requests.exceptions.ConnectionError("down")
        r = client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, [UUID])

    def test_success_never_rolls_back_the_shared_session(self, client, session, monkeypatch):
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        rollbacks = []
        monkeypatch.setattr(session, "rollback", lambda: rollbacks.append(1))
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"0"'}],
                        headers=HEADERS)
        assert r.get_json() == [UUID]
        client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert rollbacks == []

    def test_a_book_whose_patch_failed_is_not_named_until_a_patch_succeeds(self, client, session, monkeypatch):
        """Named after a failed PATCH, the Kobo's GET would delete the highlights that PATCH carried."""
        real = kobo_side.apply_kobo_patch

        def boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(kobo_side, "apply_kobo_patch", boom)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 503
        # Readest changes the book meanwhile, so its version moves past the device's ETag.
        upsert_from_side(session, USER, BOOK, "readest", "rk-1",
                         {"text": "From Readest", "xp0": CROSS_CHAPTER[0], "xp1": CROSS_CHAPTER[1]}, T0)
        session.commit()
        assert etag_version(session) == 1
        batch = [{"ContentId": UUID, "etag": 'W/"0"'}]
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == []
        # A rejected body counts as failed too.
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json={"updatedAnnotations": "x"}, headers=HEADERS)
        assert r.status_code == 400
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == []
        monkeypatch.setattr(kobo_side, "apply_kobo_patch", real)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 204
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == [UUID]

    def test_all_cwa_batch_is_not_proxied(self, client, session, monkeypatch):
        no_proxy(monkeypatch)
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"cwa-0"'}],
                        headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, [])

    def test_store_only_batch_passes_through(self, client, kobo_cloud):
        kobo_cloud.body = json.dumps([STORE]).encode()
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": STORE, "etag": 'W/"0"'}],
                        headers=HEADERS)
        assert r.get_json() == [STORE]
        assert len(kobo_cloud.calls) == 1

    def test_safe_mode_still_proxies_store_books(self, client, flags, kobo_cloud):
        flags["on"] = False
        kobo_cloud.body = json.dumps([STORE]).encode()
        batch = [{"ContentId": UUID, "etag": 'W/"0"'}, {"ContentId": STORE, "etag": 'W/"0"'}]
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == [STORE]

    def test_without_store_proxy_store_entries_are_not_proxied(self, client, cfg, session, monkeypatch):
        cfg.config_kobo_proxy = False
        client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        no_proxy(monkeypatch)
        batch = [{"ContentId": UUID, "etag": 'W/"0"'}, {"ContentId": STORE, "etag": 'W/"0"'}]
        assert client.post("/api/v3/content/checkforchanges", json=batch, headers=HEADERS).get_json() == [UUID]


@pytest.mark.unit
class TestStoreBooksAndOtherPaths:
    def test_store_book_requests_still_proxied(self, client, kobo_cloud, session):
        kobo_cloud.body = b'{"annotations": [], "nextPageOffsetToken": null}'
        r = client.get(f"/api/v3/content/{STORE}/annotations?limit=100", headers=HEADERS)
        assert r.get_json() == {"annotations": [], "nextPageOffsetToken": None}
        client.patch(f"/api/v3/content/{STORE}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert [c["method"] for c in kobo_cloud.calls] == ["GET", "PATCH"]
        assert kobo_cloud.calls[0]["url"].endswith(f"/api/v3/content/{STORE}/annotations?limit=100")
        assert session.query(Highlight).count() == 0

    @pytest.mark.parametrize("path", ["/api/UserStorage/Metadata", "/api/internal/notebooks",
                                      "/api/internal/notebooks/abc?x=1"])
    def test_userstorage_and_notebooks_proxied_when_on(self, client, kobo_cloud, path):
        kobo_cloud.body = b'{"from": "kobo"}'
        r = client.get(path, headers=HEADERS)
        assert r.get_json() == {"from": "kobo"}
        (call,) = kobo_cloud.calls
        assert call["url"] == "https://readingservices.kobo.com" + path
        assert "Cookie" not in call["headers"]

    @pytest.mark.parametrize("path", ["/api/UserStorage/Metadata", "/api/internal/notebooks"])
    def test_userstorage_and_notebooks_stubbed_without_store_proxy(self, client, cfg, kobo_cloud, path):
        cfg.config_kobo_proxy = False
        assert client.get(path, headers=HEADERS).status_code == 200
        assert kobo_cloud.calls == []


@pytest.mark.unit
class TestInitAndNeverOn:
    @pytest.mark.parametrize("proxied", [False, True])
    @pytest.mark.parametrize("kobo_proxy", [False, True])
    @pytest.mark.parametrize("on,ever", [(True, True), (False, True)])
    def test_init_points_reading_services_at_cwa_when_on_or_ever(
            self, app, cfg, session, user, flags, monkeypatch, proxied, kobo_proxy, on, ever):
        flags["on"], flags["ever"] = on, ever
        cfg.config_kobo_proxy = kobo_proxy
        resources = init_resources(app, monkeypatch, proxied, kobo_proxy)
        assert resources["reading_services_host"].startswith("http://cwa.test")
        assert session.query(HighlightDevice).one().kobo_firmware == FIRMWARE

    @pytest.mark.parametrize("proxied", [False, True])
    def test_off_never_on_init_is_unchanged(self, app, cfg, session, user, flags, monkeypatch, proxied):
        flags["on"], flags["ever"] = False, False
        resources = init_resources(app, monkeypatch, proxied, kobo_proxy=True)
        assert resources["reading_services_host"] == "https://readingservices.kobo.com"
        cfg.config_kobo_proxy = False
        resources = init_resources(app, monkeypatch, proxied, kobo_proxy=False)
        assert resources["reading_services_host"].startswith("http://cwa.test")
        assert session.query(HighlightDevice).count() == 0

    def test_off_never_on_routes_are_unchanged(self, client, flags, kobo_cloud, session):
        flags["on"], flags["ever"] = False, False
        r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"0"'}],
                        headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, [])
        r = client.get(f"/api/v3/content/{UUID}/annotations?limit=100", headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, {"data": [], "totalResults": 0})
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, {"data": [], "totalResults": 0})
        assert client.get("/api/UserStorage/Metadata", headers=HEADERS).get_json() == {}
        assert client.get("/api/internal/notebooks", headers=HEADERS).get_json() == {"data": [], "totalResults": 0}
        assert kobo_cloud.calls == []
        assert session.query(Highlight).count() == 0
        assert session.query(HighlightDevice).count() == 0

    def test_off_never_on_views_still_proxy_when_called_directly(self, app, cfg, flags, kobo_cloud, session):
        # The stub preempts these views in a full app; the views themselves must keep today's behaviour.
        flags["on"], flags["ever"] = False, False
        kobo_cloud.body = b'["from-kobo"]'
        with app.test_request_context(f"/api/v3/content/{UUID}/annotations", method="GET"):
            assert readingservices.handle_annotations(UUID).get_json() == ["from-kobo"]
        with app.test_request_context("/api/v3/content/checkforchanges", method="POST", json=[{"ContentId": UUID}]):
            assert readingservices.handle_check_for_changes().get_json() == ["from-kobo"]
        assert len(kobo_cloud.calls) == 2

    def test_non_reading_services_requests_never_read_the_settings(self, app, cfg, monkeypatch):
        def fail():
            raise AssertionError("settings read")
        monkeypatch.setattr(settings, "is_highlight_sync_enabled", fail)
        monkeypatch.setattr(settings, "highlight_sync_ever_enabled", fail)
        assert app.test_client().get("/").status_code == 200


@pytest.mark.unit
class TestRoutingDecision:
    def test_settings_failing_after_ever_was_read_still_route_to_cwa(self, client, session, monkeypatch, tmp_path):
        import sqlite3
        monkeypatch.setenv("CWA_DB_PATH", str(tmp_path))
        con = sqlite3.connect(tmp_path / "cwa.db")
        con.execute("CREATE TABLE cwa_settings (kobo_readest_highlight_sync, kobo_readest_highlight_sync_ever)")
        con.execute("INSERT INTO cwa_settings VALUES (0, 1)")
        con.commit()
        con.close()
        monkeypatch.setattr(settings, "highlight_sync_ever_enabled", REAL_EVER)
        monkeypatch.setattr(settings, "is_highlight_sync_enabled", REAL_ON)
        assert settings.highlight_sync_ever_enabled() is True
        (tmp_path / "cwa.db").unlink()

        def broken(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(settings, "_read_row_directly", broken)
        monkeypatch.setitem(sys.modules, "cwa_db", None)  # the CWA_DB() fallback can't load either
        no_proxy(monkeypatch)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert r.status_code == 204 and session.query(Highlight).count() == 1
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert r.status_code == 200 and len(r.get_json()["annotations"]) == 1

    def test_settings_failing_before_ever_was_seen_answer_as_today(self, client, session, kobo_cloud,
                                                                   monkeypatch, tmp_path):
        monkeypatch.setenv("CWA_DB_PATH", str(tmp_path))  # no cwa.db here
        monkeypatch.setattr(settings, "highlight_sync_ever_enabled", REAL_EVER)
        monkeypatch.setattr(settings, "is_highlight_sync_enabled", REAL_ON)
        monkeypatch.setitem(sys.modules, "cwa_db", None)
        r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, {"data": [], "totalResults": 0})
        assert kobo_cloud.calls == []

    @pytest.mark.parametrize("method", ["GET", "PATCH", "POST"])
    def test_a_flipping_read_cannot_proxy_a_cwa_book(self, client, session, monkeypatch, method):
        answers = iter([True] + [False] * 10)
        calls = []

        def flipping():
            calls.append(1)
            return next(answers)
        monkeypatch.setattr(kobo_side, "highlight_routing_active", flipping)
        no_proxy(monkeypatch)
        if method == "GET":
            r = client.get(f"/api/v3/content/{UUID}/annotations", headers=HEADERS)
            assert r.status_code == 200 and "annotations" in r.get_json()
        elif method == "PATCH":
            r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()),
                             headers=HEADERS)
            assert r.status_code == 204 and session.query(Highlight).count() == 1
        else:
            r = client.post("/api/v3/content/checkforchanges", json=[{"ContentId": UUID, "etag": 'W/"0"'}],
                            headers=HEADERS)
            assert (r.status_code, r.get_json()) == (200, [])
        assert len(calls) == 1

    def test_a_raising_read_answers_as_today(self, client, session, kobo_cloud, monkeypatch):
        def boom():
            raise RuntimeError("settings unreadable")
        monkeypatch.setattr(kobo_side, "highlight_routing_active", boom)
        r = client.patch(f"/api/v3/content/{UUID}/annotations", json=patch_body(annotation()), headers=HEADERS)
        assert (r.status_code, r.get_json()) == (200, {"data": [], "totalResults": 0})
        assert kobo_cloud.calls == [] and session.query(Highlight).count() == 0
