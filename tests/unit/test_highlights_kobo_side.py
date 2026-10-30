# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the Kobo side of highlight sync (store <-> Kobo reading services)."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import ub
from cps.progress_syncing import settings
from cps.progress_syncing.highlights import kobo_side
from cps.progress_syncing.highlights.kobo_side import (
    apply_kobo_patch,
    kobo_annotation_set,
    kobo_patch_error,
    kobo_changed_books,
    parse_kobo_firmware,
    record_kobo_firmware,
)
from cps.progress_syncing.highlights.ranges import KoboRange, kobo_range_to_xpointers
from cps.progress_syncing.highlights.store import (
    Highlight,
    HighlightBookState,
    HighlightDevice,
    KoboOpaqueAnnotation,
    upsert_from_side,
)
from cps.progress_syncing.position_bridge.book import EpubBook
from cps.progress_syncing.position_bridge.state import PositionBridgeState

from .position_bridge_books import CH1, CH2, kepubify_like, write_book

USER, BOOK, OTHER_BOOK = 3, 1, 2
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)
CH2_PATH = "OEBPS/text/ch2.xhtml"
KOBO_ID = "0b3a3c4e-7d5f-4c11-9f3e-2b1f0a9c8d71"
UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
FIRMWARE = "4.38.23852"
UA = "Mozilla/5.0 (Linux; U; Android 2.0; en-us;) AppleWebKit/538.1 (KHTML, like Gecko) " \
     "Version/4.0 Mobile Safari/538.1 (Kobo Touch 0383/4.38.23852)"
