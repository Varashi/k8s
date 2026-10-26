#!/usr/bin/env python3
"""Bazarr `whisperai` API on top of whisper.cpp's whisper-server.

Implements the subset of the whisper-asr-webservice API that Bazarr's whisperai
provider uses, so the GPU pool can sit behind the same Service as the CPU pool:

  POST /asr              query: task, language, output, encode, video_file
                         multipart field `audio_file`: raw s16le 16 kHz mono PCM
                         (encode=false, what Bazarr sends) or a WAV (encode=true)
                         task=translate (TRANSLATE_MODE=lingarr): transcribe in the
                         source language, translate the cues with Lingarr, return SRT
  POST /detect-language  same input; returns {detected_language, language_code}
  GET  /ready            200 when whisper-server is up (busy or not), 503 otherwise
  GET  /health           200 while the adapter process runs

Error responses to /asr carry an EMPTY body: Bazarr stores the body as the
subtitle without checking the status, and an empty body is rejected as
"Invalid subtitle" for that one request, without throttling the provider.

Python stdlib only (runs from a ConfigMap on the official python image).
"""

import contextlib
import http.client
import http.server
import json
import logging
import os
import re
import struct
import threading
import time
import urllib.parse

LOG = logging.getLogger("whisper-adapter")


def env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9000"))
WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8080").rstrip("/")
WHISPER_TIMEOUT = float(os.environ.get("WHISPER_TIMEOUT", "7200"))
# Decoding fields sent with every /inference call. whisper-server copies its CLI
# defaults per request and lets request fields override them, so these pin the
# decoding even if the server args drift. Comma-separated key=value.
WHISPER_FIELDS = os.environ.get("WHISPER_FIELDS", "beam_size=5,best_of=5,max_context=0")
# task=translate handling (the turbo model cannot translate itself):
#   lingarr  transcribe here in the source language, translate the cue texts with
#            Lingarr's /api/translate/content, return the English SRT (default)
#   forward  relay the whole request to TRANSLATE_UPSTREAM (a pool that can translate)
#   reject   501 with an empty body
TRANSLATE_MODE = os.environ.get("TRANSLATE_MODE", "lingarr").strip().lower()
TRANSLATE_UPSTREAM = os.environ.get("TRANSLATE_UPSTREAM", "").rstrip("/")
TRANSLATE_TIMEOUT = float(os.environ.get("TRANSLATE_TIMEOUT", "7200"))
TRANSLATE_TARGET = os.environ.get("TRANSLATE_TARGET", "en")
LINGARR_URL = os.environ.get("LINGARR_URL", "http://lingarr.lingarr.svc:9876").rstrip("/")
LINGARR_API_KEY = os.environ.get("LINGARR_API_KEY", "")
# Lingarr's own job timeout is 30 min; wait a bit longer so its error reaches us.
LINGARR_TIMEOUT = float(os.environ.get("LINGARR_TIMEOUT", "1920"))
# whisper ISO-639-1 code -> Lingarr language code, where they differ (as Bazarr maps them)
LINGARR_LANGUAGES = {"zh": "zh-CN"}
DETECT_SECONDS = float(os.environ.get("DETECT_SECONDS", "30"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(1 << 30)))

FILTER_ENABLED = env_bool("FILTER_ENABLED", True)
FILTER_REPEAT_CUES = int(os.environ.get("FILTER_REPEAT_CUES", "3"))
FILTER_REPEAT_PHRASE = int(os.environ.get("FILTER_REPEAT_PHRASE", "4"))
FILTER_LOOP_MIN_WORDS = int(os.environ.get("FILTER_LOOP_MIN_WORDS", "4"))
FILTER_FILLER_MAX_SECONDS = float(os.environ.get("FILTER_FILLER_MAX_SECONDS", "5"))

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # s16le mono

