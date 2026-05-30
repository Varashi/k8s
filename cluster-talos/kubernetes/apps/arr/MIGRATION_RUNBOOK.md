# Arr stack — SQLite → cnpg Postgres migration

**STATUS: COMPLETE 2026-05-30.** All 6 arr apps live on Postgres. SQLite files stashed at `/config/preflight/` (1-week soak before deletion). This document is now reference for future migrations of the same shape.

## Outcome

| App | SQLite pre | Postgres post | Notes |
|---|---|---|---|
| sonarr | 1017 MB | **440 MB** | -57%; EpisodeFiles 676 MB → 97 MB via pglz on `rawStreamData` |
| bazarr | 169 MB | (fresh, ~few MB) | fresh-PG path, re-syncs from arrs on schedule |
| radarr | 92 MB | smaller | |
| sonarr-nl | 45 MB | smaller | |
| prowlarr | 38 MB | smaller | |
| radarr-nl | 15 MB | smaller | smoke test target |

## ⚠ Learnings (what we did differently from the original plan)

1. **Bundle-merge gotcha**: PR #159 carried HR env additions for all 6 apps at once. On merge, flux reconciled all 6 simultaneously → arr Pods rolled with Postgres env before cnpg clusters finished bootstrapping → all apps crashed at startup. **Recovery was emergency scale-to-0 + serial migration**. **Lesson for next time**: split into one PR for cnpg+ES (no app impact) + one PR per arr to add HR env (gated on that arr's migration). Or — accept that the bundled merge becomes the start signal for an immediate serial migration window.

2. **Wiki's 7-table DELETE list is incomplete**: the Servarr wiki recommends `DELETE` on 7 known-seeded tables before pgloader. We hit a PK collision on `NamingConfig` (radarr-nl had it pre-populated too). **Use TRUNCATE-all instead** (preserves schema, clears all FluentMigrator seed data, resets sequences). See refined procedure below.

3. **Bazarr image does NOT honor `POSTGRES_*` env**: `ghcr.io/home-operations/bazarr:1.5.6` (and likely others) only reads `postgresql:` block in `/config/config/config.yaml`. The env block in the HelmRelease is documentary only. **Bazarr requires direct config.yaml patching** before it'll connect to PG. Procedure below.

## Migration order used

1. radarr-nl (smoke test, 15 MB) → validated TRUNCATE-all pattern
2. prowlarr (38 MB) → 73k rows / 1.7s
3. sonarr-nl (45 MB) → 36k rows / 1.8s
4. radarr (92 MB) → 135k rows / 3.2s
5. sonarr (**1017 MB**) → 439k rows / 23s
6. bazarr (fresh-PG, no pgloader) → schema bootstrapped, library re-syncs from arrs

## Per-app refined procedure (Sonarr / Radarr / Prowlarr style)

Replace `<APP>` with the namespace + deploy name. For NL variants, also adjust `<ROLE>` (`sonarr_nl`/`radarr_nl`) and `<DB>` (`sonarr_nl_main`/`radarr_nl_main`).

```bash
APP=radarr-nl
NS=$APP
PGC=${APP}-pg
ROLE=${APP//-/_}           # radarr-nl → radarr_nl
DB=${ROLE}_main
PVC=${APP}-config
SQF=${APP%-nl}.db          # radarr-nl → radarr.db ; prowlarr → prowlarr.db (manual for prowlarr)
K="kubectl --context k8s-talos -n $NS"
IMG='ghcr.io/roxedus/pgloader@sha256:1a7a86ad56623c00ee714ee4969913ed5c6f59ac9785073e2ffd1bea9cc54d31'

# 1) Bring up against empty PG so FluentMigrator creates the schema
$K scale deploy/$APP --replicas=1
$K wait --for=condition=available deploy/$APP --timeout=180s

# 2) Scale down so SQLite is quiesced and PVC detachable
$K scale deploy/$APP --replicas=0
$K wait --for=delete pod -l app.kubernetes.io/name=$APP --timeout=60s

# 3) TRUNCATE all tables (clears every FluentMigrator-seeded row, resets sequences)
$K exec -i ${PGC}-1 -- psql -d $DB <<'SQL'
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE 'TRUNCATE TABLE "' || r.tablename || '" RESTART IDENTITY CASCADE';
  END LOOP;
END $$;
SQL

# 4) Run pgloader as one-shot pod (mounts config PVC RW — SQLite WAL mode needs writable dir for -shm/-wal sidecars)
PASS=$($K get secret ${APP}-db-credentials -o jsonpath='{.data.password}' | base64 -d)
$K run pgloader-$APP --rm -i --restart=Never --image=$IMG \
  --overrides='{
    "spec": {
      "securityContext": {"runAsUser":0,"runAsGroup":0,"fsGroup":1000,"supplementalGroups":[568]},
      "containers": [{
        "name": "pgloader-'$APP'",
        "image": "'$IMG'",
        "securityContext": {"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"seccompProfile":{"type":"RuntimeDefault"}},
        "args": ["--with","quote identifiers","--with","data only","--with","prefetch rows = 100","--with","batch size = 1MB","/config/'$SQF'","postgresql://'$ROLE':'$PASS'@'$PGC'-rw.'$NS'.svc:5432/'$DB'"],
        "volumeMounts": [{"name":"config","mountPath":"/config"}]
      }],
      "volumes": [{"name":"config","persistentVolumeClaim":{"claimName":"'$PVC'"}}],
      "restartPolicy": "Never"
    }
  }'
# Watch the output. 0 errors = clean.

# 5) Scale up; app boots on populated Postgres
$K scale deploy/$APP --replicas=1
$K wait --for=condition=available deploy/$APP --timeout=180s

# 6) Verify via API: counts match pre-migration baseline.
# 7) Stash old SQLite as rollback artefact:
$K exec deploy/$APP -- sh -c "mkdir -p /config/preflight && mv /config/${SQF} /config/${SQF}-shm /config/${SQF}-wal /config/preflight/ 2>/dev/null; ls -lh /config/preflight/"

# 8) After 1-week soak: rm -rf /config/preflight/ via exec.
```