CROSS_CHAPTER = ("/body/DocFragment[1]/body/p[1]/text().2", "/body/DocFragment[2]/body/p[4]/text().8")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="jcureton", email="j@example.test"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def books(tmp_path):
    epub = write_book(tmp_path / "b.epub", {"ch1.xhtml": CH1, "ch2.xhtml": CH2})
    kepub = write_book(
        tmp_path / "b.kepub", {"ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(CH2)})
    return epub, kepub


@pytest.fixture
def files(books):
    return lambda book_id: books


@pytest.fixture
def flags(monkeypatch):
    state = {"on": True, "ever": True, "firmware": {FIRMWARE}}
    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: state["on"])
    monkeypatch.setattr(settings, "highlight_sync_ever_enabled", lambda: state["ever"])
    monkeypatch.setattr(settings, "tested_kobo_firmware", lambda: set(state["firmware"]))
    return state


def span(start_char=4, end_char=30, path=r"span#kobo\.4\.1", end_path=None, chapter=CH2_PATH,
         progress=0.8):
    return {
        "chapterFilename": chapter, "chapterProgress": progress, "chapterTitle": "Two",
        "startPath": path, "startChar": start_char,
        "endPath": end_path or path, "endChar": end_char,
    }


def annotation(kobo_id=KOBO_ID, text="wife had discovered that t", note=None, modified="",
               **span_kwargs):
    entry = {
        "clientLastModifiedUtc": modified,
        "highlightedText": text,
        "id": kobo_id,
        "location": {"span": span(**span_kwargs)},
        "type": "note" if note else "highlight",
    }
    if note:
        entry["noteText"] = note
    return entry


DOGEAR_ID = "d0d0d0d0-0000-4000-8000-000000000001"


def dogear_entry(progress=0.25):
    return {
        "type": "dogear", "id": DOGEAR_ID, "clientLastModifiedUtc": "",
        "location": {"span": {"chapterFilename": CH2_PATH, "chapterProgress": progress,
                              "chapterTitle": "Two", "startPath": r"span#kobo\.2\.1", "startChar": 0,
                              "endPath": r"span#kobo\.2\.1", "endChar": 0}},
        "futureField": {"kept": [1, 2]},
    }


def ch2_xpointers(books, start_char=4, end_char=30):
    epub, kepub = EpubBook(books[0]), EpubBook(books[1])
    stream = kepub.span_stream(CH2_PATH)
    start = stream.offset_of("kobo.4.1") + start_char
    r = KoboRange(CH2_PATH, r"span#kobo\.4\.1", start_char, r"span#kobo\.4\.1", end_char,
                  start / len(stream.text))
    return kobo_range_to_xpointers(epub, kepub, r)


def etag_version(session, book=BOOK):
    state = session.query(HighlightBookState).filter_by(user_id=USER, book_id=book).first()
    return state.kobo_etag_version if state else 0


def readest_highlight(session, xps, key="rk-1", text="wife had discovered that t", note=None):
    fields = {"text": text, "xp0": xps[0], "xp1": xps[1], "updated_at": T0, "created_at": T0}
    if note:
        fields["note"] = note
    return upsert_from_side(session, USER, BOOK, "readest", key, fields, T0)


@pytest.mark.unit
class TestFirmware:
    def test_parses_kobo_touch_user_agent(self):
        assert parse_kobo_firmware(UA) == FIRMWARE
        assert parse_kobo_firmware("Kobo Touch 0383/4.41.23145") == "4.41.23145"

    def test_other_user_agents_have_none(self):
        assert parse_kobo_firmware(None) is None
        assert parse_kobo_firmware("") is None
        assert parse_kobo_firmware("Mozilla/5.0 (Macintosh)") is None

    def test_records_firmware_once_per_change(self, session):
        assert record_kobo_firmware(session, USER, UA) == FIRMWARE
        record_kobo_firmware(session, USER, UA)
        session.commit()
        assert [d.kobo_firmware for d in session.query(HighlightDevice).all()] == [FIRMWARE]
        record_kobo_firmware(session, USER, "Kobo Touch 0383/4.39.1")
        assert session.query(HighlightDevice).one().kobo_firmware == "4.39.1"

    def test_unparseable_user_agent_records_nothing(self, session):
        assert record_kobo_firmware(session, USER, "curl/8") is None
        assert session.query(HighlightDevice).count() == 0


@pytest.mark.unit
class TestApplyKoboPatch:
    def test_create_keeps_kobo_id_and_paths_verbatim_and_translates(self, session, books, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW, files=files)
        row = session.query(Highlight).one()
        assert (row.kobo_id, row.origin, row.text) == (KOBO_ID, "kobo", "wife had discovered that t")
        assert (row.kobo_chapter, row.kobo_start_path, row.kobo_start_char) == (CH2_PATH, r"span#kobo\.4\.1", 4)
        assert (row.kobo_end_path, row.kobo_end_char, row.kobo_chapter_progress) == (r"span#kobo\.4\.1", 30, 0.8)
        assert (row.xp0, row.xp1) == ch2_xpointers(books)
        assert row.note is None
        assert etag_version(session) == 1

    def test_empty_timestamp_create_uses_now(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation(modified="")]}, NOW, files=files)
        row = session.query(Highlight).one()
        assert row.updated_at.replace(tzinfo=timezone.utc) == NOW
        assert row.created_at.replace(tzinfo=timezone.utc) == NOW

    def test_kobo_timestamp_is_used_when_present(self, session, files):
        body = {"updatedAnnotations": [annotation(modified="2024-11-03T14:14:40Z")]}
        apply_kobo_patch(session, USER, BOOK, body, NOW, files=files)
        row = session.query(Highlight).one()
        assert row.updated_at.replace(tzinfo=timezone.utc) == datetime(2024, 11, 3, 14, 14, 40, tzinfo=timezone.utc)

    def test_empty_timestamp_edit_never_beats_existing_row(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation(note="first")]}, T0, files=files)
        before = etag_version(session)
        body = {"updatedAnnotations": [annotation(note="second", modified="")]}
        apply_kobo_patch(session, USER, BOOK, body, NOW, files=files)
        row = session.query(Highlight).one()
        assert row.note == "first"
        assert row.updated_at.replace(tzinfo=timezone.utc) == T0
        assert etag_version(session) == before

    def test_newer_timestamped_edit_applies(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, T0, files=files)
        body = {"updatedAnnotations": [annotation(note="added", modified="2026-10-30T12:30:00.1234567Z")]}
        apply_kobo_patch(session, USER, BOOK, body, NOW, files=files)
        row = session.query(Highlight).one()
        assert row.note == "added"
        assert etag_version(session) == 2

    def test_untranslatable_range_is_stored_without_xpointers(self, session, files):
        body = {"updatedAnnotations": [annotation(path=r"span#kobo\.1\.1", end_path=r"span#kobo\.9\.1", end_char=3)]}
        apply_kobo_patch(session, USER, BOOK, body, NOW, files=files)
        row = session.query(Highlight).one()
        assert (row.xp0, row.xp1) == (None, None)
        assert row.kobo_end_path == r"span#kobo\.9\.1"

    def test_missing_book_files_store_without_xpointers(self, session):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW,
                         files=lambda book_id: (None, None))
        row = session.query(Highlight).one()
        assert row.kobo_start_path == r"span#kobo\.4\.1" and row.xp0 is None

    def test_unreadable_book_file_stores_without_xpointers(self, session, books, tmp_path):
        broken = tmp_path / "broken.kepub"
        broken.write_bytes(b"not a zip")
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW,
                         files=lambda book_id: (books[0], str(broken)))
        row = session.query(Highlight).one()
        assert row.kobo_start_path == r"span#kobo\.4\.1" and row.xp0 is None

    def test_stale_kobo_copy_is_not_translated(self, session, files):
        session.add(PositionBridgeState(user_id=USER, book_id=BOOK, kobo_copy_digest="another-copy"))
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW, files=files)
        assert session.query(Highlight).one().xp0 is None

    def test_delete_marks_deleted(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, T0, files=files)
        apply_kobo_patch(session, USER, BOOK, {"deletedAnnotationIds": [KOBO_ID, "unknown-id"]}, NOW, files=files)
        row = session.query(Highlight).one()
        assert row.deleted_at.replace(tzinfo=timezone.utc) == NOW

    def test_empty_body_is_a_no_op(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {}, NOW, files=files)
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": []}, NOW, files=files)
        assert session.query(Highlight).count() == 0

    def test_note_type(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation(note="mine")]}, NOW, files=files)
        assert session.query(Highlight).one().note == "mine"

    def test_unknown_timestamp_merge_links_and_keeps_the_readest_note(self, session, books, files):
        readest_highlight(session, ch2_xpointers(books), note="from the phone")
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW, files=files)
        row = session.query(Highlight).one()
        assert (row.kobo_id, row.readest_key, row.origin) == (KOBO_ID, "rk-1", "readest")
        assert row.note == "from the phone"
        assert (row.kobo_start_path, row.kobo_start_char, row.kobo_end_char) == (r"span#kobo\.4\.1", 4, 30)

    def test_dogear_is_kept_verbatim_and_echoed(self, session, books, files):
        dogear = dogear_entry()
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [dogear, annotation(kobo_id="other")]},
                         NOW, files=files)
        assert [r.kobo_id for r in session.query(Highlight).all()] == ["other"]
        assert session.query(KoboOpaqueAnnotation).one().kobo_id == DOGEAR_ID
        assert etag_version(session) == 2
        body, etag = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert dogear in body["annotations"] and etag == 'W/"cwa-2"'
        assert [e["id"] for e in body["annotations"]] == ["other", DOGEAR_ID]

    def test_dogear_echo_survives_the_fallback_read(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [dogear_entry()]}, NOW, files=files)
        body, _ = kobo_annotation_set(session, USER, BOOK, None, None, translate_missing=False)
        assert body["annotations"] == [dogear_entry()]

    def test_opaque_edit_and_delete_bump_the_version(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [dogear_entry()]}, T0, files=files)
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [dogear_entry()]}, NOW, files=files)
        assert etag_version(session) == 1  # an identical echo changes nothing
        moved = dogear_entry(progress=0.5)
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [moved]}, NOW, files=files)
        assert json.loads(session.query(KoboOpaqueAnnotation).one().raw) == moved
        assert etag_version(session) == 2
        apply_kobo_patch(session, USER, BOOK, {"deletedAnnotationIds": [DOGEAR_ID]}, NOW, files=files)
        assert session.query(KoboOpaqueAnnotation).one().deleted_at is not None
        assert etag_version(session) == 3
        body, _ = kobo_annotation_set(session, USER, BOOK, None, None)
        assert body["annotations"] == []

    def test_highlight_without_a_complete_location_is_kept_verbatim(self, session, books, files):
        entry = annotation()
        del entry["location"]["span"]["endPath"]
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [entry]}, NOW, files=files)
        assert session.query(Highlight).count() == 0
        body, _ = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert body["annotations"] == [entry]

    def test_edit_without_location_keeps_the_stored_range(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, T0, files=files)
        entry = annotation(note="later", modified="2026-10-30T12:30:00Z")
        del entry["location"]
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [entry]}, NOW, files=files)
        row = session.query(Highlight).one()
        assert row.note == "later" and row.kobo_start_path == r"span#kobo\.4\.1" and row.xp0
        assert session.query(KoboOpaqueAnnotation).count() == 0

    def test_opaque_that_becomes_a_highlight_is_served_once(self, session, books, files):
        entry = annotation()
        del entry["location"]
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [entry]}, T0, files=files)
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW, files=files)
        body, _ = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert [e["id"] for e in body["annotations"]] == [KOBO_ID]
        assert body["annotations"][0]["location"]["span"]["startChar"] == 4

    @pytest.mark.parametrize("body", [
        [annotation()],
        "text",
        {"updatedAnnotations": annotation()},
        {"updatedAnnotations": [annotation(), "junk"]},
        {"updatedAnnotations": [{k: v for k, v in annotation().items() if k != "id"}]},
        {"updatedAnnotations": [annotation(kobo_id="")]},
        {"deletedAnnotationIds": "abc"},
        {"deletedAnnotationIds": [KOBO_ID, 7]},
    ])
    def test_unfit_bodies_are_rejected_whole(self, session, files, body):
        assert kobo_patch_error(body)
        with pytest.raises(ValueError):
            apply_kobo_patch(session, USER, BOOK, body, NOW, files=files)
        assert session.query(Highlight).count() == 0

    def test_fit_bodies(self):
        assert kobo_patch_error({}) is None
        assert kobo_patch_error({"updatedAnnotations": None, "deletedAnnotationIds": []}) is None
        assert kobo_patch_error({"updatedAnnotations": [dogear_entry()], "deletedAnnotationIds": ["x"]}) is None