# whisper.cpp full language names (whisper_lang_str_full) -> ISO-639-1 codes.
LANGUAGES = {
    "english": "en", "chinese": "zh", "german": "de", "spanish": "es", "russian": "ru",
    "korean": "ko", "french": "fr", "japanese": "ja", "portuguese": "pt", "turkish": "tr",
    "polish": "pl", "catalan": "ca", "dutch": "nl", "arabic": "ar", "swedish": "sv",
    "italian": "it", "indonesian": "id", "hindi": "hi", "finnish": "fi", "vietnamese": "vi",
    "hebrew": "he", "ukrainian": "uk", "greek": "el", "malay": "ms", "czech": "cs",
    "romanian": "ro", "danish": "da", "hungarian": "hu", "tamil": "ta", "norwegian": "no",
    "thai": "th", "urdu": "ur", "croatian": "hr", "bulgarian": "bg", "lithuanian": "lt",
    "latin": "la", "maori": "mi", "malayalam": "ml", "welsh": "cy", "slovak": "sk",
    "telugu": "te", "persian": "fa", "latvian": "lv", "bengali": "bn", "serbian": "sr",
    "azerbaijani": "az", "slovenian": "sl", "kannada": "kn", "estonian": "et",
    "macedonian": "mk", "breton": "br", "basque": "eu", "icelandic": "is", "armenian": "hy",
    "nepali": "ne", "mongolian": "mn", "bosnian": "bs", "kazakh": "kk", "albanian": "sq",
    "swahili": "sw", "galician": "gl", "marathi": "mr", "punjabi": "pa", "sinhala": "si",
    "khmer": "km", "shona": "sn", "yoruba": "yo", "somali": "so", "afrikaans": "af",
    "occitan": "oc", "georgian": "ka", "belarusian": "be", "tajik": "tg", "sindhi": "sd",
    "gujarati": "gu", "amharic": "am", "yiddish": "yi", "lao": "lo", "uzbek": "uz",
    "faroese": "fo", "haitian creole": "ht", "pashto": "ps", "turkmen": "tk", "nynorsk": "nn",
    "maltese": "mt", "sanskrit": "sa", "luxembourgish": "lb", "myanmar": "my", "tibetan": "bo",
    "tagalog": "tl", "malagasy": "mg", "assamese": "as", "tatar": "tt", "hawaiian": "haw",
    "lingala": "ln", "hausa": "ha", "bashkir": "ba", "javanese": "jw", "sundanese": "su",
    "cantonese": "yue",
}

OUTPUT_FORMATS = {"srt": "srt", "vtt": "vtt", "txt": "text", "json": "verbose_json"}
CONTENT_TYPES = {"srt": "text/plain; charset=utf-8", "vtt": "text/vtt; charset=utf-8",
                 "txt": "text/plain; charset=utf-8", "json": "application/json"}

# --------------------------------------------------------------------------- filter

# Whole-sentence texts that whisper produces over music / silence / end cards.
FILLER_SENTENCES = {
    "thank you", "thank you very much", "thank you so much", "thanks", "thanks a lot",
    "thanks for watching", "thank you for watching", "thank you so much for watching",
    "see you next time", "see you in the next video", "the end",
    "well be right back", "ill be right back", "im going to be right back",
    "so im going to be right back", "bye", "bye bye", "goodbye", "you",
    "please subscribe", "like and subscribe", "subscribe to my channel",
}
FILLER_PREFIX = re.compile(
    r"^(subtitles?|subtitled|captions?|captioned|closed captions?|transcription|transcribed|"
    r"translated|translation|subs|synced|sync|corrections?) (by|from)\b|amara org")
