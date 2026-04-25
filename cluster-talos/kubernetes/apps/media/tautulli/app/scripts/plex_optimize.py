#!/usr/bin/env python3
"""
Tautulli notification script — queue Plex "Optimized for TV" for the
current episode + next 4 in series order, via a per-show playlist.

Each show gets:
  - playlist "Optimize - <ShowTitle>" holding eps we want transcoded
  - optimize job with same title, sourced from that playlist
    (Policy scope=all, Location.uri = library:///directory/<playlist items>)
  - adding an ep to the playlist makes the optimize job pick it up on
    its next scheduled re-evaluation

Script arguments (Tautulli notifier subject):
    {media_type} {rating_key} {grandparent_rating_key}

Env vars (set by Tautulli):
    PLEX_URL, PLEX_TOKEN
"""
import sys
import os
import time
import datetime
import traceback
import urllib.parse

sys.path.insert(0, '/config/scripts/lib')

LOG = '/config/scripts/plex_optimize.log'
OPTIMIZE_TARGET_TAG = 'optimized for tv'
LOOKAHEAD = 2  # current ep + next 2 = 3 total
SESSION_LOOKUP_RETRIES = 6   # ~6s max — Tautulli on_play fires before /status/sessions populates
SESSION_LOOKUP_DELAY = 1.0


def log(msg):
    line = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def _se(ep):
    return f"S{ep.seasonNumber:02d}E{ep.index:02d}"


def _playlist_title(show_title):
    return f"Optimize - {show_title}"


def session_skip_reason(plex, rating_key):
    """Return skip reason string if active session indicates optimize logic
    is unnecessary for this trigger, else None (proceed, incl. lookahead).
    Proceed when:
      - transcoding the 4K original (real pain, build optimized version)
      - direct play of the optimized version (check lookahead eps)
    Skip otherwise:
      - direct play of 4K original (client handles 4K natively)
      - transcoding a 1080p/optimized source (load is light)
      - direct play of any non-optimized sub-4K source (nothing to optimize)
    No matching session after retries → proceed (safe default).

    NB: session.media[selected].videoResolution morphs to the transcoder's
    output rung mid-stream (e.g. '4K' → 'SD' once HLS picks 720x404), and
    Plex casing is inconsistent ('4K' on session vs '4k' on library). Pull
    source resolution from canonical library metadata; only is_optimized
    and transcoding come from the session."""
    has_4k_source = any(
        (getattr(m, 'videoResolution', '') or '').lower() in ('4k', '2160')
        and not getattr(m, 'isOptimizedVersion', False)
        for m in plex.fetchItem(int(rating_key)).media
    )
    for attempt in range(SESSION_LOOKUP_RETRIES):
        for s in plex.sessions():
            if str(getattr(s, 'ratingKey', '')) != str(rating_key):
                continue
            selected = next((m for m in s.media if getattr(m, 'selected', False)), None)
            if selected is None:
                return None
            is_optimized = bool(getattr(selected, 'isOptimizedVersion', False))
            ts_list = getattr(s, 'transcodeSessions', None) or []
            transcoding = any(getattr(t, 'videoDecision', '') == 'transcode' for t in ts_list)
            if transcoding and has_4k_source and not is_optimized:
                return None
            if not transcoding and is_optimized:
                return None
            tag = 'optimized' if is_optimized else 'original'
            action = 'transcode' if transcoding else 'direct play'
            src = '4K' if has_4k_source else 'sub-4K'
            return f"{action} of {src} {tag}"
        if attempt < SESSION_LOOKUP_RETRIES - 1:
            time.sleep(SESSION_LOOKUP_DELAY)
    log(f"session lookup: no match for ratingKey {rating_key} after {SESSION_LOOKUP_RETRIES} tries, proceeding")
    return None


def get_or_create_playlist(plex, show, seed_ep):
    title = _playlist_title(show.title)
    for pl in plex.playlists():
        if pl.title == title:
            return pl, False
    pl = plex.createPlaylist(title, items=[seed_ep])
    log(f"CREATED playlist: {title} (seeded with {_se(seed_ep)})")
    return pl, True


