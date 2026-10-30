# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Route tests for the BookOrbit endpoints Readest syncs highlights with.

`FakeReadest` is a Python transcription of Readest's BookOrbit client
(readest-app/src/services/bookorbit: BookOrbitClient.ts, notesPass.ts,
annotationExchange.ts, noteMapping.ts), so the requests and the way answers are
applied are Readest's, including how it derives keys for server-added notes.
"""

import hashlib
import importlib
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import cps
from cps import ub
from cps.progress_syncing import settings
from cps.progress_syncing.highlights import bookorbit
from cps.progress_syncing.highlights.store import (
    Highlight, HighlightDevice, ensure_highlight_tables, upsert_from_side,
)
kosync = importlib.import_module("cps.progress_syncing.protocols.kosync")  # the package re-exports the blueprint as `kosync`

pytestmark = pytest.mark.unit

USER, BOOK, OTHER_BOOK = 3, 1, 2
HASH = "0123456789abcdef0123456789abcdef"          # Readest's partial MD5 of the book
OTHER_HASH = "fedcba9876543210fedcba9876543210"
SYNC_KEY_MD5 = hashlib.md5(b"readest-sync-key").hexdigest()
DEVICE = "8d6c2a1e-readest-iphone"
PREFIX = "/api/v1/koreader"

P = "/body/DocFragment[6]/body/section/p[{n}]/text().{off}"


def xp(n, off):
    return P.format(n=n, off=off)


# --- Readest's noteMapping.ts -------------------------------------------------

def ko_datetime(ms: int) -> str:
    """formatKoDatetime: UTC, to the second."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_ko_datetime(dt: str) -> int:
    """parseKoDatetime."""
    return int(datetime.strptime(dt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)


def bookorbit_key(dt: str, pos: str) -> str:
    """bookOrbitKey."""
    return hashlib.md5(f"{dt}|{pos}".encode()).hexdigest()


def bookorbit_note_id(book_hash, pos0, pos1=None) -> str:
    """bookOrbitNoteId."""
    return hashlib.md5(f"ko:{book_hash}:annotation:{pos0}:{pos1 or ''}".encode()).hexdigest()[:7]


KO_TO_READEST_COLOR = {"yellow": "yellow", "red": "red", "green": "green", "blue": "blue",
                       "purple": "violet", "orange": "#ff8800", "cyan": "#00bcd4",
                       "olive": "#808000", "gray": "#9e9e9e"}
READEST_TO_KO_COLOR = {v: k for k, v in KO_TO_READEST_COLOR.items()}


class FakeReadest:
    """One Readest install's BookOrbit pass for one open book."""

    def __init__(self, client, book_hash=HASH, device_id=DEVICE, username="jcureton",
                 userkey=SYNC_KEY_MD5):
        self.client = client
        self.hash = book_hash
        self.device_id = device_id
        self.headers = {"X-Auth-User": username, "X-Auth-Key": userkey,
                        "Accept": "application/json"}
        self.notes = {}           # BookNote by id
        self.identities = {}      # note id -> {datetime, serverId}  (BookOrbitSyncStore)
        self.watermark = 0
        # An hour behind the server: Readest's own edits postdate the seeded Kobo
        # highlights (two hours back), and server-side edits made "now" postdate them.
        self.clock = int(time.time() * 1000) - 3600 * 1000
        self.requests = []        # (endpoint, body) sent
        self.responses = []       # (endpoint, status, json)

    def now(self):
        self.clock += 1000
        return self.clock

    # BookOrbitClient.postJson: device fields on every POST body.
    def post(self, endpoint, payload):
        body = {"deviceId": self.device_id, "deviceModel": "iPhone",
                "pluginVersion": "readest-0.10.0", "deviceTime": ko_datetime(self.clock), **payload}
        self.requests.append((endpoint, body))
        r = self.client.post(PREFIX + endpoint, json=body, headers=self.headers)
        self.responses.append((endpoint, r.status_code, r.get_json(silent=True)))
        assert r.status_code == 200, (endpoint, r.status_code, r.get_data(as_text=True))
        return r.get_json()

    def highlight(self, pos0, pos1, text, note=None, color="yellow"):
        ms = self.now()
        nid = uuid.uuid4().hex[:7]
        self.notes[nid] = {"id": nid, "type": "annotation", "xpointer0": pos0, "xpointer1": pos1,
                           "text": text, "note": note or "", "color": color, "style": "highlight",
                           "createdAt": ms, "updatedAt": ms, "deletedAt": None}
        return nid

    def edit(self, nid, **changes):
        self.notes[nid].update(changes, updatedAt=self.now())

    def delete(self, nid):
        ms = self.now()
        self.notes[nid].update(deletedAt=ms, updatedAt=ms)

    def live(self):
        return [n for n in self.notes.values()
                if n["type"] == "annotation" and not n["deletedAt"] and n["xpointer0"]]

    def identity_of(self, note):
        ident = self.identities.get(note["id"])
        return ident["datetime"] if ident else ko_datetime(note["createdAt"])

    def key_of(self, note):
        return bookorbit_key(self.identity_of(note), note["xpointer0"])

    # noteMapping.noteToKoAnnotation
    def to_ko(self, note):
        ann = {"datetime": self.identity_of(note), "drawer": "lighten", "posFormat": "xpointer",
               "pos0": note["xpointer0"]}
        if note["xpointer1"]:
            ann["pos1"] = note["xpointer1"]
        if note["updatedAt"] > note["createdAt"]:
            ann["datetimeUpdated"] = ko_datetime(note["updatedAt"])
        color = READEST_TO_KO_COLOR.get(note["color"])
        if color:
            ann["color"] = color
        if note["text"]:
            ann["text"] = note["text"]
        if note["note"]:
            ann["note"] = note["note"]
        return ann

    # annotationExchange.buildAnnotationExchangeBook
    def build_request(self):
        keys, key_to_note = [], {}
        for note in self.live():
            dt = self.identity_of(note)
            k = bookorbit_key(dt, note["xpointer0"])
            keys.append({"k": k, "dt": dt})
            key_to_note[k] = note
        changes = [self.to_ko(n) for n in self.live()
                   if n["createdAt"] > self.watermark or n["updatedAt"] > self.watermark]
        return {"hash": self.hash, "keys": keys, "keysComplete": True, "changes": changes}, key_to_note

    # annotationExchange.applyAnnotationResult (resolvePosition always resolves)
    def apply(self, result, key_to_note):
        applied, deleted, identities = [], [], []
        now = self.now()
        for add in result["toApply"]["add"]:
            nid = bookorbit_note_id(self.hash, add["pos0"], add["pos1"])
            # No deletedAt: mergeNotes spreads the upsert over an existing note of the
            # same position-derived id, so a tombstone stays deleted
            # (useBookOrbitNotesSync.ts:139-147).
            upsert = {"id": nid, "type": "annotation", "xpointer0": add["pos0"],
                      "xpointer1": add["pos1"], "text": add["text"], "note": add["note"] or "",
                      "color": KO_TO_READEST_COLOR.get(add["color"]), "style": "highlight",
                      "createdAt": parse_ko_datetime(add["datetime"]), "updatedAt": now}
            existing = self.notes.get(nid)
            if existing is None:
                self.notes[nid] = {"deletedAt": None, **upsert}
            elif existing["updatedAt"] <= now:
                self.notes[nid] = {**existing, **upsert}
            applied.append({"serverId": add["serverId"], "version": add["version"],
                            "status": "applied", "verified": True})
            identities.append((nid, add["datetime"], add["serverId"]))
        for edit in result["toApply"]["edit"]:
            local = key_to_note.get(edit["key"])
            if local is None:
                applied.append({"serverId": edit["serverId"], "version": edit["version"],
                                "status": "failed"})
                continue
            local.update(color=KO_TO_READEST_COLOR.get(edit["color"]) or local["color"],
                         text=edit["text"] or local["text"], note=edit["note"] or "",
                         updatedAt=now)
            applied.append({"serverId": edit["serverId"], "version": edit["version"],
                            "status": "applied"})
        for entry in result["toApply"]["delete"]:
            local = key_to_note.get(entry["key"])
            if local is not None and not local["deletedAt"]:
                local.update(deletedAt=now, updatedAt=now)
            deleted.append({"serverId": entry["serverId"], "status": "applied"})
        return {"hash": result["hash"], "applied": applied, "deleted": deleted}, identities

    class AckLost(Exception):
        pass

    # notesPass.runBookOrbitNotesPass + exchangeNotes / runPhase: chunks of 50
    # changes, keys only on chunk 0, then empty exchanges while `more`.
    def run_pass(self, chunk_size=50, lose_ack=False, before_ack=None):
        """
        One pass. Returns the book's results merged: every response's toApply
        lists concatenated, `more` of the last response, `skippedNoPosition` of
        the first, and `responses` (each ExchangeBookResult).

        lose_ack: the ack request fails, as a network error would; Readest's
        exchange callback throws, so the pass ends before identities are stored
        or the watermark moves (notesPass.ts:165-190, 141).
        before_ack: called between an exchange's answer and its ack.
        """
        match = self.post("/plugin/match-check", {
            "hashes": [self.hash],
            "books": [{"hash": self.hash, "title": "Book", "lastOpen": self.clock // 1000,
                       "source": "current_file"}]})
        if not any(m["hash"] == self.hash for m in match["matches"]):
            return "unmatched"
        request, key_to_note = self.build_request()
        pass_start = self.now()
        results = []

        def exchange_chunk(chunk_request):
            response = self.post("/plugin/annotations/exchange", {"books": [chunk_request]})
            if self.hash in response["unmatched"]:
                return None
            result = next((r for r in response["results"] if r["hash"] == self.hash), None)
            if result is None:
                return False
            results.append(result)
            ack, identities = self.apply(result, key_to_note)
            if before_ack:
                before_ack(result)
            if ack["applied"] or ack["deleted"]:
                if lose_ack:
                    raise FakeReadest.AckLost()
                self.post("/plugin/annotations/exchange-ack", {"books": [ack]})
            for nid, dt, server_id in identities:
                self.identities[nid] = {"datetime": dt, "serverId": server_id}
            return result["more"]

        changes = request["changes"]
        chunks = [changes[i:i + chunk_size] for i in range(0, len(changes), chunk_size)] or [[]]
        try:
            more = False
            for i, chunk in enumerate(chunks):
                more = exchange_chunk({"hash": self.hash, "keys": request["keys"] if i == 0 else [],
                                       "keysComplete": i == 0, "changes": chunk})
                if more is None:
                    return "unmatched"
            follow_ups = 0
            while more:
                follow_ups += 1
                assert follow_ups < 10, "server keeps answering more: true"
                more = exchange_chunk({"hash": self.hash, "keys": [], "keysComplete": False,
                                       "changes": []})
        except FakeReadest.AckLost:
            return "ack lost"
        if not results:
            return None
        self.watermark = pass_start
        return {"hash": self.hash, "more": results[-1]["more"],
                "skippedNoPosition": results[0]["skippedNoPosition"],
                "toApply": {kind: [e for r in results for e in r["toApply"][kind]]
                            for kind in ("add", "edit", "delete")},
                "responses": results}


# --- fixtures -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: True)


@pytest.fixture
def session(monkeypatch):
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="JCureton", email="j@example.test",
                  highlight_sync_key_md5=SYNC_KEY_MD5))
    s.add(ub.User(id=USER + 1, name="nokey", email="n@example.test"))
    s.commit()
    monkeypatch.setattr(ub, "session", s)
    yield s
    s.close()