TS_LINE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def norm(text):
    t = text.lower().replace("’", "'").replace("‘", "'")
    t = t.replace("'", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def parse_srt(srt):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", srt.strip()):
        lines = block.splitlines()
        for i, line in enumerate(lines):
            m = TS_LINE.search(line)
            if m:
                g = m.groups()
                start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")) / 1000
                end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")) / 1000
                text = "\n".join(x.strip() for x in lines[i + 1:] if x.strip())
                cues.append([start, end, text])
                break
    return cues


def fmt_ts(sec):
    ms = int(round(max(sec, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_srt(cues):
    return "".join(f"{i}\n{fmt_ts(s)} --> {fmt_ts(e)}\n{t}\n\n" for i, (s, e, t) in enumerate(cues, 1))


def _period(seq):
    """Smallest p such that seq is seq[:p] repeated."""
    n = len(seq)
    for p in range(1, n + 1):
        if n % p == 0 and seq == seq[:p] * (n // p):
            return p
    return n


def collapse_phrase_loops(text, min_reps, max_n=12):
    """Collapse a multi-word phrase repeated >= min_reps times in a row to one copy.
    Single-word repeats ("Baa baa baa", "No, no, no") are left alone: they are
    usually real speech, and a one-word cue loop is handled by the cue rule."""
    words = text.split()
    keys = [norm(w) for w in words]
    out, i, changed = [], 0, False
    while i < len(words):
        best = None  # (covered, n, reps)
        for n in range(2, max_n + 1):
            phrase = keys[i:i + n]
            if len(phrase) < n or not all(phrase) or _period(phrase) != n:
                continue
            reps = 1
            while keys[i + reps * n:i + (reps + 1) * n] == phrase:
                reps += 1
            if reps >= min_reps and (best is None or reps * n > best[0]):
                best = (reps * n, n, reps)
        if best:
            covered, n, _ = best
            kept = words[i:i + n]
            # keep the punctuation that closed the run, e.g. "it, it, it." -> "it."
            tail = re.search(r"[^\w']*$", words[i + covered - 1]).group(0)
            kept[-1] = re.sub(r"[^\w']+$", "", kept[-1]) + tail
            out.extend(kept)
            i += covered
            changed = True
        else:
            out.append(words[i])
            i += 1
    return (" ".join(out), True) if changed else (text, False)


def is_filler(text):
    sentences = [norm(s) for s in re.split(r"[.!?…]+", text)]
    sentences = [s for s in sentences if s]
    return bool(sentences) and all(s in FILLER_SENTENCES or FILLER_PREFIX.search(s) for s in sentences)


def clean_srt(srt, duration=None, name="-", log=LOG):
    """Apply the clean-up rules; returns (srt, list of change records)."""
    cues = parse_srt(srt)
    changes = []

    def note(rule, cue, detail):
        changes.append((rule, cue[0], cue[1], detail))
        log.info("filter file=%s rule=%s at=%s-%s %s", name, rule, fmt_ts(cue[0]), fmt_ts(cue[1]), detail)

    # 2. phrase loop inside one cue
    for c in cues:
        new, changed = collapse_phrase_loops(c[2].replace("\n", " "), FILTER_REPEAT_PHRASE)
        if changed:
            note("phrase-loop", c, f"text={c[2]!r} -> {new!r}")
            c[2] = new

    # 1. N+ consecutive cues with the same text -> one cue. A cue also counts as
    #    the same line when one text contains the other ("P." / "P, but P."),
    #    if the shorter one has enough words to rule out short real replies.
    def same_line(a, b):
        if a == b:
            return bool(a)
        short, long_ = sorted((a, b), key=len)
        return len(short.split()) >= FILTER_LOOP_MIN_WORDS and f" {short} " in f" {long_} "

    merged, i = [], 0
    while i < len(cues):
        j, core, text = i + 1, norm(cues[i][2]), cues[i][2]
        while core and j < len(cues) and same_line(core, norm(cues[j][2])):
            if len(norm(cues[j][2])) < len(core):
                core, text = norm(cues[j][2]), cues[j][2]
            j += 1
        if j - i >= FILTER_REPEAT_CUES:
            c = [cues[i][0], cues[j - 1][1], text]
            note("repeated-cues", c, f"merged {j - i} cues text={text!r}")
            merged.append(c)
        else:
            merged.extend(cues[i:j])
        i = j
    cues = merged

    # 3. filler text: drop when long (sits over music / an end card) or final
    kept = []
    for idx, c in enumerate(cues):
        if is_filler(c[2]):
            long_ = c[1] - c[0] > FILTER_FILLER_MAX_SECONDS
            final = idx == len(cues) - 1
            if long_ or final:
                note("filler", c, f"{'long' if long_ else 'final'} removed={c[2]!r}")
                continue
        kept.append(c)
    cues = kept

    # 4. clip to the audio duration, drop empty cues
    kept = []
    for c in cues:
        if duration is not None and c[0] >= duration:
            note("past-end", c, f"removed={c[2]!r}")
            continue
        if duration is not None and c[1] > duration:
            note("clip-end", c, f"end -> {fmt_ts(duration)}")
            c[1] = duration
        if not norm(c[2]):
            note("empty", c, f"removed={c[2]!r}")
            continue
        kept.append(c)
    return render_srt(kept), changes


# --------------------------------------------------------------------------- translation (Lingarr)

def cue_line(text):
    """One cue's text as a single line for Lingarr: newlines and runs of spaces -> one space."""
    return " ".join(text.split())


def srt_to_lines(cues):
    """Lingarr `lines` payload: one entry per non-empty cue, position = cue index."""
    return [{"position": i, "line": cue_line(c[2])} for i, c in enumerate(cues) if cue_line(c[2])]


def apply_translation(cues, results):
    """Put translated lines back onto the cue timings by position.
    Returns (cues, missing): cues without a usable translation keep the source text."""
    by_pos = {}
    for item in results if isinstance(results, list) else []:
        if isinstance(item, dict) and isinstance(item.get("position"), int) and isinstance(item.get("line"), str):
            by_pos[item["position"]] = item["line"]
    out, missing = [], 0
    for i, (start, end, text) in enumerate(cues):
        if not cue_line(text):
            continue
        # no blank lines inside a cue (they would end the SRT block)
        new = "\n".join(x.strip() for x in by_pos.get(i, "").splitlines() if x.strip())
        if not new:
            missing += 1
            new = text
        out.append([start, end, new])
    return out, missing


def lingarr_translate(lines, source, target, title, timeout=None):
    """POST the lines to Lingarr; returns the parsed JSON list. Raises OSError / ValueError."""
    u = urllib.parse.urlsplit(LINGARR_URL)
    payload = json.dumps({
        "arrMediaId": 0,
        "title": title,
        "sourceLanguage": LINGARR_LANGUAGES.get(source, source),
        "targetLanguage": LINGARR_LANGUAGES.get(target, target),
        "mediaType": "Episode",
        "lines": lines,
    }).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if LINGARR_API_KEY:
        headers["X-Api-Key"] = LINGARR_API_KEY
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout or LINGARR_TIMEOUT)
    try:
        conn.request("POST", (u.path or "") + "/api/translate/content", body=payload, headers=headers)
        r = conn.getresponse()
        data = r.read()
    finally:
        conn.close()
    if r.status != 200:
        # 409: Lingarr is still working on an identical request (same title + languages)
        raise ValueError(f"HTTP {r.status}: {data[:300]!r}")
    results = json.loads(data)
    if not isinstance(results, list):
        raise ValueError(f"unexpected response: {data[:300]!r}")
    return results


# --------------------------------------------------------------------------- audio / http helpers

def wav_header(n_bytes):
    return (b"RIFF" + struct.pack("<I", 36 + n_bytes) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, BYTES_PER_SECOND, 2, 16) +
            b"data" + struct.pack("<I", n_bytes))


def wav_duration(data):
    """Duration of a PCM WAV (best effort), or None."""
    try:
        if bytes(data[:4]) != b"RIFF" or bytes(data[8:12]) != b"WAVE":
            return None
        pos, rate_bytes = 12, None
        while pos + 8 <= len(data):
            cid = bytes(data[pos:pos + 4])
            size = struct.unpack("<I", bytes(data[pos + 4:pos + 8]))[0]
            if cid == b"fmt ":
                rate_bytes = struct.unpack("<I", bytes(data[pos + 16:pos + 20]))[0]
            elif cid == b"data" and rate_bytes:
                return min(size, len(data) - pos - 8) / rate_bytes
            pos += 8 + size + (size & 1)
    except struct.error:
        pass
    return None


def multipart_part(body, content_type, field):
    """Zero-copy lookup of one multipart/form-data field; returns a memoryview or None."""
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return None
    delim = b"--" + m.group(1).encode()
    view = memoryview(body)
    pos = body.find(delim)
    while pos != -1:
        hdr_start = pos + len(delim) + 2
        hdr_end = body.find(b"\r\n\r\n", hdr_start)
        if hdr_end == -1:
            return None
        nxt = body.find(b"\r\n" + delim, hdr_end + 4)
        if nxt == -1:
            return None
        headers = bytes(view[hdr_start:hdr_end]).decode("utf-8", "replace")
        if re.search(r'name="?%s"?(;|\s|$)' % re.escape(field), headers, re.I):
            return view[hdr_end + 4:nxt]
        pos = nxt + 2
    return None


def post_multipart(base_url, path, fields, file_parts, timeout):
    """POST multipart/form-data. file_parts: list of chunks (bytes / memoryview)."""
    u = urllib.parse.urlsplit(base_url)
    boundary = "----whisper-adapter-%d" % time.monotonic_ns()
    pre = []
    for k, v in fields.items():
        pre.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    pre.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
               f'Content-Type: audio/wav\r\n\r\n')
    chunks = ["".join(pre).encode()] + list(file_parts) + [f"\r\n--{boundary}--\r\n".encode()]
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.putrequest("POST", (u.path or "") + path)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(sum(len(c) for c in chunks)))
        conn.endheaders()
        for c in chunks:
            conn.send(c)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def decode_fields():
    out = {}
    for kv in WHISPER_FIELDS.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def upstream_healthy():
    u = urllib.parse.urlsplit(WHISPER_URL)
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=2)
        conn.request("GET", (u.path or "") + "/health")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except OSError:
        return False