The `--with "prefetch rows = 100"` and `--with "batch size = 1MB"` flags are recommended by the Servarr wiki for large DBs (sonarr 1 GB). Harmless on small ones.

## Bazarr — fresh-PG, config.yaml direct edit

Bazarr image doesn't honor `POSTGRES_*` env. Patch config.yaml + restart.

```bash
NS=bazarr
POD=$(kubectl --context k8s-talos -n $NS get pod -l app.kubernetes.io/name=bazarr -o jsonpath='{.items[0].metadata.name}')
PASS=$(kubectl --context k8s-talos -n $NS get secret bazarr-db-credentials -o jsonpath='{.data.password}' | base64 -d)

# Copy out, edit with python+yaml, copy back, restart
kubectl --context k8s-talos -n $NS cp $POD:/config/config/config.yaml /tmp/bz.yaml
PASS="$PASS" python3 -c "
import os, yaml
cfg = yaml.safe_load(open('/tmp/bz.yaml'))
cfg['postgresql'] = {'enabled': True, 'host': 'bazarr-pg-rw.bazarr.svc', 'port': 5432,
                     'database': 'bazarr', 'username': 'bazarr',
                     'password': os.environ['PASS'], 'url': ''}
yaml.safe_dump(cfg, open('/tmp/bz.yaml','w'), sort_keys=True, default_flow_style=False)
"
kubectl --context k8s-talos -n $NS cp /tmp/bz.yaml $POD:/config/config/config.yaml
kubectl --context k8s-talos -n $NS delete pod $POD --wait=false
kubectl --context k8s-talos -n $NS wait --for=condition=available deploy/bazarr --timeout=180s

# After restart: Bazarr connects to PG, creates empty schema, UI shows empty wanted-subtitle lists.
# Library state re-syncs from Sonarr/Radarr on next polling cycle (or trigger Series Search in UI).
# Stash old SQLite:
kubectl --context k8s-talos -n $NS exec deploy/bazarr -- sh -c 'mkdir -p /config/db/preflight && mv /config/db/bazarr.db /config/db/bazarr.db-shm /config/db/bazarr.db-wal /config/db/preflight/ 2>/dev/null'
```

## BWS secrets (already in place)

| BWS key | Used by | State |
|---|---|---|
| `SECRET_RADARR_NL_POSTGRES_PASSWORD` | radarr-nl-pg + app | ✓ created 2026-05-30 |
| `SECRET_PROWLARR_POSTGRES_PASSWORD` | prowlarr-pg + app | ✓ |
| `SECRET_SONARR_NL_POSTGRES_PASSWORD` | sonarr-nl-pg + app | ✓ |
| `SECRET_RADARR_POSTGRES_PASSWORD` | radarr-pg + app | ✓ |
| `SECRET_SONARR_POSTGRES_PASSWORD` | sonarr-pg + app | ✓ |
| `SECRET_BAZARR_POSTGRES_PASSWORD` | bazarr-pg + app | ✓ |

## Rollback (within soak window)

```bash
APP=radarr-nl  # any arr
NS=$APP
K="kubectl --context k8s-talos -n $NS"
$K scale deploy/$APP --replicas=0
$K wait --for=delete pod -l app.kubernetes.io/name=$APP --timeout=60s
# Manually edit HR or git revert the env block (flux reconciles)
# Restore preflight:
$K exec deploy/$APP -- sh -c 'mv /config/preflight/*.db* /config/' || \
$K exec -ti $($K get pod | awk 'NR==2{print $1}') -- sh -c 'mv /config/preflight/*.db* /config/'  # if deploy has 0 pods, attach to a debug pod
$K scale deploy/$APP --replicas=1
```

For bazarr: same pattern with `/config/db/preflight/`.

## Post-soak cleanup (~2026-06-06)

```bash
for ns in sonarr sonarr-nl radarr radarr-nl prowlarr; do
  kubectl --context k8s-talos -n $ns exec deploy/$ns -- rm -rf /config/preflight
done
kubectl --context k8s-talos -n bazarr exec deploy/bazarr -- rm -rf /config/db/preflight
```

Then optionally delete the `migrate/job.yaml` files (we didn't use them — went with `kubectl run --rm -i` interactive runs instead).