@pytest.fixture
def books(monkeypatch):
    table = {HASH: BOOK, OTHER_HASH: OTHER_BOOK}

    def lookup(checksum, version=None):
        book_id = table.get(checksum)
        if book_id is None:
            return None, None, None, None, None
        return book_id, "EPUB", f"Book {book_id}", f"path/{book_id}", "koreader"
    monkeypatch.setattr(kosync, "get_book_by_checksum", lookup)
    return table


@pytest.fixture
def client(session, books):
    app = Flask(__name__)
    app.register_blueprint(kosync.kosync)
    app.register_blueprint(bookorbit.bookorbit)
    return app.test_client()


@pytest.fixture
def readest(client):
    return FakeReadest(client)


AUTH = {"X-Auth-User": "jcureton", "X-Auth-Key": SYNC_KEY_MD5}


def exchange(client, books, device=DEVICE, headers=AUTH):
    body = {"deviceId": device, "deviceModel": "iPhone", "pluginVersion": "readest-0.10.0",
            "deviceTime": "2026-10-30 12:00:00", "books": books}
    r = client.post(PREFIX + "/plugin/annotations/exchange", json=body, headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def kobo_highlight(session, kobo_id, pos0, pos1, text, note=None, at=None, **extra):
    at = at or datetime.now(timezone.utc) - timedelta(hours=2)
    fields = {"text": text, "note": note, "xp0": pos0, "xp1": pos1, "updated_at": at,
              "created_at": at, "kobo_chapter": "OEBPS/ch6.xhtml", "kobo_start_path": "span#kobo\\.2\\.1",
              "kobo_start_char": 0, "kobo_end_path": "span#kobo\\.2\\.1", "kobo_end_char": 14,
              "kobo_chapter_progress": 0.1, **extra}
    row = upsert_from_side(session, USER, BOOK, "kobo", kobo_id, fields, at)
    session.commit()
    return row


def rows(session, book_id=BOOK):
    session.expire_all()
    return session.query(Highlight).filter_by(user_id=USER, book_id=book_id).order_by(Highlight.id).all()


# --- auth, discovery, setting ---------------------------------------------------

def test_auth_rejects_wrong_md5_and_accepts_sync_key(client):
    ok = client.get(PREFIX + "/users/auth", headers=AUTH)
    assert ok.status_code == 200 and ok.get_json() == {"authorized": "OK"}
    # username is case-insensitive, like kosync
    assert client.get(PREFIX + "/users/auth",
                      headers={**AUTH, "X-Auth-User": "JCURETON"}).status_code == 200
    wrong = client.get(PREFIX + "/users/auth",
                       headers={**AUTH, "X-Auth-Key": hashlib.md5(b"nope").hexdigest()})
    assert wrong.status_code == 401
    assert client.get(PREFIX + "/users/auth", headers={"X-Auth-User": "jcureton"}).status_code == 401
    assert client.get(PREFIX + "/users/auth",
                      headers={**AUTH, "X-Auth-User": "ghost"}).status_code == 401
    r = client.post(PREFIX + "/plugin/annotations/exchange",
                    json={"deviceId": DEVICE, "books": []},
                    headers={**AUTH, "X-Auth-Key": "0" * 32})
    assert r.status_code == 401


def test_user_without_a_sync_key_is_rejected(client):
    for key in ("", "None", SYNC_KEY_MD5):
        r = client.get(PREFIX + "/users/auth", headers={"X-Auth-User": "nokey", "X-Auth-Key": key})
        assert r.status_code == 401


def test_plugin_version_has_no_capabilities(client):
    r = client.get(PREFIX + "/plugin/version", headers=AUTH)
    assert r.status_code == 200
    body = r.get_json()
    assert body["pluginVersion"] == "cwa-1"
    assert body["capabilities"] == []
    assert isinstance(body["serverVersion"], str)


def test_match_check(client):
    r = client.post(PREFIX + "/plugin/match-check", headers=AUTH, json={
        "deviceId": DEVICE, "hashes": [HASH, "deadbeef"],
        "books": [{"hash": HASH, "title": "Book", "source": "current_file"}]})
    assert r.status_code == 200
    assert r.get_json() == {"matches": [{"hash": HASH, "bookId": BOOK, "bookFileId": BOOK}],
                            "libraryVersion": 1}


def test_endpoints_404_when_setting_off(client, monkeypatch):
    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: False)
    assert client.get(PREFIX + "/users/auth", headers=AUTH).status_code == 404
    assert client.get(PREFIX + "/plugin/version", headers=AUTH).status_code == 404
    for endpoint in ("/plugin/match-check", "/plugin/annotations/exchange",
                     "/plugin/annotations/exchange-ack"):
        r = client.post(PREFIX + endpoint, headers=AUTH, json={"deviceId": DEVICE, "books": []})
        assert r.status_code == 404, endpoint
    assert client.get(PREFIX + f"/syncs/progress/{HASH}", headers=AUTH).status_code == 404
    assert client.put(PREFIX + "/syncs/progress", headers=AUTH, json={}).status_code == 404