@pytest.mark.unit
class TestAnnotationSet:
    def test_empty_book(self, session, books):
        body, etag = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert body == {"annotations": [], "nextPageOffsetToken": None}
        assert etag == 'W/"cwa-0"'

    def test_kobo_highlight_is_served_as_stored(self, session, books, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation(note="n")]}, NOW, files=files)
        body, etag = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert etag == 'W/"cwa-1"'
        assert body["nextPageOffsetToken"] is None
        (entry,) = body["annotations"]
        assert entry == {
            "type": "note",
            "id": KOBO_ID,
            "clientLastModifiedUtc": "2026-10-30T13:00:00Z",
            "highlightedText": "wife had discovered that t",
            "noteText": "n",
            "attachments": {},
            "location": {"span": {
                "chapterFilename": CH2_PATH, "chapterProgress": 0.8,
                "startPath": r"span#kobo\.4\.1", "startChar": 4,
                "endPath": r"span#kobo\.4\.1", "endChar": 30,
            }},
        }

    def test_highlight_without_note_has_no_note_text_and_no_colour(self, session, books, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW, files=files)
        session.query(Highlight).one().color = "yellow"
        body, _ = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        (entry,) = body["annotations"]
        assert entry["type"] == "highlight"
        assert "noteText" not in entry and "highlightColor" not in entry

    def test_readest_highlight_gets_a_range_stored_once_with_a_new_kobo_id(self, session, books):
        row = readest_highlight(session, ch2_xpointers(books))
        session.commit()
        version = etag_version(session)
        body, etag = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        (entry,) = body["annotations"]
        assert uuid.UUID(entry["id"]).version == 4
        assert entry["location"]["span"]["startPath"] == r"span#kobo\.4\.1"
        assert (entry["location"]["span"]["startChar"], entry["location"]["span"]["endChar"]) == (4, 30)
        assert row.kobo_id == entry["id"] and row.kobo_start_char == 4
        assert etag == f'W/"cwa-{version}"' and etag_version(session) == version
        again, _ = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert again["annotations"][0]["id"] == entry["id"]

    def test_untranslatable_readest_highlight_is_omitted_and_logged(self, session, books, caplog):
        readest_highlight(session, CROSS_CHAPTER, key="rk-x", text="secret passage text")
        good = readest_highlight(session, ch2_xpointers(books))
        with caplog.at_level(logging.WARNING):
            body, etag = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert [e["id"] for e in body["annotations"]] == [good.kobo_id]
        assert not etag.endswith('-pending"')  # it never will translate: don't keep naming the book
        bad = session.query(Highlight).filter_by(readest_key="rk-x").one()
        assert bad.kobo_id is None and bad.kobo_start_path is None
        assert str(bad.id) in caplog.text
        assert "secret passage text" not in caplog.text

    def test_range_across_chapters_is_untranslatable(self, session, books):
        readest_highlight(session, CROSS_CHAPTER)
        body, _ = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert body["annotations"] == []

    def test_stale_kobo_copy_withholds_readest_highlights_but_serves_stored(self, session, books, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, T0, files=files)
        readest_highlight(session, ch2_xpointers(books, 0, 3), key="rk-2", text="The")
        session.add(PositionBridgeState(user_id=USER, book_id=BOOK, kobo_copy_digest="another-copy"))
        body, etag = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert [e["id"] for e in body["annotations"]] == [KOBO_ID]
        assert session.query(Highlight).filter_by(readest_key="rk-2").one().kobo_id is None
        assert etag == f'W/"cwa-{etag_version(session)}-pending"'

    def test_without_translation_only_stored_ranges_are_served(self, session, books, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, T0, files=files)
        readest_highlight(session, ch2_xpointers(books, 0, 3), key="rk-2", text="The")
        body, etag = kobo_annotation_set(session, USER, BOOK, None, None, translate_missing=False)
        assert [e["id"] for e in body["annotations"]] == [KOBO_ID]
        assert session.query(Highlight).filter_by(readest_key="rk-2").one().kobo_id is None
        assert etag.endswith('-pending"')

    def test_deleted_highlights_are_not_served(self, session, books, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, T0, files=files)
        apply_kobo_patch(session, USER, BOOK, {"deletedAnnotationIds": [KOBO_ID]}, NOW, files=files)
        body, etag = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert body["annotations"] == [] and etag == 'W/"cwa-2"'

    def test_colour_is_mapped_only_when_the_kobo_sends_colour(self, session, books, files, monkeypatch):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW, files=files)
        session.query(Highlight).one().color = "blue"
        monkeypatch.setattr(kobo_side, "KOBO_SENDS_COLOR", True)
        body, _ = kobo_annotation_set(session, USER, BOOK, books[1], books[0])
        assert body["annotations"][0]["highlightColor"] == "#B2E1E8"


