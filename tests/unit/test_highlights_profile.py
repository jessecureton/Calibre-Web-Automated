# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Profile settings for highlight sync: Readwise token, Readest password, admin device controls."""

import hashlib
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import admin, ub, usermanagement, web
from cps.progress_syncing import settings
from cps.progress_syncing.highlights.store import Highlight, HighlightDevice, upsert_from_side

pytestmark = pytest.mark.unit

USER, OTHER_USER, BOOK = 3, 4, 1
TOKEN = "rw-secret-token-0123456789"
NOW = datetime(2026, 10, 30, 13, 0, 0, tzinfo=timezone.utc)
LAYOUT = "{% block body %}{% endblock %}"


@pytest.fixture
def session(monkeypatch):
    engine = create_engine("sqlite://")
    ub.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(ub.User(id=USER, name="jcureton", email="j@example.test", role=0))
    s.add(ub.User(id=OTHER_USER, name="other", email="o@example.test", role=0))
    s.commit()
    monkeypatch.setattr(ub, "session", s)
    yield s
    s.close()


@pytest.fixture
def flags(monkeypatch):
    state = {"on": True, "ever": True}
    monkeypatch.setattr(settings, "is_highlight_sync_enabled", lambda: state["on"])
    monkeypatch.setattr(settings, "highlight_sync_ever_enabled", lambda: state["ever"])
    return state


@pytest.fixture
def world(monkeypatch, session, flags):
    """The real profile and admin routes and the real user_edit.html, minus the library and Babel."""
    app = Flask(__name__)
    app.config.update(SECRET_KEY="test", TESTING=True, LOGIN_DISABLED=True, SERVER_NAME="cwa.test")
    app.register_blueprint(web.web)
    app.register_blueprint(admin.admi)
    app.before_request_funcs.clear()  # both blueprints' hooks need a configured library
    app.after_request_funcs.clear()
    templates = os.path.join(os.path.dirname(admin.__file__), "templates")
    app.jinja_loader = ChoiceLoader([DictLoader({"layout.html": LAYOUT}), FileSystemLoader(templates)])
    app.jinja_env.globals["_"] = lambda s, **k: s % k if k else s
    app.jinja_env.globals["csrf_token"] = lambda: "tok"

    state = SimpleNamespace(app=app, me=session.get(ub.User, USER), admin=True, session=session)
    state.me.role_admin = lambda: state.admin
    state.me.role_passwd = lambda: True
    app.jinja_env.globals["current_user"] = state.me
    cfg = SimpleNamespace(config_kobo_sync=False, config_login_type=0, config_anonbrowse=0,
                          get_mail_server_configured=lambda: False)

    def render(template, **kw):
        return app.jinja_env.get_template(template).render(**kw)

    for module in (web, admin):
        monkeypatch.setattr(module, "current_user", state.me)
        monkeypatch.setattr(module, "render_title_template", render)
        monkeypatch.setattr(module, "_", app.jinja_env.globals["_"])
        monkeypatch.setattr(module, "config", cfg)
    monkeypatch.setattr(usermanagement, "config", SimpleNamespace(config_allow_reverse_proxy_header_login=False))
    monkeypatch.setattr(web, "calibre_db", SimpleNamespace(speaking_language=lambda **k: []))
    monkeypatch.setattr(admin, "calibre_db", SimpleNamespace(speaking_language=lambda **k: []))
    monkeypatch.setattr(web, "get_available_locale", lambda: [])
    monkeypatch.setattr(admin, "get_available_locale", lambda: [])
    state.client = app.test_client()
    return state


def post_profile(world, **form):
    # The fields the profile handler always reads
    data = {"email": "j@example.test", "kindle_mail": "", "default_language": "all", "locale": "en", **form}
    return world.client.post("/me", data=data, follow_redirects=False)


def user(session, user_id=USER):
    session.expire_all()
    return session.get(ub.User, user_id)