def test_second_device_rejected_409(client, session):
    assert exchange(client, [])["results"] == []
    device = session.query(HighlightDevice).filter_by(user_id=USER).one()
    assert device.readest_device_id == DEVICE
    body = {"deviceId": "another-iphone", "books": []}
    for endpoint in ("/plugin/match-check", "/plugin/annotations/exchange",
                     "/plugin/annotations/exchange-ack"):
        r = client.post(PREFIX + endpoint, json=body, headers=AUTH)
        assert r.status_code == 409, endpoint
        assert r.get_json() == {"message": "Highlight sync supports one Readest device per user"}
    session.expire_all()
    assert session.query(HighlightDevice).filter_by(user_id=USER).one().readest_device_id == DEVICE


# --- exchange -------------------------------------------------------------------

def test_unknown_hash_is_unmatched(client, session):
    r = exchange(client, [{"hash": "deadbeef", "keys": [], "keysComplete": True, "changes": []}])
    assert r["unmatched"] == ["deadbeef"] and r["results"] == []
    assert session.query(Highlight).count() == 0


def test_result_has_every_field_readest_reads(client):
    r = exchange(client, [{"hash": HASH, "keys": [], "keysComplete": True, "changes": []}])
    assert r == {"results": [{"hash": HASH, "bookId": BOOK, "more": False, "skippedNoPosition": 0,
                              "toApply": {"add": [], "edit": [], "delete": []}}],
                 "unmatched": []}


