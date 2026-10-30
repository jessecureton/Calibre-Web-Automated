# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for importing highlights from a Kobo database (KoboReader.sqlite)."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import admin, ub, usermanagement
from cps.progress_syncing.highlights import kobo_import
from cps.progress_syncing.highlights.kobo_import import (
    CwaBook,
    apply_import,
    chapter_from_content_id,
    plan_import,
    read_kobo_highlights,
)
from cps.progress_syncing.highlights.kobo_side import apply_kobo_patch
from cps.progress_syncing.highlights.store import CONTENT_FIELDS, Highlight, upsert_from_side

from .position_bridge_books import CH1, CH2, kepubify_like, write_book
from .test_highlights_kobo_side import CH2_PATH, annotation, ch2_xpointers

pytestmark = pytest.mark.unit

USER, OTHER_USER, BOOK = 3, 4, 1
NOW = datetime(2026, 10, 30, 13, 0, 0, tzinfo=timezone.utc)
UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"      # a CWA book
UUID_STORE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"  # a book CWA does not hold
BM1 = "11111111-1111-4111-8111-111111111111"
BM2 = "22222222-2222-4222-8222-222222222222"
BM3 = "33333333-3333-4333-8333-333333333333"
OLD1 = "99999999-9999-4999-8999-999999999991"
OLD2 = "99999999-9999-4999-8999-999999999992"
OLD3 = "99999999-9999-4999-8999-999999999993"
TITLE, AUTHOR = "The Synthetic Novel", "Ada Example"
FIRST = "wife had discovered that t"
SECOND = "husband was carrying on an intrigue"

# The real Bookmark schema (reports/highlights-research/kobo.md section 3). The content
# table is cut down to the columns the import reads.
BOOKMARK_DDL = """CREATE TABLE Bookmark ( BookmarkID TEXT NOT NULL, VolumeID TEXT NOT NULL, ContentID TEXT NOT NULL,
  StartContainerPath TEXT NOT NULL, StartContainerChildIndex INTEGER NOT NULL, StartOffset INTEGER NOT NULL,
  EndContainerPath TEXT NOT NULL, EndContainerChildIndex INTEGER NOT NULL, EndOffset INTEGER NOT NULL,
  Text TEXT, Annotation TEXT, ExtraAnnotationData BLOB, DateCreated TEXT, ChapterProgress REAL NOT NULL DEFAULT 0,
  Hidden BOOL NOT NULL DEFAULT 0, Version TEXT, DateModified TEXT, Creator TEXT, UUID TEXT, UserID TEXT,
  SyncTime TEXT, Published BIT default false, ContextString TEXT, Type TEXT, PRIMARY KEY (BookmarkID) )"""
CONTENT_DDL = """CREATE TABLE content ( ContentID TEXT NOT NULL, ContentType TEXT NOT NULL,
  Title TEXT, Attribution TEXT, PRIMARY KEY (ContentID, ContentType) )"""


def bookmark(bookmark_id=BM1, volume=UUID_A, content=f"{UUID_A}!OEBPS!text/ch2.xhtml", text=FIRST,
             start=r"span#kobo\.4\.1", start_offset=4, end=r"span#kobo\.4\.1", end_offset=30,
             created="2025-07-13T02:06:16.000", modified="2025-07-13T02:06:16Z", progress=0.8,
             annotation_text=None, kind="highlight", hidden=0):
    return (bookmark_id, volume, content, start, -99, start_offset, end, -99, end_offset, text,
            annotation_text, None, created, progress, hidden, modified, kind)


def make_kobo_db(path, bookmarks, titles=((UUID_A, TITLE, AUTHOR),)):
    con = sqlite3.connect(path)
    con.execute(BOOKMARK_DDL)
    con.execute(CONTENT_DDL)
    for volume, title, author in titles:
        con.execute("INSERT INTO content VALUES (?, '6', ?, ?)", (volume, title, author))
        # A chapter row shares the volume's prefix but is not the book's own row.
        con.execute("INSERT INTO content VALUES (?, '9', 'Two', NULL)", (f"{volume}!OEBPS!text/ch2.xhtml",))
    for row in bookmarks:
        con.execute(
            "INSERT INTO Bookmark (BookmarkID, VolumeID, ContentID, StartContainerPath, StartContainerChildIndex, "
            "StartOffset, EndContainerPath, EndContainerChildIndex, EndOffset, Text, Annotation, "
            "ExtraAnnotationData, DateCreated, ChapterProgress, Hidden, DateModified, Type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    con.commit()
    con.close()
    return str(path)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="jcureton", email="j@example.test"))
    s.add(ub.User(id=OTHER_USER, name="other", email="o@example.test"))
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
def cwa(books):
    """The lookups plan_import takes in place of calibre_db."""
    library = [CwaBook(BOOK, TITLE, (AUTHOR,)), CwaBook(2, "Another Book", ("Zed Other",))]
    return {
        "find_book": lambda uuid: library[0] if uuid == UUID_A else None,
        "catalog": lambda: library,
        "files": lambda book_id: books,
    }


