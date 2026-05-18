#!/usr/bin/env bash
# Plex re-analyze hook — Radarr/Sonarr Custom Script connection, On Rename.
#
# After Tdarr rebuilds a media file in place the arr renames it (e.g.
# [HEVC] -> [AV1]). Plex's library scan repaths the file but does NOT
# re-read its streams, so Plex's stored videoCodec stays stale. This
# script triggers a folder scan (so the path is current) and then PUTs
# Plex's per-item /analyze on every item under the renamed folder, on
# both the prod and test Plex servers.
#
# Wired as a Custom Script notification (On Rename) in each arr.
# Lives on the arr config PVC alongside the arr's other operational
# state; not GitOps-managed (the arr's connections aren't either).
set -u

LOG=/config/logs/plex-analyze.txt
mkdir -p /config/logs 2>/dev/null
log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

# --- config -------------------------------------------------------------
PROD_URL="http://plex.plex.svc.cluster.local:32400"
PROD_TOKEN="euGbik-MxzY118VqxpX_"
TEST_URL="http://plex-test-pms.plex-test.svc.cluster.local:32400"
TEST_TOKEN="fKyzWt1h5rTKpSHJEYy1"
# arr media root -> Plex media root
ARR_MEDIA="/mnt/skw-truenas-data/media"
PLEX_MEDIA="/media"
SCAN_WAIT=15   # seconds to let the folder scan settle before analyze
# ------------------------------------------------------------------------

EV="${radarr_eventtype:-${sonarr_eventtype:-}}"
FOLDER="${radarr_movie_path:-${sonarr_series_path:-}}"
# Fallback: a Tdarr in-place rebuild fires MovieFileDelete/EpisodeFileDelete
# (the old file vanishes) — derive the folder from the file path if the
# movie/series path isn't in the env.
if [ -z "$FOLDER" ]; then
  FF="${radarr_moviefile_path:-${sonarr_episodefile_path:-}}"
  [ -z "$FF" ] && FF="${radarr_moviefile_sourcepath:-${sonarr_episodefile_sourcepath:-}}"
  [ -n "$FF" ] && FOLDER=$(dirname "$FF")
fi
log "=== event=${EV:-?} folder=${FOLDER:-?} ==="

# arr fires the script with eventtype=Test when the connection is saved
[ "$EV" = "Test" ] && { log "test event — ok"; exit 0; }
# Runs on any real event that yields a folder (Rename, MovieFileDelete,
# EpisodeFileDelete, Upgrade, ...); analyzing the folder is idempotent.
[ -n "$FOLDER" ] || { log "no folder path in env — skip"; exit 0; }

# translate arr path -> plex path
case "$FOLDER" in
  "$ARR_MEDIA"/*) PLEXPATH="${PLEX_MEDIA}/${FOLDER#"$ARR_MEDIA"/}" ;;
  *)              PLEXPATH="$FOLDER" ;;  # already plex-shaped / unknown
esac
log "plex path = $PLEXPATH"

# analyze every Plex item whose media file sits under $PLEXPATH
analyze_on() {
  local base=$1 tok=$2 lbl=$3
  local secs sk st qt items keys k code

  secs=$(curl -sf --max-time 20 -H 'Accept: application/json' \
    "$base/library/sections?X-Plex-Token=$tok") \
    || { log "$lbl: sections fetch failed"; return; }

  # library section whose root Location is a prefix of PLEXPATH
  read -r sk st < <(printf '%s' "$secs" | jq -r --arg p "$PLEXPATH" '
    .MediaContainer.Directory[]
    | select([.Location[]?.path] | any(. as $l | $p | startswith($l)))
    | "\(.key) \(.type)"' 2>/dev/null | head -1)
  [ -n "${sk:-}" ] || { log "$lbl: no section matches $PLEXPATH"; return; }
  # movies -> type=1 items ; shows -> type=4 (episodes, flat)
  qt=1; [ "$st" = show ] && qt=4
  log "$lbl: section key=$sk type=$st (item type=$qt)"

  # partial scan of the folder so each Part path is current
  curl -sf --max-time 20 -G "$base/library/sections/$sk/refresh" \
    --data-urlencode "path=$PLEXPATH" --data-urlencode "X-Plex-Token=$tok" \
    >/dev/null 2>&1 && log "$lbl: folder scan triggered" \
    || log "$lbl: folder scan request failed (continuing)"
  sleep "$SCAN_WAIT"

  items=$(curl -sf --max-time 60 -H 'Accept: application/json' \
    "$base/library/sections/$sk/all?type=$qt&X-Plex-Token=$tok") \
    || { log "$lbl: items fetch failed"; return; }
  # items whose media Part file sits under the renamed folder
  keys=$(printf '%s' "$items" | jq -r --arg p "$PLEXPATH/" '
    .MediaContainer.Metadata[]?
    | select([.Media[]?.Part[]?.file] | any(. as $f | $f | startswith($p)))
    | .ratingKey' 2>/dev/null | sort -u)
  [ -n "$keys" ] || { log "$lbl: no items under $PLEXPATH"; return; }

  for k in $keys; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
      -X PUT "$base/library/metadata/$k/analyze?X-Plex-Token=$tok")
    log "$lbl: analyze ratingKey=$k -> HTTP $code"
  done
}

analyze_on "$PROD_URL" "$PROD_TOKEN" "prod"
analyze_on "$TEST_URL" "$TEST_TOKEN" "test"
log "done"
exit 0
