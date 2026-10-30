# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Syncs highlights between a Kobo e-reader and the Readest app.

The store (`store.py`) keeps one row per highlight, shared by both sides, and
decides which edit wins. It is off unless the admin enables
`kobo_readest_highlight_sync`.
"""
