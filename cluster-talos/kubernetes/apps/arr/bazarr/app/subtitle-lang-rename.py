#!/usr/bin/env python3
"""Add the language code to subtitle files that have none, using Bazarr's guess.

Sonarr/Radarr import subtitles bundled with a release as `<video>.hi.srt` or
`<video>.2.srt` when they cannot tell the language. Bazarr guesses the language
from the content but only keeps it in its database, so tools that read file
names (Lingarr, Plex) see no language. This renames each such file to
`<video>.<lang>[.N][.hi|.forced].<ext>` with the language Bazarr assigned, then
asks Bazarr to rescan the affected series/movies.

Never overwrites an existing file. DRY_RUN=1 only reports.
Env: BAZARR_URL (default http://bazarr.bazarr.svc:6767), BAZARR_API_KEY.
"""
import collections
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

URL = os.environ.get("BAZARR_URL", "http://bazarr.bazarr.svc:6767").rstrip("/")
KEY = os.environ["BAZARR_API_KEY"]
DRY = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
# everything after the video's base name: optional number, optional modifier, extension
NO_LANG = re.compile(r"^(?P<num>\.\d+)?(?P<mod>\.(hi|sdh|cc|forced))?\.(?P<ext>srt|ass|ssa|vtt)$", re.I)


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def api(method, path, fields=None, timeout=600):
    url = f"{URL}/api/{path}"
    data = None
    if method == "GET" and fields:
        url += "?" + urllib.parse.urlencode(fields, doseq=True)
    elif fields:
        data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"X-API-KEY": KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body[:1] in (b"{", b"[") else None


def renames(video_path, subtitles):
    """Yield (old, new) for language-less external subtitles of one video."""
    if not video_path:
        return
    base = os.path.splitext(video_path)[0]
    for sub in subtitles or []:
        old, lang = sub.get("path"), sub.get("code2")
        if not old or not lang or not old.startswith(base + "."):
            continue
        m = NO_LANG.match(old[len(base):])
        if not m:
            continue  # name already carries a language token
        new = f"{base}.{lang}{m.group('num') or ''}{m.group('mod') or ''}.{m.group('ext')}"
        yield old, new


def main():
    stats = collections.Counter()
    per_title = collections.Counter()
    rescan = []

    def handle(kind, item_id, title, pairs):
        changed = False
        for old, new in pairs:
            if os.path.exists(new):
                stats["skipped, target exists"] += 1
                continue
            if not os.path.exists(old):
                stats["skipped, source gone"] += 1
                continue
            if not DRY:
                os.rename(old, new)
                log(f"renamed {old} -> {os.path.basename(new)}")
            stats["renamed" if not DRY else "would rename"] += 1
            per_title[title] += 1
            changed = True
        if changed:
            rescan.append((kind, item_id))

    series = api("GET", "series", {"start": 0, "length": -1})["data"]
    title = {s["sonarrSeriesId"]: s["title"] for s in series}
    ids = list(title)
    for i in range(0, len(ids), 25):
        for e in api("GET", "episodes", {"seriesid[]": ids[i:i + 25]})["data"]:
            handle("series", e["sonarrSeriesId"], title[e["sonarrSeriesId"]],
                   list(renames(e.get("path"), e.get("subtitles"))))
    for m in api("GET", "movies", {"start": 0, "length": -1})["data"]:
        handle("movies", m["radarrId"], m["title"], list(renames(m.get("path"), m.get("subtitles"))))

    if not DRY:
        for kind, item_id in dict.fromkeys(rescan):
            key = "seriesid" if kind == "series" else "radarrid"
            api("PATCH", kind, {key: item_id, "action": "scan-disk"})
        log(f"queued Bazarr disk scans: {len(set(rescan))}")
    log("summary:", dict(stats))
    for t, n in per_title.most_common(40):
        log(f"  {n:5} {t}")


if __name__ == "__main__":
    sys.exit(main())