def test_change_creates_highlight_with_key(readest, session):
    nid = readest.highlight(xp(2, 0), xp(2, 14), "A first passage", note="mine", color="violet")
    result = readest.run_pass()
    assert result["toApply"] == {"add": [], "edit": [], "delete": []}
    [row] = rows(session)
    note = readest.notes[nid]
    assert row.readest_key == readest.key_of(note)
    assert row.readest_datetime == ko_datetime(note["createdAt"])
    assert (row.xp0, row.xp1, row.text, row.note, row.color) == (
        xp(2, 0), xp(2, 14), "A first passage", "mine", "violet")
    assert row.origin == "readest" and row.kobo_id is None
    # A second pass offers Readest nothing back and changes nothing.
    version = row.version
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}
    assert rows(session)[0].version == version


def test_readest_edit_is_applied(readest, session):
    nid = readest.highlight(xp(2, 0), xp(2, 14), "A first passage")
    readest.run_pass()
    version = rows(session)[0].version
    readest.edit(nid, note="added a note", color="blue")
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}
    [row] = rows(session)
    assert (row.note, row.color) == ("added a note", "blue")
    assert row.version == version + 1


def test_kobo_highlight_offered_as_add_then_acked(readest, session):
    kobo = kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo", note="kobo note")
    result = readest.run_pass()
    [add] = result["toApply"]["add"]
    assert add["serverId"] == kobo.id
    assert (add["pos0"], add["pos1"], add["text"], add["note"]) == (
        xp(3, 0), xp(3, 20), "From the Kobo", "kobo note")
    assert add["posFormat"] == "xpointer"
    assert len(add["datetime"]) == 19 and datetime.strptime(add["datetime"], "%Y-%m-%d %H:%M:%S")
    [row] = rows(session)
    # The ack recorded the key Readest itself derives for the note it created.
    [note] = [n for n in readest.notes.values()]
    assert row.readest_datetime == add["datetime"]
    assert row.readest_key == readest.key_of(note) == bookorbit_key(add["datetime"], xp(3, 0))
    # The next pass (which echoes the note back as a change) deletes nothing,
    # re-offers nothing, and doesn't bump the Kobo's version.
    version = row.version
    second = readest.run_pass()
    assert second["toApply"] == {"add": [], "edit": [], "delete": []}
    [row] = rows(session)
    assert row.deleted_at is None and row.version == version and row.kobo_id == "kobo-1"


def test_untranslatable_kobo_highlight_counts_as_skipped(readest, session):
    kobo_highlight(session, "kobo-1", None, None, "No position")
    kobo_highlight(session, "kobo-2", xp(3, 0), xp(3, 20), "Placed")
    result = readest.run_pass()
    assert result["skippedNoPosition"] == 1
    assert [a["text"] for a in result["toApply"]["add"]] == ["Placed"]


def test_failed_add_is_left_undelivered_and_not_reoffered(client, session, caplog):
    kobo = kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    r = exchange(client, [{"hash": HASH, "keys": [], "keysComplete": True, "changes": []}])
    [add] = r["results"][0]["toApply"]["add"]
    with caplog.at_level(logging.INFO):
        ack = client.post(PREFIX + "/plugin/annotations/exchange-ack", headers=AUTH, json={
            "deviceId": DEVICE, "books": [{"hash": HASH, "deleted": [], "applied": [
                {"serverId": add["serverId"], "version": add["version"], "status": "failed"}]}]})
    assert ack.status_code == 200 and ack.get_json() == {}
    assert rows(session)[0].readest_key is None
    assert str(kobo.id) in caplog.text and "From the Kobo" not in caplog.text
    r = exchange(client, [{"hash": HASH, "keys": [], "keysComplete": True, "changes": []}])
    assert r["results"][0]["toApply"]["add"] == []
    # A later change on the Kobo side offers it again.
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo", note="new",
                   at=datetime.now(timezone.utc))
    r = exchange(client, [{"hash": HASH, "keys": [], "keysComplete": True, "changes": []}])
    assert [a["serverId"] for a in r["results"][0]["toApply"]["add"]] == [kobo.id]


def test_missing_key_deletes_when_complete(readest, session):
    a = readest.highlight(xp(2, 0), xp(2, 14), "first")
    readest.highlight(xp(4, 0), xp(4, 9), "second")
    readest.run_pass()
    readest.delete(a)
    result = readest.run_pass()
    assert result["toApply"] == {"add": [], "edit": [], "delete": []}
    first, second = rows(session)
    assert first.deleted_at is not None and second.deleted_at is None


def test_missing_key_is_not_a_deletion_when_keys_incomplete(client, session, readest):
    readest.highlight(xp(2, 0), xp(2, 14), "first")
    readest.highlight(xp(4, 0), xp(4, 9), "second")
    readest.run_pass()
    exchange(client, [{"hash": HASH, "keys": [], "keysComplete": False, "changes": []}])
    assert all(r.deleted_at is None for r in rows(session))


def test_mass_deletion_is_held(readest, session, caplog):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 6)]
    readest.run_pass()
    versions = [r.version for r in rows(session)]
    for nid in ids:
        readest.delete(nid)
    with caplog.at_level(logging.INFO):
        result = readest.run_pass()
    # Answered normally, so Readest's pass completes and its watermark advances.
    assert result["toApply"] == {"add": [], "edit": [], "delete": []}
    assert result["more"] is False
    assert [r.deleted_at for r in rows(session)] == [None] * 4
    assert [r.version for r in rows(session)] == versions
    assert "held" in caplog.text and "4" in caplog.text
    assert "passage" not in caplog.text


