# Position bridge lab notes

- `lab/test.sh <pytest args>` runs the unit tests in a container built from the
  stock CWA v4.0.8 image (`lab/Dockerfile.test`). Optional:
  `POSITION_BRIDGE_REAL_LIBRARY` (a directory of `<id>.epub` / `<id>.kepub` pairs,
  never committed) and `READEST_CLONE` (a Readest checkout, for the CREngine oracle test).
- `lab/readest-oracle/` generates reference XPointers with Readest's own code.
- `lab/e2e.md` is the local end-to-end test with an emulated Kobo and the Readest iOS simulator.

## Deploy and roll back

The homelab runs `docker.studs.house/calibre-web-automated:v4.0.8-position-bridge.N`
(image line in `nomad/calibre-web.nomad` in homelab_infra), with the bridge off
until it is turned on in CWA Settings.

Before the first deploy, `app.db` was copied to
`/config/kobo-migration/app.db.pre-position-bridge`.

To roll back, set the image back to `crocodilestick/calibre-web-automated:v4.0.8`
and apply `nomad_job.calibre-web`. Stock `cwa_db.py` drops the unknown
`kobo_kosync_position_bridge` column from `cwa_settings`, and the leftover
`position_bridge_state` table in `app.db` is harmless. Turning the setting off is
enough to stop the bridge without redeploying.