@pytest.mark.unit
class TestChangedBooks:
    def _setup(self, session, files):
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [annotation()]}, NOW, files=files)
        session.commit()
        return {UUID_A: BOOK, UUID_B: OTHER_BOOK}.get

    def test_names_book_only_when_etag_differs(self, session, files, flags):
        lookup = self._setup(session, files)
        apply_kobo_patch(session, USER, OTHER_BOOK, {"updatedAnnotations": [annotation(kobo_id="b")]},
                         NOW, files=files)
        entries = [{"ContentId": UUID_A, "etag": 'W/"0"'}, {"ContentId": UUID_B, "etag": 'W/"cwa-1"'}]
        assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == [UUID_A]
        entries = [{"ContentId": UUID_A, "etag": 'W/"cwa-1"'}, {"ContentId": UUID_B, "etag": 'W/"0"'}]
        assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == [UUID_B]

    def test_first_sync_names_only_books_with_cwa_content(self, session, files, flags):
        lookup = self._setup(session, files)  # BOOK has a highlight; OTHER_BOOK has never had any
        entries = [{"ContentId": UUID_A, "etag": 'W/"0"'}, {"ContentId": UUID_B, "etag": 'W/"0"'}]
        assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == [UUID_A]

    def test_version_zero_is_never_named_whatever_the_etag(self, session, files, flags):
        lookup = self._setup(session, files)
        for etag in ('W/"0"', 'W/"cwa-pending"', 'W/"something-else"', None):
            entries = [{"ContentId": UUID_B, "etag": etag}]
            assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == []

    def test_pending_etag_is_named_again(self, session, files, flags):
        lookup = self._setup(session, files)
        entries = [{"ContentId": UUID_A, "etag": 'W/"cwa-1-pending"'}]
        assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == [UUID_A]

    def test_empty_when_firmware_untested(self, session, files, flags):
        lookup = self._setup(session, files)
        entries = [{"ContentId": UUID_A, "etag": 'W/"0"'}]
        assert kobo_changed_books(session, USER, entries, "4.39.1", book_id_for=lookup) == []
        assert kobo_changed_books(session, USER, entries, None, book_id_for=lookup) == []
        flags["firmware"] = set()
        assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == []

    def test_empty_in_safe_mode(self, session, files, flags):
        lookup = self._setup(session, files)
        flags["on"] = False
        entries = [{"ContentId": UUID_A, "etag": 'W/"0"'}]
        assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == []

    def test_unknown_books_and_bad_entries_are_ignored(self, session, files, flags):
        lookup = self._setup(session, files)
        entries = [{"ContentId": "store-book", "etag": 'W/"0"'}, "junk", {"etag": 'W/"0"'},
                   {"ContentId": UUID_A, "etag": 'W/"0"'}, {"ContentId": UUID_A, "etag": 'W/"0"'}]
        assert kobo_changed_books(session, USER, entries, FIRMWARE, book_id_for=lookup) == [UUID_A]