def test_more_than_half_of_at_least_four_is_held(readest, session):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 9)]  # 7
    readest.run_pass()
    for nid in ids[:4]:
        readest.delete(nid)
    readest.run_pass()
    assert all(r.deleted_at is None for r in rows(session))            # 4 of 7: held
    assert sum(r.readest_held for r in rows(session)) == 4


def test_half_is_applied(readest, session):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 10)]  # 8
    readest.run_pass()
    for nid in ids[:4]:
        readest.delete(nid)
    readest.run_pass()
    assert sum(r.deleted_at is not None for r in rows(session)) == 4   # 4 of 8: applied


@pytest.mark.parametrize("count", [2, 3])
def test_deleting_all_of_a_books_highlights_is_held_from_two(readest, session, count):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 2 + count)]
    readest.run_pass()
    for nid in ids:
        readest.delete(nid)
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}
    assert all(r.deleted_at is None and r.readest_held for r in rows(session))


def test_a_single_deletion_always_propagates(readest, session):
    [nid] = [readest.highlight(xp(2, 0), xp(2, 9), "the only passage")]
    readest.run_pass()
    readest.delete(nid)
    readest.run_pass()
    [row] = rows(session)
    assert row.deleted_at is not None and not row.readest_held


@pytest.mark.parametrize("total,deleted,held", [
    (4, 3, True),    # more than half of more than 3
    (5, 3, True),
    (3, 2, False),   # not all, and not more than 3
    (4, 2, False),   # half
    (6, 3, False),
])
def test_more_than_half_of_more_than_three_is_held(readest, session, total, deleted, held):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 2 + total)]
    readest.run_pass()
    for nid in ids[:deleted]:
        readest.delete(nid)
    readest.run_pass()
    gone = sum(r.deleted_at is not None for r in rows(session))
    assert (gone, sum(r.readest_held for r in rows(session))) == ((0, deleted) if held else (deleted, 0))


def test_held_deletion_is_sticky(readest, session):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 6)]  # 4
    readest.run_pass()
    for nid in ids:
        readest.delete(nid)
    readest.run_pass()
    # Later activity: 5 new highlights. 4 missing of 9 would not be a mass deletion,
    # but the hold stands.
    for n in range(10, 15):
        readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}")
    readest.run_pass()
    readest.run_pass()
    assert len(rows(session)) == 9
    assert all(r.deleted_at is None for r in rows(session))
    assert sum(r.readest_held for r in rows(session)) == 4


def test_hold_clears_when_the_key_reappears(readest, session):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 6)]
    readest.run_pass()
    for nid in ids:
        readest.delete(nid)
    readest.run_pass()
    readest.notes[ids[0]]["deletedAt"] = None        # back in Readest's key list
    readest.run_pass()
    first = rows(session)[0]
    assert not first.readest_held and sum(r.readest_held for r in rows(session)) == 3
    readest.delete(ids[0])                           # now a lone deletion
    readest.run_pass()
    assert rows(session)[0].deleted_at is not None
    assert all(r.deleted_at is None for r in rows(session)[1:])


def test_release_held_deletions_applies_them(readest, session):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 6)]
    readest.run_pass()
    for nid in ids:
        readest.delete(nid)
    readest.run_pass()
    assert bookorbit.release_held_deletions(session, USER, BOOK) == 4
    session.commit()
    assert all(r.deleted_at is not None and not r.readest_held for r in rows(session))
    # Readest already deleted them: nothing is offered back.
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}
    assert bookorbit.release_held_deletions(session, USER) == 0


def test_held_highlight_is_not_offered_as_edit(readest, session):
    ids = [readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}") for n in range(2, 6)]
    readest.run_pass()
    for nid in ids:
        readest.delete(nid)
    readest.run_pass()
    # The Kobo edits one of the held highlights.
    row = rows(session)[0]
    upsert_from_side(session, USER, BOOK, "readest", row.readest_key,
                     {"note": "edited elsewhere", "updated_at": datetime.now(timezone.utc) + timedelta(hours=1)},
                     datetime.now(timezone.utc))
    session.commit()
    assert readest.run_pass()["toApply"]["edit"] == []


def test_edit_from_other_side_offered_as_edit(readest, session):
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    readest.run_pass()
    [row] = rows(session)
    key = row.readest_key
    # The Kobo adds a note to the highlight.
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo", note="later thought",
                   at=datetime.now(timezone.utc))
    result = readest.run_pass()
    [edit] = result["toApply"]["edit"]
    assert edit["key"] == key and edit["serverId"] == row.id
    assert edit["note"] == "later thought" and edit["version"] == rows(session)[0].version
    [note] = readest.notes.values()
    assert note["note"] == "later thought"
    # Acked: not offered again.
    assert readest.run_pass()["toApply"]["edit"] == []


def test_delete_from_other_side_offered_as_delete(readest, session):
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    kobo_highlight(session, "kobo-2", xp(5, 0), xp(5, 20), "Another")
    readest.run_pass()
    from cps.progress_syncing.highlights.store import delete_from_side
    delete_from_side(session, USER, BOOK, "kobo", "kobo-1", datetime.now(timezone.utc))
    session.commit()
    result = readest.run_pass()
    [entry] = result["toApply"]["delete"]
    assert entry["key"] == rows(session)[0].readest_key
    assert [n["deletedAt"] is not None for n in readest.notes.values()].count(True) == 1
    assert readest.run_pass()["toApply"]["delete"] == []