def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class TestReadwiseToken:
    def test_saved_and_masked_on_reload(self, world, session):
        resp = post_profile(world, readwise_token=TOKEN)
        assert resp.status_code == 302
        assert user(session).readwise_token == TOKEN
        html = world.client.get("/me").get_data(as_text=True)
        assert TOKEN not in html
        assert 'name="readwise_token"' in html
        assert "Readwise token is set" in html

    def test_not_set_is_said_plainly(self, world):
        html = world.client.get("/me").get_data(as_text=True)
        assert "Readwise token is not set" in html

    def test_blank_save_keeps_the_token(self, world, session):
        post_profile(world, readwise_token=TOKEN)
        post_profile(world, readwise_token="")
        assert user(session).readwise_token == TOKEN
        post_profile(world, readwise_token="   ")
        assert user(session).readwise_token == TOKEN

    def test_remove_checkbox_clears_it(self, world, session):
        post_profile(world, readwise_token=TOKEN)
        post_profile(world, remove_readwise_token="on")
        assert user(session).readwise_token is None

    def test_remove_wins_over_a_typed_token(self, world, session):
        post_profile(world, readwise_token=TOKEN)
        post_profile(world, readwise_token="another", remove_readwise_token="on")
        assert user(session).readwise_token is None

    @pytest.mark.parametrize("typed", [f"Token {TOKEN}", f"Bearer {TOKEN}", f"  token {TOKEN}  ", f"\t{TOKEN}\n"])
    def test_prefixes_and_whitespace_are_stripped(self, world, session, typed):
        post_profile(world, readwise_token=typed)
        assert user(session).readwise_token == TOKEN

    def test_a_token_replaces_the_old_one(self, world, session):
        post_profile(world, readwise_token=TOKEN)
        post_profile(world, readwise_token="new-token-value")
        assert user(session).readwise_token == "new-token-value"

    def test_the_token_never_reaches_the_log(self, world, caplog):
        with caplog.at_level("DEBUG"):
            post_profile(world, readwise_token=TOKEN)
        assert TOKEN not in caplog.text


class TestGeneratedPassword:
    def test_stores_md5_and_shows_it_once(self, world, session):
        resp = world.client.post("/me/highlight-sync-password")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        password = re.search(r'<code id="highlight-sync-password"[^>]*>([^<]+)</code>', html).group(1)
        assert len(password) >= 24
        assert user(session).highlight_sync_key_md5 == md5(password)
        assert password not in str(user(session).highlight_sync_key_md5)
        assert resp.headers["Cache-Control"] == "no-store"
        # the instructions name the host and the user to type into Readest
        assert "http://cwa.test" in html and "jcureton" in html
        assert "Settings" in html and "BookOrbit" in html
        # never shown again
        again = world.client.get("/me").get_data(as_text=True)
        assert password not in again
        assert 'id="highlight-sync-password"' not in again

    def test_matches_how_readest_hashes_it(self, world, session):
        """BookOrbitForm sends md5(password) of the text typed, untrimmed, as lowercase hex."""
        html = world.client.post("/me/highlight-sync-password").get_data(as_text=True)
        password = re.search(r'<code id="highlight-sync-password"[^>]*>([^<]+)</code>', html).group(1)
        stored = user(session).highlight_sync_key_md5
        assert stored == hashlib.md5(password.encode("utf-8")).hexdigest()
        assert re.fullmatch(r"[0-9a-f]{32}", stored)
        assert password == password.strip() and re.fullmatch(r"[A-Za-z0-9_-]+", password)

    def test_generating_again_replaces_the_old_password(self, world, session):
        first = world.client.post("/me/highlight-sync-password").get_data(as_text=True)
        old = user(session).highlight_sync_key_md5
        second = world.client.post("/me/highlight-sync-password").get_data(as_text=True)
        assert user(session).highlight_sync_key_md5 != old
        assert first != second

    def test_generated_password_authenticates_against_bookorbit(self, world, session):
        from cps.progress_syncing.highlights import bookorbit
        html = world.client.post("/me/highlight-sync-password").get_data(as_text=True)
        password = re.search(r'<code id="highlight-sync-password"[^>]*>([^<]+)</code>', html).group(1)
        def authenticate(key):
            with world.app.test_request_context("/", headers={"X-Auth-User": "JCureton", "X-Auth-Key": key}):
                return bookorbit._authenticate()
        assert authenticate(md5(password)).id == USER
        assert authenticate(md5(password + "x")) is None

    def test_only_post_is_routed(self, world, session):
        # (a GET falls through to a catch-all trailing-slash route in web.py, so look at the rule itself)
        rule = next(world.app.url_map.iter_rules("web.highlight_sync_password"))
        assert rule.methods - {"OPTIONS"} == {"POST"}
        assert user(session).highlight_sync_key_md5 is None

    def test_refused_when_highlight_sync_was_never_on(self, world, session, flags):
        flags.update(on=False, ever=False)
        assert world.client.post("/me/highlight-sync-password").status_code == 404
        assert user(session).highlight_sync_key_md5 is None


