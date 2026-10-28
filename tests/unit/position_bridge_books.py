# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Tiny synthetic EPUB/KEPUB pairs for the position bridge tests."""

import re
import zipfile

CONTAINER = (
    '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
    'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
OPF = (
    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
    '<manifest><item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/></manifest>'
    '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>'
)
HEAD = '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>'
CH1 = HEAD + '<p>A scene break follows.</p><p>* * *</p><p>Then more.</p></body></html>'
CH2 = HEAD + (
    '<p>Happy families are all alike; every unhappy family is unhappy in its own way.</p>'
    '<p>Everything was in confusion in the Oblonskys’ house.</p>'
    '<p>* * *</p><p>The wife had discovered that the husband was carrying on an intrigue.</p>'
    '</body></html>'
)


def kepubify_like(xhtml):
    """Wrap each sentence-ish run of a paragraph in koboSpans, roughly as kepubify does."""
    counter = [0]

    def para(m):
        counter[0] += 1
        parts = re.findall(r"[^.;]+[.;]?\s*", m.group(2))
        spans = "".join(
            f'<span class="koboSpan" id="kobo.{counter[0]}.{i + 1}">{p}</span>'
            for i, p in enumerate(parts)
        )
        return f"<p{m.group(1) or ''}>{spans}</p>"

    body = re.sub(r"<p(\s[^>]*)?>(.*?)</p>", para, xhtml)
    return body.replace("<body>", '<body><div id="book-columns"><div id="book-inner">').replace(
        "</body>", "</div></div></body>"
    )


def write_book(path, chapters):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        for name, body in chapters.items():
            z.writestr(f"OEBPS/text/{name}", body)
    return str(path)