def test_readest_position_move_clears_kobo_range_keeps_id(readest, session):
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    readest.run_pass()
    [row] = rows(session)
    version = row.version
    from cps.progress_syncing.highlights.store import HighlightBookState
    etag = session.query(HighlightBookState).filter_by(user_id=USER, book_id=BOOK).one().kobo_etag_version
    [nid] = readest.notes
    readest.edit(nid, xpointer0=xp(3, 5), xpointer1=xp(3, 25))
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}
    [row] = rows(session)
    assert row.deleted_at is None
    assert (row.xp0, row.xp1) == (xp(3, 5), xp(3, 25))
    assert row.readest_key == readest.key_of(readest.notes[nid])
    assert row.kobo_id == "kobo-1"
    assert (row.kobo_chapter, row.kobo_start_path, row.kobo_start_char, row.kobo_end_path,
            row.kobo_end_char, row.kobo_chapter_progress) == (None,) * 6
    assert row.version == version + 1
    session.expire_all()
    assert session.query(HighlightBookState).filter_by(
        user_id=USER, book_id=BOOK).one().kobo_etag_version == etag + 1


def _change(dt, pos0, pos1, text):
    return {"datetime": dt, "drawer": "lighten", "posFormat": "xpointer", "pos0": pos0,
            "pos1": pos1, "text": text}


def test_two_books_in_one_exchange(client, session):
    dt = "2026-10-30 10:00:00"
    r = exchange(client, [
        {"hash": HASH, "keys": [{"k": bookorbit_key(dt, xp(2, 0)), "dt": dt}], "keysComplete": True,
         "changes": [_change(dt, xp(2, 0), xp(2, 4), "in book one")]},
        {"hash": OTHER_HASH, "keys": [{"k": bookorbit_key(dt, xp(7, 0)), "dt": dt}],
         "keysComplete": True, "changes": [_change(dt, xp(7, 0), xp(7, 4), "in book two")]},
        {"hash": "deadbeef", "keys": [], "keysComplete": True, "changes": []},
    ])
    assert [res["bookId"] for res in r["results"]] == [BOOK, OTHER_BOOK]
    assert r["unmatched"] == ["deadbeef"]
    assert [row.text for row in rows(session, BOOK)] == ["in book one"]
    assert [row.text for row in rows(session, OTHER_BOOK)] == ["in book two"]


def test_key_from_another_book_is_not_applied_across_books(client, session):
    dt = "2026-10-30 10:00:00"
    key = [{"k": bookorbit_key(dt, xp(2, 0)), "dt": dt}]
    exchange(client, [{"hash": HASH, "keys": key, "keysComplete": True,
                       "changes": [_change(dt, xp(2, 0), xp(2, 4), "in book one")]}])
    exchange(client, [{"hash": OTHER_HASH, "keys": key, "keysComplete": True,
                       "changes": [_change(dt, xp(2, 0), xp(2, 4), "same key, other book")]}])
    assert [row.text for row in rows(session, BOOK)] == ["in book one"]
    assert rows(session, OTHER_BOOK) == []


def test_readest_key_is_unique_per_user(session):
    session.add(Highlight(user_id=USER, book_id=BOOK, readest_key="k1", version=1))
    session.add(Highlight(user_id=USER, book_id=BOOK, readest_key=None, version=1))
    session.add(Highlight(user_id=USER, book_id=BOOK, readest_key=None, version=1))
    session.add(Highlight(user_id=USER + 1, book_id=BOOK, readest_key="k1", version=1))
    session.commit()
    session.add(Highlight(user_id=USER, book_id=OTHER_BOOK, readest_key="k1", version=1))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_ensure_tables_adds_unique_readest_key_index_idempotently():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY)")
    ensure_highlight_tables(conn)
    ensure_highlight_tables(conn)
    conn.execute("INSERT INTO highlight (user_id, book_id, readest_key) VALUES (3, 1, 'k1')")
    conn.execute("INSERT INTO highlight (user_id, book_id, readest_key) VALUES (3, 1, NULL)")
    conn.execute("INSERT INTO highlight (user_id, book_id, readest_key) VALUES (3, 1, NULL)")
    conn.execute("INSERT INTO highlight (user_id, book_id, readest_key) VALUES (4, 1, 'k1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO highlight (user_id, book_id, readest_key) VALUES (3, 2, 'k1')")


def test_bad_request_body_is_400(client):
    r = client.post(PREFIX + "/plugin/annotations/exchange", headers=AUTH, data="nope",
                    content_type="application/json")
    assert r.status_code == 400
    r = client.post(PREFIX + "/plugin/annotations/exchange", headers=AUTH,
                    json={"deviceId": DEVICE, "books": "x"})
    assert r.status_code == 400


# --- progress -------------------------------------------------------------------

def test_progress_routes_reach_bridge(client, session, monkeypatch):
    calls = []
    monkeypatch.setattr(kosync, "is_koreader_sync_enabled", lambda: True)
    monkeypatch.setattr(kosync, "is_position_bridge_enabled", lambda: True)
    monkeypatch.setattr(kosync, "update_book_read_status", lambda *a, **k: None)
    monkeypatch.setattr(kosync, "push_reading_state_to_hardcover", lambda *a, **k: None)
    monkeypatch.setattr(cps, "calibre_db", SimpleNamespace(get_book=lambda book_id: None), raising=False)
    monkeypatch.setattr(kosync, "run_bridge", lambda hook, s, user_id, book_id, progress, *rest:
                        calls.append((user_id, book_id, progress)))
    # Readest's KOSyncClient.updateProgress payload, with BookOrbit's headers.
    body = {"document": HASH, "progress": "/body/DocFragment[6]/body/p[3]/text().0",
            "percentage": 0.42, "device": "iPhone", "device_id": DEVICE}
    r = client.put(PREFIX + "/syncs/progress", json=body, headers=AUTH)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["document"] == HASH
    assert calls == [(USER, BOOK, body["progress"])]
    r = client.get(PREFIX + f"/syncs/progress/{HASH}", headers=AUTH)
    assert r.status_code == 200
    assert r.get_json()["progress"] == body["progress"]
    assert r.get_json()["percentage"] == pytest.approx(0.42)
    # BookOrbit auth is required; kosync's Basic auth is not consulted here.
    r = client.put(PREFIX + "/syncs/progress", json=body, headers={**AUTH, "X-Auth-Key": "0" * 32})
    assert r.status_code == 401
    assert len(calls) == 1


