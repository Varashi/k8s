# Kasten K10

Declarative K10 install + profiles + policies. All credentials via ESO ← Bitwarden Secrets Manager.

## Layout

| File | Purpose |
|------|---------|
| `namespace.yaml` | Privileged PSA |
| `ad-ca-configmap.yaml` | AD CA bundle (base64 from `SECRET_AD_CA_PEM_B64`) for LDAPS trust |
| `externalsecret.yaml` | ESO: `k10-secrets`, `k10-b2-creds`, `k10-vsphere-creds`, `k8s-app-backup-migration-token`, `k10-dr-secret` |
| `helmrelease.yaml` | K10 chart values (LDAPS email-claim path, GPU anti-affinity, uninit-taint toleration) |
| `httproute.yaml` | `kasten.${SECRET_DOMAIN}` via Gateway `main`, `/→/k10/` 301 |
| `clusterrolebinding.yaml` | Admin user → `cluster-admin` (K10's `k10-admin` role lacks RBAC verbs) |
| `profile-b2.yaml` | Location profile `backblaze-b2` |
| `profile-vsphere.yaml` | Infra profile `skw-vcsa` |
| `policypreset.yaml` | `k8s-app-backup` — daily backup, weekly export to B2 |
| `policy.yaml` | `k8s-app-backup` policy, selector `k10.kasten.io/backup=true` |
| `policy-dr.yaml` | `k10-disaster-recovery-policy` — daily×3, backup+export to B2 |

## BW SM secrets consumed

- `SECRET_VSPHERE_USERNAME` / `SECRET_VSPHERE_PASSWORD` — reused from CCM/CSI
- `SECRET_BACKBLAZE_B2_KEY_ID` / `SECRET_BACKBLAZE_B2_APP_KEY`
- `SECRET_EXTERNAL_DNS_KERBEROS_PASSWORD` — reused as LDAPS bindPW
- `SECRET_KASTEN_MIGRATION_TOKEN` — export receiveString (migrated from RKE2 Kasten)
- `SECRET_KASTEN_DR_PASSPHRASE` — DR encryption passphrase
- `SECRET_KASTEN_TALOS_CLUSTER_ID` = `4747fdfe-cd2b-4eea-acac-52e03d32de7d` (= kube-system ns UID) — required at DR restore

Substvars (via `kasten-substvars` ES):
- `SECRET_LDAP_BASE_DN`, `SECRET_LDAP_BIND_DN`, `SECRET_KASTEN_ADMIN_USER`, `SECRET_AD_CA_PEM_B64`, `SECRET_KASTEN_MIGRATION_TOKEN`

## Adding an app to backup

Label its namespace:

```
kubectl label ns <app> k10.kasten.io/backup=true
```

Policy `k8s-app-backup` picks it up on next reconcile.

## Cross-cluster migration from RKE2

Proven on ocis migration 2026-04-13. Same flow for any RKE2 app whose data must move to Talos.

### Source side (RKE2) one-time fix

RKE2 `k8s-app-backup` policy historically had **no `profile` in `exportParameters`** — exports went to the (now-deleted) `skw-truenas-nfs` FileStore. Patch once:

```
kubectl --context k8s -n kasten-io patch policy k8s-app-backup --type=json \
  -p='[{"op":"add","path":"/spec/actions/1/exportParameters/profile","value":{"name":"backblaze-b2","namespace":"kasten-io"}}]'
```

### Pre-flight

- Delete any **standalone Pods** (no ownerRef) in the source namespace — break dataOnly restore with `specType=pod, spec type not supported`:

  ```
  kubectl --context k8s get pod -A -o json | \
    jq -r '.items[] | select(.metadata.ownerReferences==null) | "\(.metadata.namespace)/\(.metadata.name)"'
  ```

### Migration (ad-hoc, per app)

1. Source: `BackupAction` on the app → `ExportAction` with `profile: backblaze-b2` (receiveString can be a stale one; K10 auto-generates a fresh `migrationToken` + `receiveString` bound to B2 and stores the token in a secret `export-<id>-migration-token` in `kasten-io`).
2. Copy the new token value to Talos:

   ```
   TOKEN=$(kubectl --context k8s -n kasten-io get secret export-<id>-migration-token -o jsonpath='{.data.migrationToken}' | base64 -d)
   kubectl --context k8s-talos -n kasten-io create secret generic ocis-migration-token \
     --from-literal=migrationToken="$TOKEN" --dry-run=client -o yaml | kubectl --context k8s-talos apply -f -
   ```

3. Patch Talos import policy (e.g. `rancher-k8s-app-restore`) with the new `receiveString` (from the ExportAction spec).
4. Run import → produces `RestorePointContent` in the app ns.
5. Create a `RestorePoint` referencing the content (importAction does not do this automatically):

   ```yaml
   apiVersion: apps.kio.kasten.io/v1alpha1
   kind: RestorePoint
   metadata: {name: <app>-migrate-rp, namespace: <app>}
   spec:
     restorePointContentRef: {name: <content-name>}
   ```

6. Suspend Flux `apps` kustomization, delete target PVC(s), run `RestoreAction` with `dataOnly: true`. Kasten creates fresh PVC on the source SC.

### Storage class mismatch

Source SC (e.g. `skw-vsan-stripe`) likely absent on Talos. Two options:

- **TransformSet + policy restore** (preferred — future-proof):

  ```yaml
  apiVersion: config.kio.kasten.io/v1alpha1
  kind: TransformSet
  metadata: {name: sc-remap-vsan, namespace: kasten-io}
  spec:
    transforms:
    - subject: {resource: persistentvolumeclaims}
      name: remap-sc
      json:
      - {op: replace, path: /spec/storageClassName, value: vsan}
  ```

  Attach via `RestoreAction.spec.transforms: [{transformSetRef: {name, namespace}}]`. The RestoreAction CRD schema hides `transforms` — apply with `kubectl create --validate=false`; the server accepts it.

- **Compat SC**: create an identically-named SC on Talos pointing to the same provisioner (quick workaround, long-term clutter).

### Post-restore

- Update `apps/<app>/pvc.yaml` in git to match the SC and size Kasten actually created.
- Resume Flux `apps`, scale workload back up, verify.

## DR policy shape gotchas

K10 validation rejects:

- `backup` action without `backupParameters.profile` → "BackupParameters cannot be nil for Veeam Kasten Disaster Recovery policy"
- `exportCatalogSnapshot: true` without a separate `export` action → "DR policy with enabled export catalog snapshot must have export action"
- `exportCatalogSnapshot: false` WITH an `export` action → "DR policy with disabled export catalog snapshot should not have export action"

Current shape: both `backup` and `export` actions, `exportCatalogSnapshot: true`.

Frequency `@daily` + retention `daily: 3`. Hourly frequency + daily retention is wasteful — 23/24 hourly snapshots prune immediately.

## DR restore (target cluster)

1. Install K10 chart with equivalent values (same `dashboardURL`, same B2 profile definition).
2. Materialize `k10-dr-secret` (key = `key`) containing `SECRET_KASTEN_DR_PASSPHRASE`.
3. Materialize B2 credentials secret + `backblaze-b2` profile.
4. Run import:

   ```
   k10tools restore --from-backup \
     --cluster-id 4747fdfe-cd2b-4eea-acac-52e03d32de7d \
     --profile backblaze-b2 \
     --passphrase-file /path/to/passphrase
   ```

5. Catalog imports → RestorePoints, Policies, Profiles reappear; app PVCs restorable from B2 exports.

## Operational gotchas

- RWO vSphere CSI PVCs can Multi-Attach during K10 upgrade. Recovery: scale `jobs-svc metering-svc logging-svc` to 0, delete stale VolumeAttachments, scale back.
- Policy controller caches stale Failed status after profile becomes Success — restart `deploy/controllermanager-svc`.
- ESO `force-sync` annotation does not refresh already-materialized secrets reliably. Delete the target Secret to force re-materialization.
