# Arr stack — SQLite → cnpg Postgres migration

Per-app cnpg cluster (Ombi/tracearr pattern). One PR (this branch) carries all 6 cluster manifests + HR envs. Migration runs **app-by-app**, in this order:

1. radarr-nl (smoke test, 15 MB)
2. prowlarr (38 MB) — single indexer source-of-truth; do early to catch any RSS-flow issues
3. sonarr-nl (45 MB)
4. radarr (92 MB)
5. sonarr (**1017 MB**, the original symptom)
6. bazarr (fresh-PG path, see bottom)

## Prereq — BWS secrets (must exist BEFORE merge)

Add to Bitwarden Secrets Manager — 32-char random per app:

| BWS key | Used by |
|---|---|
| `SECRET_RADARR_NL_POSTGRES_PASSWORD` | radarr-nl-pg + app |
| `SECRET_PROWLARR_POSTGRES_PASSWORD` | prowlarr-pg + app |
| `SECRET_SONARR_NL_POSTGRES_PASSWORD` | sonarr-nl-pg + app |
| `SECRET_RADARR_POSTGRES_PASSWORD`    | radarr-pg + app |
| `SECRET_SONARR_POSTGRES_PASSWORD`    | sonarr-pg + app |
| `SECRET_BAZARR_POSTGRES_PASSWORD`    | bazarr-pg + app |

Without these, ESO sync fails → cnpg bootstrap stalls → arr Pods crash on env injection.

## Migrator tool — pick one before applying any `migrate/job.yaml`

Per-app Job YAMLs sit at `apps/arr/<app>/migrate/job.yaml` with `image: TBD-PER-RUNBOOK`. Out of gitops on purpose (one-shot, deleted after success).

Candidate tools (verify maintenance state at apply time):

1. **`recyclarr-postgres-migrator`-style .NET tool** — purpose-built, schema-aware, FK-respecting. Best fit when active.
2. **`dimitri/pgloader:latest`** — generic SQLite → Postgres, well-known. Needs a small `.load` file per arr to handle identity-column resets. Confirmed-working community recipes exist.
3. **Workstation Python script** — `sqlite3` + `psycopg2`, ~50 LOC, walks SQLite tables in FK order, COPYs into Postgres, resets sequences. Most control; no extra image to vet.

**Recommendation order**: 1 if maintained → 2 with vetted .load file → 3 if both options stale. Pin a digest (`@sha256:...`) for whichever image you pick.

## Per-app migration sequence (Sonarr / Radarr / Prowlarr style)

Replace `<APP>` with the namespace (radarr-nl / prowlarr / sonarr-nl / radarr / sonarr).

```bash
APP=radarr-nl   # change per iteration
K="kubectl --context k8s-talos -n $APP"

# 1) Confirm cnpg cluster is healthy (after PR merge + flux reconcile)
$K get cluster.postgresql.cnpg.io
# expect: STATUS = Cluster in healthy state, READY 2/2

# 2) Verify ESO secret synced
$K get secret ${APP}-db-credentials -o jsonpath='{.data.password}' | base64 -d | head -c8; echo
# expect: 8 chars of the BWS password

# 3) Stop the app so SQLite is quiesced
$K scale deploy/${APP} --replicas=0
$K wait --for=delete pod -l app.kubernetes.io/name=${APP} --timeout=60s

# 4) Bring the app up against EMPTY Postgres → FluentMigrator creates schema
$K scale deploy/${APP} --replicas=1
$K wait --for=condition=available deploy/${APP} --timeout=180s
# Wait for log line: "Migrated to revision N" / "Database migrations completed"
$K logs deploy/${APP} | grep -iE 'migrat|postgres'

# 5) Stop the app again before data copy
$K scale deploy/${APP} --replicas=0
$K wait --for=delete pod -l app.kubernetes.io/name=${APP} --timeout=60s

# 6) Edit migrate/job.yaml, replace image: TBD-PER-RUNBOOK with the vetted image, then:
$K apply -f apps/arr/${APP}/migrate/job.yaml
$K wait --for=condition=complete job/${APP}-pg-migrate --timeout=30m
$K logs job/${APP}-pg-migrate

# 7) Start the app against populated Postgres
$K scale deploy/${APP} --replicas=1

# 8) Verify in UI: Series/Movies list intact, History present, Activity working,
#    queued downloads still queued, indexers connected.

# 9) Keep the SQLite as rollback artefact for 1 week
$K exec deploy/${APP} -- sh -c 'mv /config/*.db /config/*.db-wal /config/*.db-shm /config/preflight/ 2>/dev/null; mkdir -p /config/preflight && mv /config/*.db* /config/preflight/'

# 10) After soak: delete /config/preflight + delete the migrate Job
$K exec deploy/${APP} -- rm -rf /config/preflight
$K delete -f apps/arr/${APP}/migrate/job.yaml
```

**Rollback (within soak window)**:
```bash
$K scale deploy/${APP} --replicas=0
# restore preflight DB
$K exec deploy/${APP%-*}-... -- sh -c 'mv /config/preflight/*.db* /config/'  # adapt
# revert HR env: remove the *__POSTGRES__* block (git revert <commit>) — flux reconciles
$K scale deploy/${APP} --replicas=1
```

If post-merge but pre-migration: simply don't run the Job — arr is still on SQLite. The cnpg cluster sits idle (~120 MB RAM per instance × 2). To roll back fully, `git revert` the PR.

## Bazarr — fresh-PG path (no migrator)

Bazarr Postgres support (since v1.4) has no battle-tested migrator. Path: empty PG + Bazarr rescans state from Sonarr/Radarr APIs.

```bash
K="kubectl --context k8s-talos -n bazarr"

# 1) Cluster healthy + secret synced (same checks as above)

# 2) Stop bazarr; preserve old DB as rollback
$K scale deploy/bazarr --replicas=0
$K exec deploy/bazarr -- sh -c 'mkdir -p /config/db/preflight && mv /config/db/bazarr.db* /config/db/preflight/' || true

# 3) Start bazarr → connects to empty bazarr db → creates schema on first boot
$K scale deploy/bazarr --replicas=1

# 4) In UI: verify Series/Movies sync from Sonarr/Radarr; trigger a full library scan
#    to repopulate wanted-subtitle lists. Download history will be empty (acceptable).

# 5) Soak 1 week, then delete /config/db/preflight.
```

## Verification matrix (per arr, post-migration)

- `kubectl logs deploy/<app>` shows no DB errors, "Connected to Postgres" or equivalent.
- UI Series/Movies/Indexers list count matches pre-migration.
- History tab renders within ~1s (was the user complaint for sonarr).
- New download triggers + imports work end-to-end (test with a small NZB).
- Gatus probe stays green.

## DB sizing reference (pre-migration)

| App | SQLite | Notes |
|---|---|---|
| sonarr | 1017 MB | EpisodeFiles.MediaInfo blobs dominate (~650 MB); History 182 MB / 209k rows |
| bazarr | 169 MB | fresh-PG (no migrator) |
| radarr | 92 MB | |
| sonarr-nl | 45 MB | |
| prowlarr | 38 MB | |
| radarr-nl | 15 MB | smoke test |
