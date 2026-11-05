#!/usr/bin/env python3
"""Notify (via a Home Assistant webhook) about subtitles Lingarr keeps failing on.

Lingarr has no outbound notifications. Its nightly automation retries every
file that still lacks a translation, so a file that fails for a structural
reason (unparseable model output, karaoke fragments, ...) fails again every
night without anyone noticing. This reads Lingarr's translation requests and
reports each file whose latest request failed within LOOKBACK_HOURS and that
has failed at least MIN_FAILURES times in total; one-off hiccups are ignored.
Sends nothing when there is nothing to report.

DRY_RUN=1 only prints the message.
Env: LINGARR_URL (default http://lingarr.lingarr.svc:9876), HA_WEBHOOK_URL,
LOOKBACK_HOURS (default 30), MIN_FAILURES (default 2), MAX_LISTED (default 5).
"""
import collections
import datetime as dt
import json
import os
import re
import urllib.request

URL = os.environ.get("LINGARR_URL", "http://lingarr.lingarr.svc:9876").rstrip("/")
WEBHOOK = os.environ.get("HA_WEBHOOK_URL", "")
DRY = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
LOOKBACK = dt.timedelta(hours=float(os.environ.get("LOOKBACK_HOURS", "30")))
MIN_FAILURES = int(os.environ.get("MIN_FAILURES", "2"))
MAX_LISTED = int(os.environ.get("MAX_LISTED", "5"))
RETRY_SUFFIX = re.compile(r"( \[part \d+/\d+\]| \[retry\])+$")


def log(*a):
    print(dt.datetime.now().strftime("%F %T"), *a, flush=True)


def parse_ts(s):
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def short(title):
    """'whisper-asr - Show (2025) - S01E02 - 002 - Name [WEBDL-1080p]...mkv' -> 'Show (2025) - S01E02'"""
    t = title.split(" · ")[0].removeprefix("whisper-asr - ")
    t = re.sub(r"\s*\[.*$", "", t)
    m = re.match(r"(.*? - S\d+E\d+)\b", t)
    return m.group(1) if m else t


def requests():
    out, page = [], 1
    while True:
        q = f"{URL}/api/translationrequest/requests?pageSize=1000&pageNumber={page}"
        d = json.load(urllib.request.urlopen(q, timeout=60))
        out += d["items"]
        if page * d["pageSize"] >= d["totalCount"] or not d["items"]:
            return out
        page += 1


def main():
    by_file = collections.defaultdict(list)
    for r in requests():
        # requests made via /api/translate/content (e.g. by Bazarr or the whisper
        # adapter) have no file; the adapter's retries add " [retry]" / " [part k/n]"
        key = r.get("subtitleToTranslate") or RETRY_SUFFIX.sub("", r.get("title") or "")
        by_file[key].append(r)
    since = dt.datetime.now(dt.timezone.utc) - LOOKBACK
    stuck, recent_failed = [], 0
    for reqs in by_file.values():
        reqs.sort(key=lambda r: parse_ts(r["createdAt"]))
        last = reqs[-1]
        if last["status"] != "Failed" or parse_ts(last["createdAt"]) < since:
            continue
        recent_failed += 1
        fails = sum(r["status"] == "Failed" for r in reqs)
        if fails >= MIN_FAILURES:
            stuck.append((fails, short(last["title"]), (last.get("errorMessage") or "").strip()))
    log(f"{recent_failed} file(s) failed recently, {len(stuck)} failing repeatedly")
    if not stuck:
        return
    stuck.sort(reverse=True)
    lines = [f"- {t} ({n}x): {e[:60]}" for n, t, e in stuck[:MAX_LISTED]]
    if len(stuck) > MAX_LISTED:
        lines.append(f"- ... and {len(stuck) - MAX_LISTED} more")
    msg = {
        "title": f"Lingarr: {len(stuck)} subtitle(s) keep failing",
        "message": "\n".join(lines),
    }
    log(json.dumps(msg, ensure_ascii=False))
    if DRY or not WEBHOOK:
        return
    req = urllib.request.Request(WEBHOOK, data=json.dumps(msg).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    log("webhook", urllib.request.urlopen(req, timeout=30).status)


if __name__ == "__main__":
    main()