@pytest.fixture
def main_db(tmp_path):
    return make_kobo_db(tmp_path / "KoboReader.sqlite", [
        bookmark(BM1),
        bookmark(BM2, text=""),
        bookmark(BM3, volume=UUID_STORE, content=f"{UUID_STORE}!OEBPS!text/ch2.xhtml", text="Unmatched volume"),
    ], titles=((UUID_A, TITLE, AUTHOR), (UUID_STORE, "A Store Book", "Sam Store")))


def plan(session, rows, older=(), cwa=None, user=USER):
    return plan_import(session, user, rows, older, **cwa)


class TestContentId:
    def test_chapter_is_everything_after_the_first_bang_with_the_rest_as_slashes(self):
        assert chapter_from_content_id("vol!ops!xhtml/ch01a.html") == "ops/xhtml/ch01a.html"

    def test_empty_opf_dir_part_gives_just_the_path(self):
        assert chapter_from_content_id("vol!!xhtml/ch01a.html") == "xhtml/ch01a.html"

    def test_without_a_bang_there_is_no_chapter(self):
        assert chapter_from_content_id("file:///mnt/onboard/old.epub#(7)OEBPS/ch.xhtml") is None
        assert chapter_from_content_id("") is None


class TestReadKoboHighlights:
    def test_reads_highlights_and_skips_empty_text(self, main_db):
        rows = read_kobo_highlights(main_db)
        assert sorted(r["bookmark_id"] for r in rows) == [BM1, BM3]
        first = next(r for r in rows if r["bookmark_id"] == BM1)
        assert (first["volume_id"], first["content_id"]) == (UUID_A, f"{UUID_A}!OEBPS!text/ch2.xhtml")
        assert (first["start_path"], first["start_offset"], first["end_path"], first["end_offset"]) == \
            (r"span#kobo\.4\.1", 4, r"span#kobo\.4\.1", 30)
        assert (first["text"], first["note"], first["chapter_progress"]) == (FIRST, None, 0.8)
        assert (first["title"], first["author"]) == (TITLE, AUTHOR)
        assert first["created"] == datetime(2025, 7, 13, 2, 6, 16, tzinfo=timezone.utc)
        assert first["modified"] == datetime(2025, 7, 13, 2, 6, 16, tzinfo=timezone.utc)

    def test_only_highlights_and_never_hidden_ones(self, tmp_path):
        path = make_kobo_db(tmp_path / "k.sqlite", [
            bookmark(BM1), bookmark(BM2, kind="dogear"), bookmark(BM3, hidden=1)])
        assert [r["bookmark_id"] for r in read_kobo_highlights(path)] == [BM1]

    def test_hidden_stored_as_text_like_nickel_does(self, tmp_path):
        """Nickel writes Hidden as the text 'false'/'true', not 0/1."""
        path = make_kobo_db(tmp_path / "k.sqlite", [
            bookmark(BM1, hidden="false"), bookmark(BM2, hidden="true"), bookmark(BM3, hidden="TRUE"),
            bookmark(OLD1, hidden="0"), bookmark(OLD2, hidden="1")])
        con = sqlite3.connect(path)
        assert con.execute("SELECT typeof(Hidden) FROM Bookmark WHERE BookmarkID = ?", (BM1,)).fetchone() == \
            ("text",)
        con.close()
        assert sorted(r["bookmark_id"] for r in read_kobo_highlights(path)) == [BM1, OLD1]

    def test_notes_are_read_and_empty_ones_are_none(self, tmp_path):
        path = make_kobo_db(tmp_path / "k.sqlite", [
            bookmark(BM1, annotation_text="my note"), bookmark(BM2, annotation_text="")])
        notes = {r["bookmark_id"]: r["note"] for r in read_kobo_highlights(path)}
        assert notes == {BM1: "my note", BM2: None}

    def test_blank_modified_is_none(self, tmp_path):
        path = make_kobo_db(tmp_path / "k.sqlite", [bookmark(BM1, modified="")])
        assert read_kobo_highlights(path)[0]["modified"] is None

    def test_does_not_modify_the_file(self, main_db):
        before = os.stat(main_db).st_mtime_ns
        read_kobo_highlights(main_db)
        assert os.stat(main_db).st_mtime_ns == before