def test_corrected_pos0_in_ack_does_not_change_the_key(client, session):
    # Readest keeps the add's pos0 as the note's xpointer0 even when it acks a
    # corrected one (annotationExchange.ts), so its key uses the pos0 CWA sent.
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    r = exchange(client, [{"hash": HASH, "keys": [], "keysComplete": True, "changes": []}])
    [add] = r["results"][0]["toApply"]["add"]
    ack = client.post(PREFIX + "/plugin/annotations/exchange-ack", headers=AUTH, json={
        "deviceId": DEVICE, "books": [{"hash": HASH, "deleted": [], "applied": [
            {"serverId": add["serverId"], "version": add["version"], "status": "applied",
             "verified": False, "corrected": True, "pos0": xp(3, 1), "pos1": xp(3, 21)}]}]})
    assert ack.status_code == 200
    [row] = rows(session)
    assert row.readest_key == bookorbit_key(add["datetime"], xp(3, 0))
    assert (row.xp0, row.xp1) == (xp(3, 0), xp(3, 20))


# --- fix round 1 ------------------------------------------------------------------

def live_notes(readest):
    return [n for n in readest.notes.values() if not n["deletedAt"]]


def test_readd_into_a_readest_tombstone_is_not_deleted_from_the_kobo(readest, session):
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "The same passage")
    readest.run_pass()                                  # offered, applied, acked
    readest.run_pass()                                  # Readest lists the key
    [nid] = readest.notes
    readest.delete(nid)
    readest.run_pass()
    assert rows(session)[0].deleted_at is not None      # a lone deletion propagates
    # The Kobo highlights the same passage again: a new Kobo id, the same position,
    # so Readest's add lands on its tombstone (same note id) and stays deleted.
    kobo_highlight(session, "kobo-2", xp(3, 0), xp(3, 20), "The same passage",
                   at=datetime.now(timezone.utc))
    result = readest.run_pass()
    assert [a["text"] for a in result["toApply"]["add"]] == ["The same passage"]
    assert live_notes(readest) == [] and len(readest.notes) == 1
    for _ in range(3):
        result = readest.run_pass()
        assert result["toApply"] == {"add": [], "edit": [], "delete": []}
    again = [r for r in rows(session) if r.kobo_id == "kobo-2"][0]
    assert again.deleted_at is None                     # still on the Kobo
    assert again.readest_key is None and again.readest_failed_version == again.version


def test_chunked_exchange_with_60_overlapping_highlights_does_not_duplicate(readest, session):
    # The same 60 passages, highlighted on the Kobo and in Readest before either synced.
    for n in range(2, 62):
        kobo_highlight(session, f"kobo-{n}", xp(n, 0), xp(n, 9), f"passage {n}")
        readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}")
    result = readest.run_pass(chunk_size=50)
    assert len(result["responses"]) >= 2
    assert result["toApply"]["add"] == []
    assert len(rows(session)) == 60
    assert all(r.readest_key and r.kobo_id for r in rows(session))
    assert len(live_notes(readest)) == 60 and len(readest.notes) == 60
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}
    assert len(rows(session)) == 60 and all(r.deleted_at is None for r in rows(session))


def test_three_chunks_with_overlapping_highlights_do_not_duplicate(readest, session):
    # 120 changes: chunks of 50, 50 and 20. The middle chunk mustn't carry adds
    # that the last chunk's changes would have merged.
    for n in range(2, 122):
        kobo_highlight(session, f"kobo-{n}", xp(n, 0), xp(n, 9), f"passage {n}")
        readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}")
    result = readest.run_pass(chunk_size=50)
    assert len(result["responses"]) >= 3
    assert result["toApply"]["add"] == []
    assert len(rows(session)) == 120 and len(readest.notes) == 120


def test_chunked_exchange_still_delivers_kobo_only_highlights(readest, session):
    for n in range(2, 62):
        kobo_highlight(session, f"kobo-{n}", xp(n, 0), xp(n, 9), f"passage {n}")
        readest.highlight(xp(n, 0), xp(n, 9), f"passage {n}")
    kobo_highlight(session, "kobo-only", xp(80, 0), xp(80, 9), "only on the Kobo")
    result = readest.run_pass(chunk_size=50)
    assert [a["text"] for a in result["toApply"]["add"]] == ["only on the Kobo"]
    assert len(rows(session)) == 61 and len(live_notes(readest)) == 61
    assert result["more"] is False


def test_lost_ack_then_echo_recovers_each_row_by_key(readest, session):
    # Two highlights of the same phrase in one chapter, so a text merge can't tell
    # them apart. Created out of id order, so Readest holds them in the other order.
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 7), "the end", at=base + timedelta(minutes=1))
    kobo_highlight(session, "kobo-2", xp(9, 0), xp(9, 7), "the end", at=base)
    assert readest.run_pass(lose_ack=True) == "ack lost"
    assert all(r.readest_key is None for r in rows(session))
    readest.run_pass()                                   # echoes both notes as changes
    by_kobo = {r.kobo_id: r for r in rows(session)}
    assert (by_kobo["kobo-1"].xp0, by_kobo["kobo-2"].xp0) == (xp(3, 0), xp(9, 0))
    for row in rows(session):
        assert row.readest_key == bookorbit_key(row.readest_datetime, row.xp0)
    assert len(rows(session)) == 2 and len(readest.notes) == 2
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}


def test_ack_whose_key_is_taken_is_marked_failed(client, session):
    a = kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "first")
    b = kobo_highlight(session, "kobo-2", xp(5, 0), xp(5, 20), "second")
    r = exchange(client, [{"hash": HASH, "keys": [], "keysComplete": True, "changes": []}])
    add = [e for e in r["results"][0]["toApply"]["add"] if e["serverId"] == a.id][0]
    b_row = session.get(Highlight, b.id)
    b_row.readest_key = bookorbit_key(add["datetime"], add["pos0"])
    session.commit()
    client.post(PREFIX + "/plugin/annotations/exchange-ack", headers=AUTH, json={
        "deviceId": DEVICE, "books": [{"hash": HASH, "deleted": [], "applied": [
            {"serverId": a.id, "version": add["version"], "status": "applied"}]}]})
    row = session.get(Highlight, a.id)
    session.refresh(row)
    assert row.readest_key is None and row.readest_failed_version == row.version
    r = exchange(client, [{"hash": HASH, "keys": [], "keysComplete": True, "changes": []}])
    assert a.id not in [e["serverId"] for e in r["results"][0]["toApply"]["add"]]