class TestVisibility:
    def test_section_shown_when_on(self, world, flags):
        flags.update(on=True, ever=True)
        html = world.client.get("/me").get_data(as_text=True)
        assert 'name="readwise_token"' in html and "Generate highlight-sync password for Readest" in html

    def test_section_shown_when_it_was_once_on(self, world, flags):
        flags.update(on=False, ever=True)
        html = world.client.get("/me").get_data(as_text=True)
        assert 'name="readwise_token"' in html

    def test_section_hidden_when_never_enabled(self, world, flags):
        flags.update(on=False, ever=False)
        html = world.client.get("/me").get_data(as_text=True)
        assert 'name="readwise_token"' not in html
        assert "highlight-sync-password" not in html and "Readwise" not in html

    def test_saving_a_profile_with_the_section_hidden_leaves_the_token(self, world, session, flags):
        post_profile(world, readwise_token=TOKEN)
        flags.update(on=False, ever=False)
        post_profile(world)  # the hidden section sends nothing
        assert user(session).readwise_token == TOKEN


def add_held(session, count=2, user_id=USER):
    for n in range(count):
        row = upsert_from_side(session, user_id, BOOK, "readest", f"key{n}",
                               {"text": f"text {n}", "xp0": "/body/DocFragment[2]/body/p/text().0"}, NOW)
        row.readest_held = True
    session.commit()


class TestAdminSection:
    def test_shows_device_firmware_and_held_count(self, world, session):
        session.add(HighlightDevice(user_id=USER, readest_device_id="dev-abc-123", kobo_firmware="4.38.23171"))
        add_held(session, 2)
        html = world.client.get(f"/admin/user/{USER}").get_data(as_text=True)
        assert 'id="highlight-readest-device">dev-abc-123<' in html
        assert 'id="highlight-kobo-firmware">4.38.23171<' in html
        assert 'id="highlight-held-count">2<' in html
        assert "Reset Readest device" in html and "Apply held deletions" in html

    def test_shows_none_when_nothing_is_recorded(self, world):
        html = world.client.get(f"/admin/user/{USER}").get_data(as_text=True)
        assert 'id="highlight-readest-device">none<' in html
        assert 'id="highlight-kobo-firmware">none<' in html
        assert 'id="highlight-held-count">0<' in html
        assert "Reset Readest device" not in html  # nothing to reset

    def test_admin_section_not_on_the_profile_page(self, world):
        html = world.client.get("/me").get_data(as_text=True)
        assert "Reset Readest device" not in html and "Apply held deletions" not in html

    def test_hidden_when_never_enabled(self, world, flags):
        flags.update(on=False, ever=False)
        html = world.client.get(f"/admin/user/{USER}").get_data(as_text=True)
        assert "Reset Readest device" not in html and "Apply held deletions" not in html

    def test_reset_clears_the_device_id_only(self, world, session):
        session.add(HighlightDevice(user_id=USER, readest_device_id="dev-abc-123", kobo_firmware="4.38.23171"))
        session.add(HighlightDevice(user_id=OTHER_USER, readest_device_id="other-dev"))
        session.commit()
        resp = world.client.post(f"/admin/user/{USER}/highlight-sync/reset-device")
        assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/admin/user/{USER}")
        session.expire_all()
        mine = session.query(HighlightDevice).filter_by(user_id=USER).one()
        assert mine.readest_device_id is None and mine.kobo_firmware == "4.38.23171"
        assert session.query(HighlightDevice).filter_by(user_id=OTHER_USER).one().readest_device_id == "other-dev"

    def test_reset_with_no_device_row_is_harmless(self, world, session):
        resp = world.client.post(f"/admin/user/{USER}/highlight-sync/reset-device")
        assert resp.status_code == 302
        assert session.query(HighlightDevice).count() == 0

    def test_apply_held_deletions_releases_that_users_holds(self, world, session):
        add_held(session, 2)
        add_held(session, 1, user_id=OTHER_USER)
        resp = world.client.post(f"/admin/user/{USER}/highlight-sync/apply-held")
        assert resp.status_code == 302
        session.expire_all()
        mine = session.query(Highlight).filter_by(user_id=USER).all()
        assert all(not h.readest_held and h.deleted_at is not None for h in mine) and len(mine) == 2
        other = session.query(Highlight).filter_by(user_id=OTHER_USER).one()
        assert other.readest_held and other.deleted_at is None

    def test_both_actions_are_post_only(self, world):
        for path in ("reset-device", "apply-held"):
            assert world.client.get(f"/admin/user/{USER}/highlight-sync/{path}").status_code == 405

    @pytest.mark.parametrize("path", ["reset-device", "apply-held"])
    def test_admin_only(self, world, session, path):
        session.add(HighlightDevice(user_id=USER, readest_device_id="dev-abc-123"))
        add_held(session, 1)
        world.admin = False
        resp = world.client.post(f"/admin/user/{USER}/highlight-sync/{path}")
        assert resp.status_code == 403
        session.expire_all()
        assert session.query(HighlightDevice).one().readest_device_id == "dev-abc-123"
        held = session.query(Highlight).one()
        assert held.readest_held and held.deleted_at is None

    def test_unknown_user_is_not_found(self, world):
        assert world.client.post("/admin/user/99/highlight-sync/reset-device").status_code == 404
        assert world.client.post("/admin/user/99/highlight-sync/apply-held").status_code == 404