class TestPlanImport:
    def test_plan_matches_cwa_books_and_keeps_bookmark_ids(self, session, main_db, books, cwa):
        p = plan(session, read_kobo_highlights(main_db), cwa=cwa)
        assert [i.row["bookmark_id"] for i in p.new] == [BM1]
        assert [i.row["bookmark_id"] for i in p.unmatched] == [BM3]
        assert p.already_present == [] and p.recovered == []
        item = p.new[0]
        assert (item.book_id, item.title) == (BOOK, TITLE)
        f = item.fields
        assert f["kobo_chapter"] == CH2_PATH  # OPF dir "OEBPS" and path joined with a slash
        assert (f["kobo_start_path"], f["kobo_start_char"]) == (r"span#kobo\.4\.1", 4)
        assert (f["kobo_end_path"], f["kobo_end_char"], f["kobo_chapter_progress"]) == (r"span#kobo\.4\.1", 30, 0.8)
        assert f["xp0"] and f["xp1"]
        assert (f["text"], f["note"]) == (FIRST, None)
        assert session.query(Highlight).count() == 0  # a plan writes nothing

    def test_row_already_in_cwa_is_already_present_even_if_deleted_there(self, session, main_db, cwa):
        upsert_from_side(session, USER, BOOK, "kobo", BM1, {"text": FIRST}, NOW)
        session.commit()
        p = plan(session, read_kobo_highlights(main_db), cwa=cwa)
        assert [i.row["bookmark_id"] for i in p.already_present] == [BM1]
        assert p.new == []

    def test_another_users_highlight_does_not_count(self, session, main_db, cwa):
        upsert_from_side(session, OTHER_USER, BOOK, "kobo", BM1, {"text": FIRST}, NOW)
        session.commit()
        assert [i.row["bookmark_id"] for i in plan(session, read_kobo_highlights(main_db), cwa=cwa).new] == [BM1]

    def test_a_row_that_is_not_a_kepub_location_is_unmatched_not_guessed(self, session, tmp_path, cwa):
        path = make_kobo_db(tmp_path / "k.sqlite", [
            bookmark(BM1, start="OEBPS/text/ch2.xhtml#point(/1/4/5/132/3:217)"),
            bookmark(BM2, content="file:///mnt/onboard/x.epub#(7)OEBPS/ch.xhtml")])
        p = plan(session, read_kobo_highlights(path), cwa=cwa)
        assert p.new == [] and sorted(i.row["bookmark_id"] for i in p.unmatched) == [BM1, BM2]
        assert all(i.reason for i in p.unmatched)

    def test_untranslatable_range_is_still_imported_with_the_kobo_location(self, session, tmp_path, cwa):
        path = make_kobo_db(tmp_path / "k.sqlite", [bookmark(BM1, start=r"span#kobo\.99\.9", end=r"span#kobo\.99\.9")])
        p = plan(session, read_kobo_highlights(path), cwa=cwa)
        (item,) = p.new
        assert item.fields["xp0"] is None and item.fields["kobo_start_path"] == r"span#kobo\.99\.9"

    def test_translation_that_raises_is_untranslatable(self, session, main_db, cwa, monkeypatch):
        from cps.progress_syncing.highlights import kobo_side

        def boom(*a):
            raise ValueError("malformed chapter")
        monkeypatch.setattr(kobo_side, "kobo_range_to_xpointers", boom)
        (item,) = plan(session, [r for r in read_kobo_highlights(main_db) if r["bookmark_id"] == BM1], cwa=cwa).new
        assert item.fields["xp0"] is None

    def test_summary_names_books_and_counts_and_never_text(self, session, main_db, cwa):
        p = plan(session, read_kobo_highlights(main_db), cwa=cwa)
        summary = p.summary()
        none = {"new": 0, "already_present": 0, "contained_in_existing": 0, "recovered": 0, "unmatched": 0}
        assert summary == [
            {"title": "A Store Book", **none, "unmatched": 1, "reasons": {kobo_import.REASONS["no_book"]: 1}},
            {"title": TITLE, **none, "new": 1, "reasons": {}},
        ]
        assert FIRST not in repr(summary)
        assert p.counts() == {**none, "new": 1, "unmatched": 1}


