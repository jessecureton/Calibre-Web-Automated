# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
Translates reading positions between Kobo sync and KOReader sync.

The Kobo reports positions as KEPUB span ids; KOReader sync clients (KOReader,
Readest) report CREngine XPointers into the EPUB. This package maps one to the
other by matching the text at the position, and decides which reports are real
moves worth forwarding. It is off unless the admin enables
`kobo_kosync_position_bridge` alongside KOReader sync.
"""