# --------------------------------------------------------------------------- server

class State:
    lock = threading.Lock()      # one whisper-server job at a time
    pending = 0                  # running + queued jobs (reported by /ready)
    pending_lock = threading.Lock()


@contextlib.contextmanager
def busy():
    """Count a running or queued job for the duration of the block. Nests."""
    with State.pending_lock:
        State.pending += 1
    try:
        yield
    finally:
        with State.pending_lock:
            State.pending -= 1


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "whisper-adapter/1"

    def log_message(self, fmt, *args):
        if self.path.startswith(("/ready", "/health")):
            return
        LOG.info("%s %s", self.address_string(), fmt % args)

    def reply(self, status, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def reply_json(self, status, obj):
        self.reply(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self.reply(200, b"ok")
        elif path == "/ready":
            # Busy pods stay ready: extra requests queue on State.lock instead of
            # failing. A pod with no endpoint left makes Bazarr throttle whisperai.
            if not upstream_healthy():
                self.reply(503, b"whisper-server not ready")
            else:
                self.reply(200, f"ready ({State.pending} jobs)".encode())
        else:
            self.reply(404, b"")

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY_BYTES:
            return None
        return self.rfile.read(n)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        if u.path == "/asr":
            self.handle_asr(q, u)
        elif u.path == "/detect-language":
            self.handle_detect(q)
        else:
            self.reply(404, b"")

    # -- helpers

    def audio(self, body, q):
        """Returns (wav_chunks, duration_s) for the audio_file field, or (None, None)."""
        part = multipart_part(body, self.headers.get("Content-Type"), "audio_file")
        if part is None:
            return None, None
        if q.get("encode", "true").lower() == "false":
            n = len(part) - (len(part) % 2)
            return [wav_header(n), part[:n]], n / BYTES_PER_SECOND
        return [part], wav_duration(part)  # must already be WAV (no ffmpeg conversion)

    def run_local(self, fields, chunks):
        """One whisper-server call; the pod counts as busy while it runs or waits."""
        with busy():
            with State.lock:
                return post_multipart(WHISPER_URL, "/inference", fields, chunks, WHISPER_TIMEOUT)

    def detect(self, chunks, raw):
        """Language of the first DETECT_SECONDS: (whisper name, ISO-639-1 code or None).
        Raises OSError / ValueError."""
        if raw:
            n = min(len(chunks[1]), int(DETECT_SECONDS * BYTES_PER_SECOND))
            chunks = [wav_header(n), chunks[1][:n]]
        fields = {"language": "auto", "detect_language": "true", "response_format": "json"}
        status, result = self.run_local(fields, chunks)
        lang = json.loads(result).get("language", "") if status == 200 else ""
        return lang, LANGUAGES.get(lang)

    def transcribe(self, chunks, duration, lang, output, name):
        """Transcribe on whisper-server, filter SRT output; returns the text or None on error."""
        fields = decode_fields()
        fields.update(language=lang, response_format=OUTPUT_FORMATS[output])
        dur = f"{duration:.0f}s" if duration else "?"
        LOG.info("asr start file=%s language=%s output=%s audio=%s", name, lang, output, dur)
        t0 = time.monotonic()
        try:
            status, result = self.run_local(fields, chunks)
        except OSError as e:
            LOG.error("asr file=%s: whisper-server error: %s", name, e)
            return None
        if status != 200:
            LOG.error("asr file=%s: whisper-server HTTP %d: %s", name, status, result[:300])
            return None
        text = result.decode("utf-8", "replace")
        n_changes = 0
        if output == "srt" and FILTER_ENABLED:
            text, changes = clean_srt(text, duration, name)
            n_changes = len(changes)
        elapsed = time.monotonic() - t0
        LOG.info("asr done file=%s elapsed=%.0fs audio=%s rtf=%s filter_changes=%d", name, elapsed, dur,
                 f"{elapsed / duration:.2f}" if duration else "?", n_changes)
        return text

    # -- endpoints

    def handle_asr(self, q, u):
        name = os.path.basename(q.get("video_file") or "") or "-"
        task = q.get("task", "transcribe")
        output = q.get("output", "txt")
        lang = q.get("language") or "auto"
        body = self.read_body()
        if body is None:
            LOG.warning("asr file=%s: missing or too large body", name)
            return self.reply(413, b"")
        if task == "translate" and TRANSLATE_MODE == "forward":
            return self.handle_forward(body, u, name, lang)
        if task == "translate" and TRANSLATE_MODE != "lingarr":
            LOG.info("asr file=%s: task=translate rejected (TRANSLATE_MODE=%s)", name, TRANSLATE_MODE)
            return self.reply(501, b"", headers={"X-Error": "translate not supported"})
        formats = ("srt",) if task == "translate" else OUTPUT_FORMATS
        if task not in ("transcribe", "translate") or output not in formats:
            LOG.warning("asr file=%s: unsupported task=%s output=%s", name, task, output)
            return self.reply(400, b"")
        chunks, duration = self.audio(body, q)
        if chunks is None:
            LOG.warning("asr file=%s: no audio_file field", name)
            return self.reply(400, b"")
        if task == "translate":
            status, srt = self.translate_lingarr(chunks, duration, q, name, lang)
            return self.reply(status, srt.encode("utf-8"), CONTENT_TYPES["srt"],
                              headers=None if status == 200 else {"X-Error": "translate failed"})
        text = self.transcribe(chunks, duration, lang, output, name)
        if text is None:
            return self.reply(502, b"")
        self.reply(200, text.encode("utf-8"), CONTENT_TYPES[output])

    def translate_lingarr(self, chunks, duration, q, name, lang):
        """Transcribe in the source language here, translate the cue texts with Lingarr.
        Returns (status, srt); srt is empty on errors."""
        t_job = time.monotonic()
        # busy for the whole job, Lingarr call included: the pod stays out of the
        # Service until the translated SRT is back
        with busy():
            if lang in ("", "auto"):
                try:
                    whisper_name, code = self.detect(chunks, q.get("encode", "true").lower() == "false")
                except (OSError, ValueError) as e:
                    LOG.error("translate file=%s: language detection failed: %s", name, e)
                    return 502, ""
                if not code:
                    LOG.error("translate file=%s: language detection returned %r", name, whisper_name)
                    return 502, ""
                LOG.info("translate file=%s: no language given, detected %s", name, code)
                lang = code
            t0 = time.monotonic()
            srt = self.transcribe(chunks, duration, lang, "srt", name)
            t_transcribe = time.monotonic() - t0
            if srt is None:
                return 502, ""
            cues = parse_srt(srt)
            lines = srt_to_lines(cues)
            if lang == TRANSLATE_TARGET or not lines:
                LOG.info("translate file=%s: source=%s cues=%d, nothing to translate", name, lang, len(lines))
                return 200, srt
            title = f"whisper-asr - {name}"
            LOG.info("translate file=%s: source=%s cues=%d transcribe=%.0fs, sending to lingarr",
                     name, lang, len(lines), t_transcribe)
            t1 = time.monotonic()
            try:
                results = lingarr_translate(lines, lang, TRANSLATE_TARGET, title)
            except (OSError, ValueError, http.client.HTTPException) as e:
                LOG.error("translate file=%s: lingarr failed after %.0fs (%s): %s", name,
                          time.monotonic() - t1, type(e).__name__, e)
                return 503, ""
            t_translate = time.monotonic() - t1
        out, missing = apply_translation(cues, results)
        if missing >= len(lines):
            LOG.error("translate file=%s: lingarr returned no usable lines (%d results)", name, len(results))
            return 503, ""
        if missing:
            LOG.warning("translate file=%s: %d of %d cues not translated, kept the source text",
                        name, missing, len(lines))
        LOG.info("translate done file=%s source=%s target=%s cues=%d transcribe=%.0fs translate=%.0fs "
                 "total=%.0fs missing=%d", name, lang, TRANSLATE_TARGET, len(lines), t_transcribe,
                 t_translate, time.monotonic() - t_job, missing)
        return 200, render_srt(out)

    def handle_forward(self, body, u, name, lang):
        if not TRANSLATE_UPSTREAM:
            LOG.info("asr file=%s: task=translate rejected (TRANSLATE_MODE=forward, no TRANSLATE_UPSTREAM)", name)
            return self.reply(501, b"", headers={"X-Error": "translate not supported"})
        t = urllib.parse.urlsplit(TRANSLATE_UPSTREAM)
        LOG.info("asr file=%s: task=translate language=%s forwarded to %s", name, lang, t.netloc)
        t0 = time.monotonic()
        try:
            conn = http.client.HTTPConnection(t.hostname, t.port or 80, timeout=TRANSLATE_TIMEOUT)
            conn.request("POST", (t.path or "") + u.path + ("?" + u.query if u.query else ""), body=body,
                         headers={"Content-Type": self.headers.get("Content-Type", "")})
            r = conn.getresponse()
            data, status, ctype = r.read(), r.status, r.getheader("Content-Type", "text/plain")
            conn.close()
        except OSError as e:
            LOG.warning("asr file=%s: translate upstream unavailable: %s", name, e)
            return self.reply(503, b"", headers={"X-Error": "translate upstream unavailable"})
        LOG.info("asr file=%s: translate upstream HTTP %d in %.0fs", name, status, time.monotonic() - t0)
        self.reply(status, data if status == 200 else b"", ctype)

    def handle_detect(self, q):
        name = os.path.basename(q.get("video_file") or "") or "-"
        empty = {"detected_language": None, "language_code": None}
        body = self.read_body()
        if body is None:
            return self.reply_json(413, empty)
        chunks, _ = self.audio(body, q)
        if chunks is None:
            return self.reply_json(400, empty)
        try:
            lang, code = self.detect(chunks, q.get("encode", "true").lower() == "false")
        except (OSError, ValueError) as e:
            LOG.error("detect file=%s: whisper-server error: %s", name, e)
            return self.reply_json(502, empty)
        LOG.info("detect file=%s: language=%r code=%s", name, lang, code)
        if not code:
            return self.reply_json(502, empty)
        self.reply_json(200, {"detected_language": lang, "language_code": code})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    if TRANSLATE_MODE == "lingarr":
        translate = f"lingarr {LINGARR_URL} -> {TRANSLATE_TARGET}"
    elif TRANSLATE_MODE == "forward" and TRANSLATE_UPSTREAM:
        translate = f"forward {TRANSLATE_UPSTREAM}"
    else:
        translate = "rejected"
    LOG.info("listening on :%d, whisper-server %s, fields %s, filter %s, translate %s", LISTEN_PORT,
             WHISPER_URL, decode_fields(), "on" if FILTER_ENABLED else "off", translate)
    Server(("", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