class TestRecovery:
    def older(self, tmp_path, rows, title=TITLE, author=AUTHOR):
        path = make_kobo_db(tmp_path / f"old{len(os.listdir(tmp_path))}.sqlite", rows,
                            titles=(("file:///mnt/onboard/old.epub", title, author),))
        return read_kobo_highlights(path)

    def old_row(self, bookmark_id=OLD1, text=SECOND, progress=0.9,
                content="file:///mnt/onboard/old.epub#(7)OEBPS/text/ch2.xhtml"):
        return bookmark(bookmark_id, volume="file:///mnt/onboard/old.epub",
                        content=content,
                        start="OEBPS/text/ch2.xhtml#point(/1/4/5/1/3:5)", end="OEBPS/text/ch2.xhtml#point(/1/4/5/1/3:9)",
                        text=text, progress=progress)

    def test_recovers_highlight_only_in_older_db_by_text(self, session, main_db, tmp_path, cwa):
        older = self.older(tmp_path, [self.old_row()])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        (item,) = p.recovered
        assert (item.row["bookmark_id"], item.book_id, item.title) == (OLD1, BOOK, TITLE)
        f = item.fields
        assert f["kobo_chapter"] == CH2_PATH and f["kobo_start_path"] == r"span#kobo\.4\.1"
        assert f["xp0"] and f["xp1"] and f["text"] == SECOND

    def test_recovered_location_covers_the_text(self, session, main_db, tmp_path, cwa, books):
        from cps.progress_syncing.position_bridge.book import EpubBook
        older = self.older(tmp_path, [self.old_row()])
        (item,) = plan(session, read_kobo_highlights(main_db), older, cwa=cwa).recovered
        stream = EpubBook(books[1]).span_stream(CH2_PATH)
        f = item.fields
        start = stream.offset_of("kobo.4.1") + f["kobo_start_char"]
        end = stream.offset_of("kobo.4.1") + f["kobo_end_char"]
        assert stream.text[start:end] == SECOND

    def test_older_highlight_whose_text_is_in_the_main_db_is_not_recovered(self, session, main_db, tmp_path, cwa):
        # same passage, different case, punctuation and spacing
        older = self.older(tmp_path, [self.old_row(OLD1, text="  Wife had  discovered that t.")])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert p.recovered == []
        assert [i.row["bookmark_id"] for i in p.already_present] == [OLD1]

    def test_older_highlight_already_in_cwa_is_not_recovered_even_if_deleted(self, session, tmp_path, cwa):
        row = upsert_from_side(session, USER, BOOK, "readest", "rk", {"text": SECOND}, NOW)
        row.deleted_at = NOW
        session.commit()
        main = make_kobo_db(tmp_path / "main.sqlite", [bookmark(BM1)])
        p = plan(session, read_kobo_highlights(main), self.older(tmp_path, [self.old_row()]), cwa=cwa)
        assert p.recovered == [] and len(p.already_present) == 1

    def test_older_highlights_repeating_one_text_recover_once(self, session, main_db, tmp_path, cwa):
        older = self.older(tmp_path, [self.old_row(OLD1), self.old_row(OLD2)])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert len(p.recovered) == 1 and len(p.already_present) == 1

    def test_matches_title_and_author_ignoring_case_and_punctuation(self, session, main_db, tmp_path, cwa):
        older = self.older(tmp_path, [self.old_row()], title="the synthetic novel!", author="EXAMPLE, Ada")
        assert len(plan(session, read_kobo_highlights(main_db), older, cwa=cwa).recovered) == 1

    def test_subtitle_does_not_stop_a_match(self, session, main_db, tmp_path, cwa):
        older = self.older(tmp_path, [self.old_row()], title="The Synthetic Novel: A Tale")
        assert len(plan(session, read_kobo_highlights(main_db), older, cwa=cwa).recovered) == 1

    def test_unknown_book_or_wrong_author_is_unmatched(self, session, main_db, tmp_path, cwa):
        for title, author in (("No Such Book", AUTHOR), (TITLE, "Someone Else")):
            older = self.older(tmp_path, [self.old_row()], title=title, author=author)
            p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
            assert p.recovered == [] and OLD1 in [i.row["bookmark_id"] for i in p.unmatched]

    def test_two_cwa_books_with_the_same_title_and_author_are_ambiguous(self, session, main_db, tmp_path, cwa):
        library = [CwaBook(1, TITLE, (AUTHOR,)), CwaBook(5, TITLE, (AUTHOR,))]
        cwa = dict(cwa, catalog=lambda: library)
        older = self.older(tmp_path, [self.old_row()])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert p.recovered == [] and OLD1 in [i.row["bookmark_id"] for i in p.unmatched]

    def test_text_that_is_not_in_the_book_is_unmatched(self, session, main_db, tmp_path, cwa):
        older = self.older(tmp_path, [self.old_row(text="a passage the book never had")])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert p.recovered == [] and OLD1 in [i.row["bookmark_id"] for i in p.unmatched]

    def test_missing_book_files_are_unmatched(self, session, main_db, tmp_path, cwa):
        cwa = dict(cwa, files=lambda book_id: (None, None))
        older = self.older(tmp_path, [self.old_row()])
        assert len(plan(session, read_kobo_highlights(main_db), older, cwa=cwa).recovered) == 0

    def repeated_cwa(self, cwa, tmp_path):
        """A library whose book repeats "Then more." twice in chapter 2, and once in chapter 1."""
        ch2 = CH2.replace("</body>", "<p>Then more.</p><p>Then more.</p></body>")
        epub = write_book(tmp_path / "r.epub", {"ch1.xhtml": CH1, "ch2.xhtml": ch2})
        kepub = write_book(tmp_path / "r.kepub", {"ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(ch2)})
        return dict(cwa, files=lambda book_id: (epub, kepub))

    def test_a_passage_repeated_in_the_book_is_ambiguous_not_placed(self, session, main_db, tmp_path, cwa):
        cwa = self.repeated_cwa(cwa, tmp_path)
        # the older chapter's file is known (ch2) and holds two copies
        older = self.older(tmp_path, [self.old_row(text="Then more.")])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert p.recovered == []
        (item,) = [i for i in p.unmatched if i.row["bookmark_id"] == OLD1]
        assert item.reason == kobo_import.REASONS["ambiguous_text"]
        assert apply_import(session, USER, p, NOW) == 1  # only the main row; nothing placed by guesswork

    def test_unknown_chapter_searches_the_whole_book_and_a_repeat_is_ambiguous(self, session, main_db, tmp_path, cwa):
        cwa = self.repeated_cwa(cwa, tmp_path)
        older = self.older(tmp_path, [self.old_row(text="Then more.", content="file:///old.epub#(3)OEBPS/zzz/other.xhtml")])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert p.recovered == []
        assert [i.reason for i in p.unmatched if i.row["bookmark_id"] == OLD1] == [kobo_import.REASONS["ambiguous_text"]]

    def test_a_unique_passage_still_recovers_in_a_book_with_repeats(self, session, main_db, tmp_path, cwa):
        cwa = self.repeated_cwa(cwa, tmp_path)
        older = self.older(tmp_path, [self.old_row(), self.old_row(OLD2, text="A scene break follows.",
                                                                    content="file:///old.epub#(3)OEBPS/zzz/other.xhtml")])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert sorted(i.row["bookmark_id"] for i in p.recovered) == [OLD1, OLD2]
        assert {i.row["bookmark_id"]: i.fields["kobo_chapter"] for i in p.recovered} == {
            OLD1: CH2_PATH, OLD2: "OEBPS/text/ch1.xhtml"}

    def test_a_text_not_in_the_named_chapter_is_looked_for_in_the_whole_book(self, session, main_db, tmp_path, cwa):
        older = self.older(tmp_path, [self.old_row(text="A scene break follows.")])  # names ch2; the text is in ch1
        (item,) = plan(session, read_kobo_highlights(main_db), older, cwa=cwa).recovered
        assert item.fields["kobo_chapter"] == "OEBPS/text/ch1.xhtml"

    def test_the_named_chapter_decides_between_copies_in_different_chapters(self, session, main_db, tmp_path, cwa):
        ch2 = CH2.replace("</body>", "<p>A scene break follows.</p></body>")  # also in ch1
        epub = write_book(tmp_path / "d.epub", {"ch1.xhtml": CH1, "ch2.xhtml": ch2})
        kepub = write_book(tmp_path / "d.kepub", {"ch1.xhtml": kepubify_like(CH1), "ch2.xhtml": kepubify_like(ch2)})
        cwa = dict(cwa, files=lambda book_id: (epub, kepub))
        older = self.older(tmp_path, [self.old_row(text="A scene break follows.")])
        (item,) = plan(session, read_kobo_highlights(main_db), older, cwa=cwa).recovered
        assert item.fields["kobo_chapter"] == CH2_PATH

    def test_an_unmatched_main_row_does_not_suppress_the_older_copy(self, session, tmp_path, cwa):
        # same text, but its location is not a KEPUB one, so nothing is imported for it
        main = make_kobo_db(tmp_path / "main.sqlite", [bookmark(BM2, start="OEBPS/text/ch2.xhtml#point(/1/2:3)",
                                                                text=SECOND)])
        p = plan(session, read_kobo_highlights(main), self.older(tmp_path, [self.old_row()]), cwa=cwa)
        assert [i.row["bookmark_id"] for i in p.unmatched] == [BM2]
        assert [i.row["bookmark_id"] for i in p.recovered] == [OLD1]
        assert p.already_present == [] and p.contained_in_existing == []

    def test_text_inside_an_existing_highlight_has_its_own_bucket(self, session, main_db, tmp_path, cwa):
        upsert_from_side(session, USER, BOOK, "readest", "rk", {
            "text": "The wife had discovered that the husband was carrying on an intrigue."}, NOW)
        session.commit()
        p = plan(session, read_kobo_highlights(main_db), self.older(tmp_path, [self.old_row()]), cwa=cwa)
        assert [i.row["bookmark_id"] for i in p.contained_in_existing] == [OLD1]
        assert p.recovered == [] and p.counts()["contained_in_existing"] == 1
        (entry,) = [b for b in p.summary() if b["title"] == TITLE]
        assert entry["contained_in_existing"] == 1 and entry["already_present"] == 0

    def test_without_an_older_db_the_catalog_is_never_read(self, session, main_db, cwa):
        def fail():
            raise AssertionError("catalog read")
        plan(session, read_kobo_highlights(main_db), cwa=dict(cwa, catalog=fail))


class TestApplyImport:
    def test_applies_new_rows_as_import_origin_and_readwise_dirty(self, session, main_db, cwa):
        p = plan(session, read_kobo_highlights(main_db), cwa=cwa)
        assert apply_import(session, USER, p, NOW) == 1
        session.commit()
        row = session.query(Highlight).one()
        assert (row.kobo_id, row.origin, row.readwise_dirty, row.book_id, row.user_id) == (BM1, "import", True, BOOK, USER)
        assert row.created_at == datetime(2025, 7, 13, 2, 6, 16)
        assert row.updated_at == datetime(2025, 7, 13, 2, 6, 16)
        assert row.text == FIRST

    def test_reimport_is_idempotent(self, session, main_db, cwa):
        rows = read_kobo_highlights(main_db)
        first = plan(session, rows, cwa=cwa)
        stale = plan(session, rows, cwa=cwa)  # made before the first apply
        assert apply_import(session, USER, first, NOW) == 1
        session.commit()
        again = plan(session, rows, cwa=cwa)
        assert again.new == [] and [i.row["bookmark_id"] for i in again.already_present] == [BM1]
        assert apply_import(session, USER, again, NOW + timedelta(hours=1)) == 0
        assert apply_import(session, USER, stale, NOW + timedelta(hours=1)) == 0
        assert session.query(Highlight).count() == 1

    def test_import_never_overwrites_an_edit_or_a_delete(self, session, main_db, cwa):
        rows = read_kobo_highlights(main_db)
        stale = plan(session, rows, cwa=cwa)
        row = upsert_from_side(session, USER, BOOK, "kobo", BM1, {"text": FIRST, "note": "edited"}, NOW)
        row.deleted_at = NOW
        session.commit()
        assert apply_import(session, USER, stale, NOW + timedelta(days=1)) == 0
        row = session.query(Highlight).one()
        assert row.deleted_at is not None and row.note == "edited"

    def test_import_into_other_user_is_independent(self, session, main_db, cwa):
        rows = read_kobo_highlights(main_db)
        assert apply_import(session, USER, plan(session, rows, cwa=cwa), NOW) == 1
        assert apply_import(session, OTHER_USER, plan(session, rows, cwa=cwa, user=OTHER_USER), NOW) == 1
        assert session.query(Highlight).count() == 2

    def test_applies_recovered_rows(self, session, main_db, tmp_path, cwa):
        older = TestRecovery().older(tmp_path, [TestRecovery().old_row()])
        p = plan(session, read_kobo_highlights(main_db), older, cwa=cwa)
        assert apply_import(session, USER, p, NOW) == 2
        recovered = session.query(Highlight).filter_by(kobo_id=OLD1).one()
        assert (recovered.origin, recovered.readwise_dirty, recovered.text) == ("import", True, SECOND)
        assert recovered.xp0 and recovered.kobo_start_path == r"span#kobo\.4\.1"

    def test_note_is_imported(self, session, tmp_path, cwa):
        path = make_kobo_db(tmp_path / "k.sqlite", [bookmark(BM1, annotation_text="a thought")])
        apply_import(session, USER, plan(session, read_kobo_highlights(path), cwa=cwa), NOW)
        assert session.query(Highlight).one().note == "a thought"

    def test_links_to_the_same_passage_already_in_cwa_from_readest(self, session, main_db, books, cwa):
        xp0, xp1 = ch2_xpointers(books)
        newer = NOW + timedelta(days=1)
        upsert_from_side(session, USER, BOOK, "readest", "rk", {
            "text": FIRST, "xp0": xp0, "xp1": xp1, "updated_at": newer, "created_at": newer}, newer)
        session.commit()
        assert apply_import(session, USER, plan(session, read_kobo_highlights(main_db), cwa=cwa), NOW) == 1
        row = session.query(Highlight).one()  # one highlight, not two
        assert (row.origin, row.readest_key, row.kobo_id) == ("readest", "rk", BM1)
        assert (row.kobo_start_path, row.kobo_start_char, row.kobo_end_char) == (r"span#kobo\.4\.1", 4, 30)

    def test_matches_the_highlight_arriving_by_kobo_patch(self, session, tmp_path, books, cwa):
        """An imported row equals the one the device itself would have PATCHed."""
        # the PATCH, for one user; the import of the same highlight, for another
        entry = annotation(kobo_id=BM1, text=FIRST, note="a thought", modified="2025-07-13T02:06:16Z",
                           start_char=4, end_char=30, path=r"span#kobo\.4\.1", chapter=CH2_PATH, progress=0.8)
        apply_kobo_patch(session, USER, BOOK, {"updatedAnnotations": [entry]}, NOW, files=lambda b: books)
        path = make_kobo_db(tmp_path / "k.sqlite", [bookmark(BM1, annotation_text="a thought")])
        apply_import(session, OTHER_USER, plan(session, read_kobo_highlights(path), cwa=cwa, user=OTHER_USER),
                     NOW)
        patched = session.query(Highlight).filter_by(user_id=USER).one()
        imported = session.query(Highlight).filter_by(user_id=OTHER_USER).one()
        for name in CONTENT_FIELDS:
            assert getattr(imported, name) == getattr(patched, name), name
        assert imported.kobo_id == patched.kobo_id == BM1
        assert imported.xp0 is not None
        assert (imported.origin, patched.origin) == ("import", "kobo")


class TestStash:
    @pytest.fixture(autouse=True)
    def stash_root(self, tmp_path, monkeypatch):
        root = tmp_path / "stash"
        monkeypatch.setattr(kobo_import, "STASH_ROOT", str(root))
        return root

    class Upload:
        def __init__(self, data):
            self.data = data

        def save(self, path):
            with open(path, "wb") as f:
                f.write(self.data)

    def test_saved_files_are_found_by_token_and_deleted_on_discard(self):
        token = kobo_import.save_stash(self.Upload(b"main"), self.Upload(b"older"), USER)
        found = kobo_import.load_stash(token)
        assert found.user_id == USER
        assert open(found.main_path, "rb").read() == b"main" and open(found.older_path, "rb").read() == b"older"
        kobo_import.discard_stash(token)
        assert kobo_import.load_stash(token) is None
        assert not os.path.exists(os.path.dirname(found.main_path))

    def test_older_file_is_optional(self):
        token = kobo_import.save_stash(self.Upload(b"main"), None, USER)
        assert kobo_import.load_stash(token).older_path is None

    def test_expires_after_an_hour_and_is_removed(self):
        token = kobo_import.save_stash(self.Upload(b"main"), None, USER, now=1000.0)
        assert kobo_import.load_stash(token, now=1000.0 + 3599) is not None
        assert kobo_import.load_stash(token, now=1000.0 + 3601) is None
        assert os.listdir(kobo_import.STASH_ROOT) == []

    def test_purge_removes_only_expired_stashes(self):
        old = kobo_import.save_stash(self.Upload(b"x"), None, USER, now=1000.0)
        new = kobo_import.save_stash(self.Upload(b"y"), None, USER, now=5000.0)
        kobo_import.purge_expired(now=5000.0)
        assert kobo_import.load_stash(old, now=5000.0) is None
        assert kobo_import.load_stash(new, now=5000.0) is not None

    @pytest.mark.parametrize("token", ["", "../etc", "a/b", "x" * 5, None])
    def test_hostile_tokens_find_nothing(self, token):
        assert kobo_import.load_stash(token) is None
        kobo_import.discard_stash(token)  # and discarding one is harmless


LAYOUT = "{% block body %}{% endblock %}"


@pytest.fixture
def web(monkeypatch, tmp_path, session, main_db, cwa):
    app = Flask(__name__)
    app.config.update(SECRET_KEY="test", TESTING=True, LOGIN_DISABLED=True)
    app.register_blueprint(admin.admi)
    app.before_request_funcs.clear()  # admin's before_app_request needs a configured library
    app.jinja_loader = ChoiceLoader([DictLoader({"layout.html": LAYOUT}),
                                     FileSystemLoader(os.path.join(os.path.dirname(admin.__file__), "templates"))])
    app.jinja_env.globals["_"] = lambda s, **k: s % k if k else s
    app.jinja_env.globals["csrf_token"] = lambda: "tok"
    me = SimpleNamespace(id=USER, name="jcureton", role_admin=lambda: True, is_authenticated=True)
    state = SimpleNamespace(user=me)
    monkeypatch.setattr(admin, "current_user", state.user)
    monkeypatch.setattr(admin, "_", app.jinja_env.globals["_"])  # no Babel in this app
    monkeypatch.setattr(usermanagement, "config", SimpleNamespace(config_allow_reverse_proxy_header_login=False))
    monkeypatch.setattr(admin, "render_title_template",
                        lambda template, **kw: app.jinja_env.get_template(template).render(**kw))
    monkeypatch.setattr(ub, "session", session)
    monkeypatch.setattr(kobo_import, "STASH_ROOT", str(tmp_path / "stash"))
    # route-level lookups into the library
    monkeypatch.setattr(kobo_import, "book_by_uuid", cwa["find_book"])
    monkeypatch.setattr(kobo_import, "all_books", cwa["catalog"])
    monkeypatch.setattr(kobo_import, "library_files", cwa["files"])
    state.app = app
    state.client = app.test_client()
    state.path = main_db
    return state


def upload(web, path=None, **extra):
    data = {"kobo_db": (open(path or web.path, "rb"), "KoboReader.sqlite"), "user_id": str(USER), **extra}
    return web.client.post("/admin/highlights/import", data=data, content_type="multipart/form-data")


class TestAdminRoute:
    def test_admin_only_route(self, web, monkeypatch):
        monkeypatch.setattr(admin, "current_user",
                            SimpleNamespace(id=USER, role_admin=lambda: False, is_authenticated=True))
        assert web.client.get("/admin/highlights/import").status_code == 403
        assert upload(web).status_code == 403
        assert web.client.post("/admin/highlights/import/apply").status_code == 403

    def test_form_defaults_to_the_current_user(self, web):
        html = web.client.get("/admin/highlights/import").get_data(as_text=True)
        assert 'value="3" selected' in html and 'value="4"' in html
        assert "KoboReader.sqlite" in html

    def test_upload_shows_counts_and_titles_but_no_highlight_text(self, web, session):
        resp = upload(web)
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert TITLE in html and "A Store Book" in html
        assert kobo_import.REASONS["no_book"] in html  # the reason an unmatched row was left out
        assert FIRST not in html and "Unmatched volume" not in html
        assert 'name="token"' in html and ">Import<" in html
        assert session.query(Highlight).count() == 0  # a dry run writes nothing
        assert len(os.listdir(kobo_import.STASH_ROOT)) == 1

    def test_apply_imports_then_deletes_the_upload(self, web, session):
        upload(web)
        with web.client.session_transaction() as s:
            token = s["highlight_import_token"]
        resp = web.client.post("/admin/highlights/import/apply", data={"token": token})
        assert resp.status_code in (200, 302)
        assert [h.kobo_id for h in session.query(Highlight).all()] == [BM1]
        assert os.listdir(kobo_import.STASH_ROOT) == []
        with web.client.session_transaction() as s:
            assert "highlight_import_token" not in s

    def test_apply_with_another_token_does_nothing(self, web, session):
        upload(web)
        web.client.post("/admin/highlights/import/apply", data={"token": "x" * 22})
        assert session.query(Highlight).count() == 0
        assert len(os.listdir(kobo_import.STASH_ROOT)) == 1

    def test_apply_without_a_session_token_does_nothing(self, web, session):
        upload(web)
        with web.client.session_transaction() as s:
            token = s.pop("highlight_import_token")
        web.client.post("/admin/highlights/import/apply", data={"token": token})
        assert session.query(Highlight).count() == 0

    def test_apply_imports_for_the_chosen_user(self, web, session):
        upload(web, user_id=str(OTHER_USER))
        with web.client.session_transaction() as s:
            token = s["highlight_import_token"]
        web.client.post("/admin/highlights/import/apply", data={"token": token})
        assert [h.user_id for h in session.query(Highlight).all()] == [OTHER_USER]

    def test_not_a_kobo_database_is_refused_and_nothing_is_kept(self, web, tmp_path):
        junk = tmp_path / "junk.sqlite"
        junk.write_bytes(b"not a database")
        resp = upload(web, junk)
        assert resp.status_code == 200
        assert not os.path.isdir(kobo_import.STASH_ROOT) or os.listdir(kobo_import.STASH_ROOT) == []

    def test_apply_for_a_user_deleted_since_the_upload_does_nothing(self, web, session):
        upload(web, user_id=str(OTHER_USER))
        with web.client.session_transaction() as s:
            token = s["highlight_import_token"]
        session.delete(session.query(ub.User).filter_by(id=OTHER_USER).one())
        session.commit()
        web.client.post("/admin/highlights/import/apply", data={"token": token})
        assert session.query(Highlight).count() == 0
        assert os.listdir(kobo_import.STASH_ROOT) == []

    def test_older_copy_page_shows_the_contained_column(self, web):
        path = make_kobo_db(web.path + ".old", [TestRecovery().old_row()],
                            titles=(("file:///mnt/onboard/old.epub", TITLE, AUTHOR),))
        data = {"kobo_db": (open(web.path, "rb"), "KoboReader.sqlite"),
                "older_db": (open(path, "rb"), "old.sqlite"), "user_id": str(USER)}
        html = web.client.post("/admin/highlights/import", data=data,
                               content_type="multipart/form-data").get_data(as_text=True)
        assert "Inside an existing one" in html and "Older copy: text inside a highlight CWA already has" in html

    def test_unknown_user_is_refused(self, web):
        resp = upload(web, user_id="999")
        assert resp.status_code == 200
        assert not os.path.isdir(kobo_import.STASH_ROOT) or os.listdir(kobo_import.STASH_ROOT) == []

    def test_older_copy_adds_recovered_highlights(self, web, session, tmp_path):
        old = TestRecovery()
        path = make_kobo_db(tmp_path / "old.sqlite", [old.old_row()],
                            titles=(("file:///mnt/onboard/old.epub", TITLE, AUTHOR),))
        data = {"kobo_db": (open(web.path, "rb"), "KoboReader.sqlite"),
                "older_db": (open(path, "rb"), "old.sqlite"), "user_id": str(USER)}
        web.client.post("/admin/highlights/import", data=data, content_type="multipart/form-data")
        with web.client.session_transaction() as s:
            token = s["highlight_import_token"]
        web.client.post("/admin/highlights/import/apply", data={"token": token})
        assert sorted(h.kobo_id for h in session.query(Highlight).all()) == sorted([BM1, OLD1])
