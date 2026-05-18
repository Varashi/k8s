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

Script arguments (Tautulli notifier subject) — two forms:
    play:        {media_type} {rating_key} {grandparent_rating_key}
    newly_added: newly_added {media_type} {rating_key}

play (on_play/on_resume/on_change):
    session-state gate + current ep + LOOKAHEAD next eps
newly_added (on_created / Recently Added):
    queue freshly-added eps for already-tracked shows. {media_type} may be
    episode, or season/show when Tautulli groups a batch of additions — all
    resolve to a flat episode list.

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
# newly_added: a grouped season/show notification expands to ALL episodes;
# only those added this recently are actually new — the rest is back catalog.
NEW_EPISODE_MAX_AGE_HOURS = 24


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


def _queue_new_episode(plex, ep, playlists_by_title, tags):
    """Queue one freshly-added episode, if its show is tracked and the ep is a
    4K original not already optimized / already queued."""
    show_title = ep.grandparentTitle
    label = f"{show_title} {_se(ep)} {ep.title}"
    has_4k = any(
        (getattr(m, 'videoResolution', '') or '').lower() in ('4k', '2160')
        and not getattr(m, 'isOptimizedVersion', False)
        for m in ep.media
    )
    if not has_4k:
        log(f"SKIP (no 4K original): {label}")
        return

    playlist = playlists_by_title.get(_playlist_title(show_title))
    if playlist is None:
        log(f"SKIP (show not tracked): {label}")
        return
    if any(getattr(m, 'isOptimizedVersion', False) for m in ep.media):
        log(f"SKIP (already optimized): {label}")
        return
    if str(ep.ratingKey) in {str(it.ratingKey) for it in playlist.items()}:
        log(f"SKIP (already in playlist): {label}")
        return

    # playlist exists → optimize job should too; recreate defensively if not
    get_or_create_optimize_job(plex, playlist, show_title, tags[OPTIMIZE_TARGET_TAG])
    playlist.addItems([ep])
    log(f"QUEUE (new episode): {label}")


def _is_recent(ep):
    """True if the episode was added within NEW_EPISODE_MAX_AGE_HOURS — used to
    keep back catalog out when a grouped season/show notification is expanded."""
    added = getattr(ep, 'addedAt', None)
    if not added:
        return False
    return datetime.datetime.now() - added <= datetime.timedelta(hours=NEW_EPISODE_MAX_AGE_HOURS)


def handle_new_episode(plex, item_type, rating_key):
    """Recently-added trigger. Tautulli groups a batch of additions: a single
    new episode arrives as 'episode', several eps of one season as 'season',
    several seasons (or a whole new show) as 'show'. fetchItem on a season/show
    yields ALL its episodes, not just the new ones — so filter to eps added in
    the last NEW_EPISODE_MAX_AGE_HOURS before queuing.

    Queue only for shows already tracked by an 'Optimize - <Show>' playlist
    (someone watches them in 4K); untracked shows are skipped so a brand-new
    series doesn't drag the whole library into the optimize queue.

    No session gate, no lookahead — these eps ARE the new frontier."""
    item = plex.fetchItem(int(rating_key))
    if item.type == 'episode':
        candidates = [item]
    elif item.type in ('season', 'show'):
        candidates = item.episodes()
    else:
        log(f"SKIP (unsupported type {item.type!r}): ratingKey {rating_key}")
        return

    eps = [e for e in candidates if _is_recent(e)]
    if item.type != 'episode' or len(eps) != len(candidates):
        log(f"{item.type} ratingKey {rating_key} ({item.title!r}): {len(candidates)} "
            f"ep(s), {len(eps)} added in last {NEW_EPISODE_MAX_AGE_HOURS}h")
    if not eps:
        log(f"SKIP (no newly-added eps): {item.type} {item.title!r}")
        return

    playlists_by_title = {pl.title: pl for pl in plex.playlists()}
    tags = {t.tag.lower(): t.id for t in plex.library.tags('mediaProcessingTarget')}
    for ep in eps:
        ep.reload()
        _queue_new_episode(plex, ep, playlists_by_title, tags)


def handle_play(plex, rating_key, grandparent_rating_key):
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


def main():
    args = sys.argv[1:]

    # newly_added subject: "newly_added {media_type} {rating_key}"
    # play subject:        "{media_type} {rating_key} {grandparent_rating_key}"
    if args and args[0] == 'newly_added':
        if len(args) < 3:
            log(f"ERROR: newly_added expected 3 args, got {len(args)}: {sys.argv}")
            sys.exit(1)
        mode, item_type, rating_key = 'newly_added', args[1], args[2]
        if item_type not in ('episode', 'season', 'show'):
            sys.exit(0)
    else:
        if len(args) < 3:
            log(f"ERROR: play expected 3 args, got {len(args)}: {sys.argv}")
            sys.exit(1)
        mode = 'play'
        media_type, rating_key, grandparent_rating_key = args[:3]
        if media_type != 'episode':
            sys.exit(0)

    plex_url = os.environ.get('PLEX_URL', '').rstrip('/')
    plex_token = os.environ.get('PLEX_TOKEN', '')
    if not plex_url or not plex_token:
        log("ERROR: PLEX_URL or PLEX_TOKEN not set")
        sys.exit(1)

    from plexapi.server import PlexServer
    plex = PlexServer(plex_url, plex_token)

    if mode == 'newly_added':
        handle_new_episode(plex, item_type, rating_key)
    else:
        handle_play(plex, rating_key, grandparent_rating_key)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        with open(LOG, 'a') as f:
            f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} FATAL:\n")
            traceback.print_exc(file=f)
        sys.exit(1)