class TestKoboImportGate:
    PATH = f"/admin/user/{USER}/highlight-sync/no-kobo-import"

    def test_shows_whether_the_kobo_import_is_done(self, world, session):
        html = world.client.get(f"/admin/user/{USER}").get_data(as_text=True)
        assert 'id="highlight-kobo-import">not done<' in html
        assert "This user has no Kobo highlights to import" in html
        session.add(HighlightDevice(user_id=USER, kobo_import_done=True))
        session.commit()
        html = world.client.get(f"/admin/user/{USER}").get_data(as_text=True)
        assert 'id="highlight-kobo-import">done<' in html
        assert "This user has no Kobo highlights to import" not in html

    def test_marks_the_import_done_for_that_user_only(self, world, session):
        session.add(HighlightDevice(user_id=USER, kobo_firmware="4.38.23852"))
        session.commit()
        resp = world.client.post(self.PATH)
        assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/admin/user/{USER}")
        session.expire_all()
        mine = session.query(HighlightDevice).filter_by(user_id=USER).one()
        assert mine.kobo_import_done is True and mine.kobo_firmware == "4.38.23852"
        assert session.query(HighlightDevice).filter_by(user_id=OTHER_USER).first() is None

    def test_makes_the_device_row_when_there_is_none(self, world, session):
        world.client.post(self.PATH)
        session.expire_all()
        assert session.query(HighlightDevice).filter_by(user_id=USER).one().kobo_import_done is True

    def test_admin_only_post_only_and_unknown_user(self, world, session):
        assert world.client.get(self.PATH).status_code == 405
        assert world.client.post("/admin/user/99/highlight-sync/no-kobo-import").status_code == 404
        world.admin = False
        assert world.client.post(self.PATH).status_code == 403
        assert session.query(HighlightDevice).count() == 0

    def test_is_its_own_form_with_csrf_and_a_confirm(self, world, session):
        forms = forms_of(world.client.get(f"/admin/user/{USER}").get_data(as_text=True))
        form = next(f for f in forms if f["attrs"].get("action") == self.PATH)
        assert form is not main_form(forms) and form["attrs"]["method"].lower() == "post"
        assert any(i.get("name") == "csrf_token" and i.get("value") for i in form["inputs"])
        assert len(submits(form)) == 1 and form["attrs"]["data-confirm"]
        assert form["attrs"]["onsubmit"] == "return confirm(this.dataset.confirm);"


