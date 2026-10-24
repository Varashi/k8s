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
| `max_batch_size` | `100` (0 = whole file in one request → truncated output) |
| `anthropic_request_template` | `{"model":"{model}","max_tokens":16000,"output_config":{"effort":"low"},"system":"{systemPrompt}","messages":[{"role":"user","content":"{userMessage}"}]}` |
| `automation_enabled` / `max_translations_per_run` | `true` / `50` (schedule `0 4 * * *`, runs in UTC) |
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

## Gotchas

- **Anthropic API key must be scoped to a workspace.** Lingarr cannot send the
  `anthropic-workspace-id` header, so an unscoped key fails with
  `invalid_request_error`. Create the key inside a dedicated workspace (also gives
  a per-workspace spend limit and usage view).
- **Default request template** uses `max_tokens: 1024`; with batch size 0 the
  response is cut off (`stop_reason: max_tokens`). Use the template above.
- **Stale job lock after a pod restart.** If the pod is replaced while a
  Hangfire job holds a distributed lock, the next job of that type hangs without
  logging. Check `hangfire.lock` in the `lingarr` database and delete rows older
  than the current pod.
- **Source subtitles must be in sync.** Lingarr keeps the source timestamps, so
  re-time the English source first (e.g. Bazarr sync against the embedded track).
- Automation walks episodes in internal id order (grouped per season) and skips
  episodes that already have a target-language subtitle.