def get_or_create_optimize_job(plex, playlist, show_title, tv_tag_id):
    job_title = _playlist_title(show_title)
    for g in plex.optimizedItems():
        if g.title == job_title:
            return g, False

    uri = f"library:///directory/{urllib.parse.quote_plus(f'/playlists/{playlist.ratingKey}/items')}"
    params = {
        'Item[type]': '42',
        'Item[title]': job_title,
        'Item[target]': '',
        'Item[targetTagID]': str(tv_tag_id),
        'Item[locationID]': '-1',
        'Item[Location][uri]': uri,
        'Item[Policy][scope]': 'all',
        'Item[Policy][value]': '0',
        'Item[Policy][unwatched]': '0',
    }
    plex.query('/playlists/1066/items', method=plex._session.put, params=params)
    log(f"CREATED optimize job: {job_title}")
    for g in plex.optimizedItems():
        if g.title == job_title:
            return g, True
    raise RuntimeError(f"optimize job {job_title!r} not found after creation")


def main():
    if len(sys.argv) < 4:
        log(f"ERROR: expected 3 args, got {len(sys.argv) - 1}: {sys.argv}")
        sys.exit(1)

    media_type, rating_key, grandparent_rating_key = sys.argv[1:4]
    if media_type != 'episode':
        sys.exit(0)

    plex_url = os.environ.get('PLEX_URL', '').rstrip('/')
    plex_token = os.environ.get('PLEX_TOKEN', '')
    if not plex_url or not plex_token:
        log("ERROR: PLEX_URL or PLEX_TOKEN not set")
        sys.exit(1)

    from plexapi.server import PlexServer
    plex = PlexServer(plex_url, plex_token)

    reason = session_skip_reason(plex, rating_key)
    if reason:
        ep = plex.fetchItem(int(rating_key))
        log(f"SKIP ({reason}): {ep.grandparentTitle} {_se(ep)} {ep.title}")
        sys.exit(0)

    ep = plex.fetchItem(int(rating_key))
    ep.reload()
    has_4k = any(
        (getattr(m, 'videoResolution', '') or '').lower() in ('4k', '2160')
        and not getattr(m, 'isOptimizedVersion', False)
        for m in ep.media
    )
    if not has_4k:
        log(f"SKIP (no 4K original): {ep.grandparentTitle} {_se(ep)} {ep.title}")
        sys.exit(0)

    show = plex.fetchItem(int(grandparent_rating_key))
    episodes = show.episodes()
    try:
        idx = next(i for i, e in enumerate(episodes) if str(e.ratingKey) == str(rating_key))
    except StopIteration:
        log(f"ERROR: could not find ratingKey {rating_key} in show {show.title!r}")
        sys.exit(1)
    targets = episodes[idx: idx + 1 + LOOKAHEAD]

    tags = {t.tag.lower(): t.id for t in plex.library.tags('mediaProcessingTarget')}
    tv_tag_id = tags[OPTIMIZE_TARGET_TAG]

    playlist, pl_created = get_or_create_playlist(plex, show, targets[0])
    job, job_created = get_or_create_optimize_job(plex, playlist, show.title, tv_tag_id)

    existing_keys = {str(it.ratingKey) for it in playlist.items()}

    to_add = []
    for t in targets:
        t.reload()
        label = f"{show.title} {_se(t)} {t.title}"
        if any(getattr(m, 'isOptimizedVersion', False) for m in t.media):
            log(f"SKIP (already optimized): {label}")
            continue
        if str(t.ratingKey) in existing_keys:
            log(f"SKIP (already in playlist): {label}")
            continue
        to_add.append(t)
        log(f"QUEUE: {label}")

    if to_add:
        playlist.addItems(to_add)
        log(f"added {len(to_add)} ep(s) to playlist {playlist.title!r}")
    else:
        log(f"no new eps for {show.title!r}")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        with open(LOG, 'a') as f:
            f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} FATAL:\n")
            traceback.print_exc(file=f)
        sys.exit(1)
