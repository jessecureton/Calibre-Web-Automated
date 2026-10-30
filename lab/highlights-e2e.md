# Highlight sync: end-to-end test on a Mac

How highlight sync was tested locally on 2026-10-30, so the run can be repeated for
the next build. It uses the same local setup as `lab/e2e.md`: an arm64 image, named
volumes, *Anna Karenina* and CWA's default local login. In addition, it uses:

- a curl Kobo that speaks the reading-services annotation API;
- Readest in the iOS simulator, connected as a BookOrbit client;
- a fake Readwise server on the Mac.

## CWA

Build the arm64 image and start CWA as in `lab/e2e.md`. Use `TZ=America/New_York`
to match the lab. Point Readwise at the fake server with
`-e READWISE_API_BASE=http://host.docker.internal:9999`.

Turn everything on. Settings live in two databases, so set them directly and then
restart, because the Kobo routes and the Readwise job register at startup:

```bash
docker exec cwa-e2e sqlite3 /config/app.db "update settings set config_kobo_sync=1, config_kobo_proxy=0"
docker exec cwa-e2e sqlite3 /config/cwa.db "update cwa_settings set koreader_sync_enabled=1,
  kobo_kosync_position_bridge=1, kobo_readest_highlight_sync=1,
  kobo_readest_highlight_sync_ever=1, highlight_sync_kobo_firmware='4.38.23852'"
docker restart cwa-e2e
```

Then, in the browser:
1. Profile → **Generate highlight-sync password for Readest**. The password is shown once.
2. Profile → Kobo Sync → **Create/View** to make the Kobo token.
3. Read the token into a file, as in `lab/e2e.md`.
4. Set a dummy Readwise token for the fake server:
   `update user set readwise_token='fake-local-token' where id=1`.

## Fake Kobo

The User-Agent must carry the tested firmware:
`… (Kobo Touch 0383/4.38.23852)`. Keep a cookie jar, because reading-services calls
are authenticated by the session cookie set during Kobo sync.

```bash
curl -s -A "$UA" -c jar -b jar http://localhost:8083/kobo/$TOKEN/v1/initialization   # reading_services_host = CWA
curl -s -A "$UA" -c jar -b jar http://localhost:8083/kobo/$TOKEN/v1/library/sync
curl -s -A "$UA" -c jar -b jar -o kobo.kepub http://localhost:8083/kobo/$TOKEN/download/<book_id>/kepub
curl -s -A "$UA" -b jar -c jar -X PATCH -H 'Content-Type: application/json' --data @patch.json \
  http://localhost:8083/api/v3/content/<uuid>/annotations                                # 204
curl -s -A "$UA" -b jar -X POST -H 'Content-Type: application/json' \
  --data '[{"ContentId":"<uuid>","etag":"W/\"0\""}]' http://localhost:8083/api/v3/content/checkforchanges
curl -s -A "$UA" -b jar "http://localhost:8083/api/v3/content/<uuid>/annotations?limit=100"
```

`patch.json` takes the device's wire shape, recorded on the Sage. It is in
`docs/superpowers/specs/2026-10-30-kobo-iphone-highlight-sync-design.md` in
homelab_infra:

```json
{"updatedAnnotations": [{"id": "<uuid4>", "type": "highlight", "clientLastModifiedUtc": "<now>",
  "highlightedText": "<the span text>",
  "location": {"span": {"chapterFilename": "epub/text/chapter-4-10.xhtml", "chapterProgress": 0.257,
    "chapterTitle": "IV.10", "startPath": "span#kobo\\.12\\.1", "startChar": 0,
    "endPath": "span#kobo\\.12\\.2", "endChar": 80}}}]}
```

A delete is `{"updatedAnnotations": [], "deletedAnnotationIds": ["<id>"]}`.

## Readest

1. Open Readest's Online Library and download the book again from CWA's OPDS feed. A copy
   from an earlier CWA instance has a different checksum and is reported as unmatched.
2. Open the book.
3. Go to Settings → Integrations → **BookOrbit** and enter:
   - server `http://localhost:8083`;
   - username `admin`;
   - the generated password.
4. Press Connect.
5. Turn off BookOrbit's **Sync Reading Progress**, stats and book states. KOReader sync
   and the position bridge already carry position, and two progress paths raise
   Readest's conflict prompt.

## Fake Readwise

`python3 -I fake_readwise.py fake_readwise.log` (kept out of the repo) implements:

- `GET /api/v2/books/`
- `POST /api/v2/highlights/` (returns `modified_highlights`)
- `GET /api/v2/export/`
- `PATCH` and `DELETE /api/v2/highlights/<id>/`

It logs each request and whether a `Token` header was present, never its value. The
outbox runs every 5 minutes.

## What was checked (2026-10-30)

| Step | Result |
| --- | --- |
| Kobo PATCH, `kobo.12.1`/0 to `kobo.12.2`/80 | 204; row with `xp0 …/p[11]/text().0`, `xp1 …/p[11]/text().160` |
| Readest connects and opens the book | device recorded; CWA offered `add=[1]`; Readest acked; the highlight is in Readest's annotation list and drawn on exactly that sentence |
| Highlight "agree" in Readest | arrives as a Readest change, with `xp0 …/p[12]/text().16`, `xp1 …text().21` |
| Kobo `checkforchanges` with `W/"0"`, plus an unknown uuid | names only the CWA book; the GET answers 200, `ETag: W/"cwa-2"`, 2 entries; the Readest one is `kobo.13.1` 16–21, which is exactly "agree" |
| Delete "agree" in Readest | "Readest deleted [2]"; `checkforchanges` with `W/"cwa-2"` names the book again; the GET holds only the Kobo highlight |
| Kobo PATCH deletes its highlight | CWA offers Readest `delete=[1]`, and it disappears from the page |
| New Kobo highlight, then the outbox | `POST /api/v2/highlights/` with `cwa://<uuid>/highlight/3`, export check, `readwise_id` 5001 recorded |
| Kobo deletes it, then the outbox | `DELETE /api/v2/highlights/5001/`; the token never appears in CWA's log |

One cosmetic gap: a Kobo highlight carries no colour, and it showed in Readest as red.

## Clean up

```bash
docker rm -f cwa-e2e
docker volume rm cwa-e2e-config cwa-e2e-library cwa-e2e-ingest
```