class Forms(HTMLParser):
    """The page's forms: attributes, and the inputs and buttons inside each (nesting is tracked)."""

    def __init__(self):
        super().__init__()
        self.stack, self.forms = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            form = {"attrs": attrs, "inputs": [], "buttons": []}
            self.forms.append(form)
            self.stack.append(form)
        elif self.stack and tag == "input":
            self.stack[-1]["inputs"].append(attrs)
        elif self.stack and tag == "button":
            self.stack[-1]["buttons"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "form" and self.stack:
            self.stack.pop()


def forms_of(html):
    parser = Forms()
    parser.feed(html)
    return parser.forms


def main_form(forms):
    return next(f for f in forms if f["attrs"].get("role") == "form")


def submits(form):
    """Buttons that submit the form: a <button> with no type is a submit button too."""
    return [b for b in form["buttons"] if b.get("type", "submit") == "submit"]


class TestEnterCannotTriggerAnAction:
    """Enter in a text field presses the form's first submit button, so the main form must have none."""

    def test_profile_main_form_has_no_submit_button(self, world):
        main = main_form(forms_of(world.client.get("/me").get_data(as_text=True)))
        assert submits(main) == []
        assert not any("formaction" in b for b in main["buttons"])

    def test_admin_main_form_has_no_submit_button(self, world, session):
        session.add(HighlightDevice(user_id=USER, readest_device_id="dev-abc-123"))
        add_held(session, 1)
        main = main_form(forms_of(world.client.get(f"/admin/user/{USER}").get_data(as_text=True)))
        assert submits(main) == []
        assert not any("formaction" in b for b in main["buttons"])

    def test_profile_action_form_is_its_own_form_with_csrf(self, world):
        forms = forms_of(world.client.get("/me").get_data(as_text=True))
        form = next(f for f in forms if f["attrs"].get("action") == "/me/highlight-sync-password")
        assert form is not main_form(forms) and form["attrs"]["method"].lower() == "post"
        assert any(i.get("name") == "csrf_token" and i.get("value") for i in form["inputs"])
        assert len(submits(form)) == 1 and "formaction" not in form["buttons"][0]

    def test_admin_action_forms_are_their_own_forms_with_csrf(self, world, session):
        session.add(HighlightDevice(user_id=USER, readest_device_id="dev-abc-123"))
        add_held(session, 3)
        forms = forms_of(world.client.get(f"/admin/user/{USER}").get_data(as_text=True))
        for path in ("reset-device", "apply-held"):
            form = next(f for f in forms if f["attrs"].get("action") == f"/admin/user/{USER}/highlight-sync/{path}")
            assert form is not main_form(forms) and form["attrs"]["method"].lower() == "post"
            assert any(i.get("name") == "csrf_token" and i.get("value") for i in form["inputs"])
            assert len(submits(form)) == 1 and "formaction" not in form["buttons"][0]
            assert form["attrs"]["data-confirm"]  # asked before it runs

    def test_confirms_name_the_held_count_and_survive_quotes_in_a_name(self, world, session):
        session.get(ub.User, USER).name = "Jo\"hn's"
        session.add(HighlightDevice(user_id=USER, readest_device_id="dev-abc-123"))
        add_held(session, 3)
        forms = forms_of(world.client.get(f"/admin/user/{USER}").get_data(as_text=True))
        by_action = {f["attrs"].get("action", ""): f["attrs"] for f in forms}
        assert "3 highlights" in by_action[f"/admin/user/{USER}/highlight-sync/apply-held"]["data-confirm"]
        reset = by_action[f"/admin/user/{USER}/highlight-sync/reset-device"]
        assert "Jo\"hn's" in reset["data-confirm"]  # parsed back from an escaped attribute
        assert reset["onsubmit"] == "return confirm(this.dataset.confirm);"  # no user text in the script

    def test_enter_in_the_main_form_goes_to_the_normal_save(self, world, session):
        """What Enter submits is the main form's own fields, posted to its own (empty) action: the profile save."""
        main = main_form(forms_of(world.client.get("/me").get_data(as_text=True)))
        assert "action" not in main["attrs"] and main["attrs"]["method"].lower() == "post"
        names = {i.get("name") for i in main["inputs"]}
        assert {"csrf_token", "email", "readwise_token"} <= names
        post_profile(world, readwise_token=TOKEN)  # the same fields, posted to /me
        assert user(session).readwise_token == TOKEN
        assert user(session).highlight_sync_key_md5 is None  # no password was generated
