# Position bridge: end-to-end test on a Mac

How `v4.0.7-position-bridge.1` was tested locally on 2026-10-28, so the run can be
repeated for the next build. Everything runs on localhost with throwaway data and
CWA's documented default login (`admin` / `admin123`).

## Images

The cluster is amd64; build that tag for deploy:

```bash
docker buildx build --platform linux/amd64 \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --build-arg VERSION=v4.0.7-position-bridge.N \
  -t docker.studs.house/calibre-web-automated:v4.0.7-position-bridge.N --load .
```

On Apple Silicon without Rosetta, kepubify crashes under qemu, so the first Kobo
download fails in the amd64 image. Run the test against a native build of the
same commit:

```bash
docker buildx build --platform linux/arm64 \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --build-arg VERSION=v4.0.7-position-bridge.N \
  -t cwa-position-bridge:e2e-arm64 --load .
```

## Run CWA

Use Docker volumes. A bind-mounted `/config` from the Mac comes up read-only
(`attempt to write a readonly database`).

```bash
docker run -d --name cwa-e2e --platform linux/arm64 -p 8083:8083 \
  -e PUID=1000 -e PGID=1000 -e TZ=UTC \
  -v cwa-e2e-config:/config -v cwa-e2e-library:/calibre-library \
  -v cwa-e2e-ingest:/cwa-book-ingest cwa-position-bridge:e2e-arm64
docker cp tests/fixtures/position_bridge/anna-karenina.epub cwa-e2e:/cwa-book-ingest/
docker exec cwa-e2e chown 1000:1000 /cwa-book-ingest/anna-karenina.epub
```

In the browser at `http://localhost:8083`:

1. Admin → Basic Configuration: enable Kobo sync and save. Then **restart the
   container**: the Kobo routes only register at startup.
2. CWA Settings: enable KOReader Sync and "Translate reading positions between Kobo sync and KOReader sync"; Submit.
3. Profile → Kobo Sync: open "Generate Kobo Auth URL" once to create the token.

Read the token into a file rather than the terminal:

```bash
docker exec cwa-e2e sqlite3 /config/app.db \
  "select auth_token from remote_auth_token where token_type=1" > token
TOKEN=$(cat token)
UA='Mozilla/5.0 (Linux; U; Android 2.0; en-us;) AppleWebKit/538.1 (KHTML, like Gecko) Version/4.0 Mobile Safari/538.1 (Kobo Touch 0390/4.38.23171)'
```

`curl -s -o /dev/null -w '%{http_code}' http://localhost:8083/kosync/users/auth` → 401.

## Emulate the Kobo

```bash
# Library sync: note the book's RevisionId (its uuid) and keep the sync token.
curl -s -A "$UA" -D sync.h -o sync.json http://localhost:8083/kobo/$TOKEN/v1/library/sync
# Download as the Kobo: converts and stores the KEPUB and records kobo_copy_digest.
curl -s -A "$UA" -o kobo.kepub http://localhost:8083/kobo/$TOKEN/download/<book_id>/kepub
# Report a position: Source is the full zip path of the chapter.
curl -s -A "$UA" -X PUT -H 'Content-Type: application/json' --data @state.json \
  http://localhost:8083/kobo/$TOKEN/v1/library/<uuid>/state
# Later syncs with the earlier token return ChangedReadingState.
curl -s -A "$UA" -H "X-Kobo-SyncToken: $(grep -i '^x-kobo-synctoken' sync.h | cut -d' ' -f2- | tr -d '\r')" \
  http://localhost:8083/kobo/$TOKEN/v1/library/sync
```

`state.json`:

```json
{"ReadingStates":[{"EntitlementId":"<uuid>","LastModified":"<now>",
  "CurrentBookmark":{"LastModified":"<now>","ProgressPercent":48,"ContentSourceProgressPercent":30,
    "Location":{"Source":"epub/text/chapter-4-10.xhtml","Type":"KoboSpan","Value":"kobo.12.1"}},
  "StatusInfo":{"LastModified":"<now>","Status":"Reading","TimesStartedReading":1},
  "Statistics":{"LastModified":"<now>","SpentReadingMinutes":5,"RemainingTimeMinutes":900}}]}
```

Inspect results with
`docker exec cwa-e2e sqlite3 /config/app.db "select ... from kosync_progress / position_bridge_state / kobo_bookmark / kobo_reading_state"`
and `docker logs cwa-e2e 2>&1 | grep position-bridge` (moves and not-moved are not logged; every other reason is).

## Readest in the iOS Simulator

The app builds from Jesse's Readest clone (`tauri ios build --target aarch64-sim`).
The simulator reaches the Mac's `localhost`, and Readest accepts plain `http://localhost`.

- Add OPDS catalog `http://localhost:8083/opds` with the admin login; download *Anna Karenina*.
- In the reader: Settings → Integrations → KOReader: server `http://localhost:8083/kosync`,
  admin login, checksum "File Content", strategy "Always use latest" (silent).

## What was checked (2026-10-28)

| Step | Result |
| --- | --- |
| Kobo PUT `chapter-4-10#kobo.12.1` | kosync_progress `/body/DocFragment[117]/body/section/p[11]/text().0`, resolves to "The natural sciences have just as great an educational value…" |
| Readest open | Sync conflict offered "Kobo (via CWA)"; chose it; opened at that sentence. Readest then sent `…/p[11]` (element-only): `already-there`, nothing echoed |
| Readest reads on, closes | Kobo bookmark `kobo.23.3`, the sentence holding Readest's page start; `LastModified == PriorityTimestamp` |
| Kobo re-PUTs `kobo.23.3` | not-moved; kosync unchanged |
| Kobo PUT earlier chapter, 5 min before Readest's move | `older-than-other-move`; nothing forwarded |
| Same span again with now | not-moved (opening is not a move) |
| Kobo PUT the next paragraph with now | forwarded; Readest moved back on reopen |
| Readest lands on the pushed page, reports its page start | **found a bug**: forwarded as a move, Kobo pushed back ~5 paragraphs. Fixed with the landing rule; rerun: `landing`, Kobo unchanged |
| Readest reads on after landing | `ChangedReadingState` with `kobo.23.1` = Readest's page start |

## Clean up

```bash
docker rm -f cwa-e2e
docker volume rm cwa-e2e-config cwa-e2e-library cwa-e2e-ingest
```
