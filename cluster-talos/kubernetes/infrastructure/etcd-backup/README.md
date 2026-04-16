# etcd-backup

Daily encrypted etcd snapshots to Backblaze B2 via [siderolabs/talos-backup](https://github.com/siderolabs/talos-backup).

## Architecture

- **Auth:** Talos `kubernetesTalosAPIAccess` (CP machine config) +
  `talos.dev/v1alpha1 ServiceAccount` CR. No client certificate; pods
  authenticate via mounted Talos-issued credentials at
  `/var/run/secrets/talos.dev`. Role scoped to `os:etcd:backup`.
- **CronJob:** daily 03:00 Europe/Brussels, image
  `ghcr.io/siderolabs/talos-backup:v0.1.0-beta.2`.
- **Storage:** B2 bucket `k8s-talos-etcd-backup` (region `eu-central-003`).
  Object path: `<cluster>/<cluster>-<RFC3339-UTC>.snap.age`.
- **Encryption:** [age](https://age-encryption.org) asymmetric. Public key
  baked into CronJob env. Private key in BW SM
  (`SECRET_ETCD_BACKUP_AGE_PRIVATE`) — required for restore.
- **Retention:** 30 days via B2 lifecycle (29 day hide + 1 day delete).
  No app-side prune.

## Prerequisites

The `kubernetesTalosAPIAccess` feature must be enabled in CP machine config
(`talos/templates/controlplane.yaml`):

```yaml
features:
  kubernetesTalosAPIAccess:
    enabled: true
    allowedRoles: [os:etcd:backup]
    allowedKubernetesNamespaces: [etcd-backup]
```

Apply via `talm apply -f nodes/patches/cp-{1,2,3}.yaml --mode auto`.
Hot-reload, no reboot.

## BW SM secrets

| Key                              | Purpose                          |
| -------------------------------- | -------------------------------- |
| `SECRET_B2_ETCD_KEY_ID`          | B2 app key ID (bucket-scoped)    |
| `SECRET_B2_ETCD_APP_KEY`         | B2 app key secret                |
| `SECRET_ETCD_BACKUP_AGE_PRIVATE` | age private key (restore only)   |

## Manual trigger

```bash
kubectl -n etcd-backup create job --from=cronjob/talos-backup \
    talos-backup-manual-$(date +%s)
```

## Restore

```bash
# 1. Fetch encrypted snapshot
b2 file download \
    b2://k8s-talos-etcd-backup/k8s-talos/<file>.snap.age \
    ./snap.age

# 2. Retrieve private key from BW SM
export BWS_SERVER_URL=https://vault.bitwarden.eu
export BWS_ACCESS_TOKEN=$(cat ~/.config/bitwarden-sm-token)
bws secret list e6e860bb-7dc4-4ee0-9229-b42900bcfae2 \
    | jq -r '.[] | select(.key=="SECRET_ETCD_BACKUP_AGE_PRIVATE") | .value' \
    > age.key

# 3. Decrypt
age -d -i age.key -o etcd.snap snap.age

# 4. Recover (per Talos docs)
talosctl bootstrap --recover-from=./etcd.snap -n <new-cp-ip>
```

**Lose the age private key = backups unrecoverable.** Keep an offline copy.

## Known issues

- `ENABLE_COMPRESSION=true` had no measurable effect in v0.1.0-beta.2:
  encrypted file size matches plain snapshot. Re-evaluate on upgrade.
