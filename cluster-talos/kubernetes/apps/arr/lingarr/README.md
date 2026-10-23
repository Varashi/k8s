# Lingarr

Subtitle translation (English → Dutch), scoped per show.

## Where configuration lives

- **HelmRelease env**: process-level only (listen URL, database connection, worker count).
- **Lingarr database** (`lingarr-pg`): every in-app setting — Sonarr connection, translation
  service, model, languages, batching, automation, per-show scope, API keys. Manage these in
  the Lingarr UI (Settings / Media) or via `POST /api/setting/multiple/set`.

Setting env vars (`SONARR_URL`, `SERVICE_TYPE`, `ANTHROPIC_MODEL`, …) are deliberately not
used: Lingarr re-applies them on every start, overriding changes made in the UI.

## Rebuild baseline

After a fresh install, complete onboarding (auth disabled — internal-only route), then set:

| Setting | Value |
|---|---|
| `sonarr_url` | in-cluster Sonarr service URL |
| `service_type` | `["anthropic"]` |
| `anthropic_model` | `claude-haiku-5-5` |
| `anthropic_version` | `2023-06-01` |
| `source_languages` | `[{"name":"English","code":"en"}]` |
| `target_languages` | `[{"name":"Dutch","code":"nl"}]` |
| `use_batch_translation` | `true` |
| `telemetry_enabled` | `false` |
| `sonarr_default_include` | `false` (only explicitly included shows are translated) |

API keys come from the `lingarr-secret` ExternalSecret and are written through the
encrypted-setting endpoint (keys `sonarr_api_key`, `anthropic_api_key`):

```bash
kubectl -n lingarr port-forward svc/lingarr 9876 &
for k in ANTHROPIC_API_KEY:anthropic_api_key SONARR_API_KEY:sonarr_api_key; do
  v=$(kubectl -n lingarr get secret lingarr-secret -o jsonpath="{.data.${k%%:*}}" | base64 -d)
  curl -s -X POST -H 'Content-Type: application/json' \
    -d "{\"key\":\"${k##*:}\",\"value\":\"$v\"}" http://localhost:9876/api/setting/encrypted
done
```

The data-protection keys for that encryption live on the `lingarr-config` PVC.
