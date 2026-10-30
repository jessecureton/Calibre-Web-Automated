# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

from flask_babel import lazy_gettext as N_

from cps import db, logger, ub
from cps.progress_syncing import settings
from cps.progress_syncing.highlights import readwise
from cps.services.worker import CalibreTask


class TaskReadwiseHighlights(CalibreTask):
    """Copy every user's dirty highlights to their Readwise account (the outbox)."""

    def __init__(self, task_message=N_('Sending highlights to Readwise')):
        super(TaskReadwiseHighlights, self).__init__(task_message)
        self.log = logger.create()

    def run(self, worker_thread):
        # The setting may have been turned off since the job was scheduled.
        if not settings.is_highlight_sync_enabled():
            self._handleSuccess()
            return
        app_db_session = ub.get_new_session_instance()
        calibre = db.CalibreDB(expire_on_commit=False, init=True)
        try:
            lookup = lambda book_id: readwise.book_info_from(calibre, book_id)  # noqa: E731
            users = app_db_session.query(ub.User).filter(
                ub.User.readwise_token.isnot(None), ub.User.readwise_token != "").all()
            for user in users:
                readwise.push_dirty(app_db_session, user, book_info=lookup)
            self._handleSuccess()
        except Exception as e:
            app_db_session.rollback()
            # Class name only: an exception's text can carry highlight text or a URL.
            self.log.error(f"Readwise outbox failed: {type(e).__name__}")
            self._handleError(f"Readwise outbox failed: {type(e).__name__}")
        finally:
            calibre.session.close()
            app_db_session.remove()

    @property
    def name(self):
        return "Readwise highlights"

    @property
    def is_cancellable(self):
        return False