def test_row_deleted_between_offer_and_ack_is_deleted_in_readest(readest, session):
    from cps.progress_syncing.highlights.store import delete_from_side
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")

    def kobo_deletes(result):
        delete_from_side(session, USER, BOOK, "kobo", "kobo-1", datetime.now(timezone.utc))
        session.commit()
    readest.run_pass(before_ack=kobo_deletes)
    assert live_notes(readest) != []                     # Readest applied it
    assert rows(session)[0].readest_key is not None      # keyed although deleted
    result = readest.run_pass()
    assert len(result["toApply"]["delete"]) == 1
    assert live_notes(readest) == []
    assert len(rows(session)) == 1 and rows(session)[0].deleted_at is not None
    assert readest.run_pass()["toApply"] == {"add": [], "edit": [], "delete": []}
    assert len(rows(session)) == 1 and rows(session)[0].deleted_at is not None


def test_move_of_a_merged_highlight_keeps_its_kobo_id(readest, session):
    # The Readest note is older than the Kobo's copy of the passage, so the merge
    # applies no content; it must still record Readest's identity datetime.
    nid = readest.highlight(xp(3, 1), xp(3, 20), "Merged passage")
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "Merged passage",
                   at=datetime.now(timezone.utc))
    readest.run_pass()
    [row] = rows(session)
    assert row.readest_key == readest.key_of(readest.notes[nid])
    assert row.readest_datetime == readest.identity_of(readest.notes[nid])
    readest.run_pass()
    readest.clock = int(time.time() * 1000) + 3600 * 1000      # Readest's later move wins
    readest.edit(nid, xpointer0=xp(3, 6), xpointer1=xp(3, 26))
    readest.run_pass()
    live = [r for r in rows(session) if r.deleted_at is None]
    assert len(rows(session)) == 1 and len(live) == 1
    assert live[0].kobo_id == "kobo-1" and live[0].xp0 == xp(3, 6) and live[0].kobo_start_path is None


def test_readest_edit_of_a_merged_highlight_keeps_the_kobo_range(readest, session):
    # Merged: the row keeps the XPointers CWA computed from the Kobo's range, while
    # Readest's key comes from its own pos0. A key match proves no move.
    nid = readest.highlight(xp(3, 1), xp(3, 20), "Merged passage")
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "Merged passage",
                   at=datetime.now(timezone.utc))
    readest.run_pass()
    [row] = rows(session)
    assert row.xp0 == xp(3, 0) and row.kobo_start_path is not None
    readest.clock = int(time.time() * 1000) + 3600 * 1000      # Readest's later edit wins
    readest.edit(nid, note="a later thought")
    readest.run_pass()
    [row] = rows(session)
    assert row.note == "a later thought"
    assert (row.xp0, row.xp1) == (xp(3, 0), xp(3, 20))
    assert (row.kobo_start_path, row.kobo_start_char, row.kobo_end_char) == ("span#kobo\\.2\\.1", 0, 14)


def test_readest_echo_with_another_pos1_on_a_key_match_keeps_the_kobo_range(readest, session):
    # pos1 isn't in the key, so a pos1 that differs from CWA's xp1 on a key match is
    # another spelling of the same end, not a move: the Kobo's range stays.
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    readest.run_pass()
    [nid] = readest.notes
    readest.edit(nid, xpointer1=xp(3, 30))
    readest.run_pass()
    [row] = rows(session)
    assert row.xp1 == xp(3, 20) and row.kobo_start_path == "span#kobo\\.2\\.1" and row.kobo_id == "kobo-1"


def test_colourless_kobo_highlight_is_offered_yellow(readest, session):
    """Readest shows a note without a colour in red; the Kobo's highlights are yellow."""
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    result = readest.run_pass()
    [add] = result["toApply"]["add"]
    assert add["color"] == "yellow"
    [note] = readest.notes.values()
    assert note["color"] == "yellow"
    [row] = rows(session)
    assert row.color is None


def test_yellow_echo_of_a_colourless_highlight_changes_nothing(client, session, readest):
    kobo_highlight(session, "kobo-1", xp(3, 0), xp(3, 20), "From the Kobo")
    readest.run_pass()
    [row] = rows(session)
    version = row.version
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    change = {**_change(row.readest_datetime, xp(3, 0), xp(3, 20), "From the Kobo"),
              "color": "yellow", "datetimeUpdated": later}
    r = exchange(client, [{"hash": HASH, "keys": [{"k": row.readest_key, "dt": row.readest_datetime}],
                           "keysComplete": True, "changes": [change]}])
    assert r["results"][0]["toApply"] == {"add": [], "edit": [], "delete": []}
    [row] = rows(session)
    assert row.version == version and row.color is None


def test_edit_of_a_colourless_highlight_leaves_readests_own_colour(readest, session):
    nid = readest.highlight(xp(2, 0), xp(2, 14), "A first passage", color="#123456")  # no KOReader name
    readest.run_pass()
    [row] = rows(session)
    assert row.color is None
    upsert_from_side(session, USER, BOOK, "readest", row.readest_key,
                     {"note": "from the Kobo", "updated_at": datetime.now(timezone.utc) + timedelta(hours=1)},
                     datetime.now(timezone.utc))
    session.commit()
    [edit] = readest.run_pass()["toApply"]["edit"]
    assert edit["color"] == ""
    assert readest.notes[nid]["color"] == "#123456" and readest.notes[nid]["note"] == "from the Kobo"
