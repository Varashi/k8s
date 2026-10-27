# whisper-asr

Speech-to-text for Bazarr's `whisperai` provider: it transcribes episodes that
have no usable subtitle source, or translates them to English. Bazarr's endpoint is the
`whisper-asr:9000` Service, which only selects the GPU pool.

## Layout

| Controller | Nodes | Replicas | Engine | Translate |
|---|---|---|---|---|
| `whisper-asr` | non-GPU workers | 0 (2 when used, one per node) | [openai-whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice), `faster_whisper` `medium` on CPU (2 cores) | whisper itself |
| `gpu` (`whisper-asr-gpu`) | GPU workers | 3 (one per node) | [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-server`, `large-v3-turbo` q8_0 on the Intel GPU (Vulkan), plus an adapter | transcribes, then [Lingarr](https://github.com/lingarr-translate/lingarr) translates |

- `whisper-asr` Service (`rawResources.service`): selects only the GPU pool. Bazarr's endpoint.
- `whisper-asr-cpu` Service (`rawResources.service-cpu`): selects only the CPU pool. The GPU adapter forwards translate jobs here when it runs with `TRANSLATE_MODE=forward`. The CPU pool is scaled to 0; set its `replicas` back to 2 before switching to `forward`.
- Models are cached in `emptyDir`s. They can be downloaded again, and no PVC pins pods to a node or blocks GPU-worker drains.
- The GPU pods use the `background-low` PriorityClass, so they are the first to go under node pressure.
- On the GPU pods, a 24-minute episode takes about 6-7 minutes and uses about 0.3 CPU core and about 1.1 GiB of VRAM. The CPU pool needs about 20 minutes for the same episode.

### GPU pod

| Container | Image | Role |
|---|---|---|
| `fetch-model` (init) | whisper.cpp `main-vulkan` | Downloads the model from the official `ggerganov/whisper.cpp` Hugging Face repo, pinned to a commit, and verifies its SHA-256. Skips the download if the file is already there and valid. |
| `app` | whisper.cpp `main-vulkan` | `whisper-server` on `127.0.0.1:8080`, so it is not reachable from outside the pod. Uses one `gpu.intel.com/i915` device. |
| `adapter` | official `python` slim | `adapter/whisper_adapter.py` (stdlib only), mounted from the `whisper-asr-adapter` ConfigMap. Serves the Bazarr API on `:9000`. |

The adapter is a ConfigMap script on a stock image, so no image build or CI is
needed. Reloader rolls the GPU pods when the script changes.

## Adapter contract

The adapter implements the parts of the webservice API that Bazarr calls:

- `POST /asr?task=transcribe&language=<iso1>&output=srt&encode=false&video_file=<path>`
  - Multipart field `audio_file` holds raw s16le 16 kHz mono PCM. That is what Bazarr sends with `encode=false`.
  - The adapter adds a WAV header and calls `/inference` with the requested language and the decoding fields.
  - It runs the clean-up filter and returns SRT.
  - With `encode=true` the file is passed through as-is, so it must already be a WAV (the server does no ffmpeg conversion).
  - `output` can also be `vtt`, `txt` or `json`. The filter only runs on `srt`.
- `POST /detect-language`: same input.
  - Only the first 30 s (`DETECT_SECONDS`) is sent, with `detect_language=true`.
  - The adapter maps whisper.cpp's full language name to an ISO-639-1 code and returns `{"detected_language": "japanese", "language_code": "ja"}`.
- `GET /ready`: returns 200 when `whisper-server` has loaded the model and answers, busy or not, and 503 otherwise. The body reports the number of running and queued jobs.
- `GET /health`: returns 200 while the adapter process is running.

When `/asr` fails, the adapter returns an error status with an **empty body**.
Bazarr stores the response body as the subtitle without checking the status.
It rejects an empty body as "Invalid subtitle" for that one download, and it
does not throttle the provider.

### Translate

`large-v3-turbo` was trained without translation data, so it cannot translate.
Most of Bazarr's whisperai jobs are translations to English (for example,
Japanese-audio anime). The adapter handles `task=translate` according to
`TRANSLATE_MODE`:

- **`lingarr`** (default): whisper only transcribes, Lingarr translates.
  1. The adapter transcribes the audio on the GPU in the source language (the `language` query parameter; if it is missing, it detects the language first). This is the same path as `task=transcribe`, including the clean-up filter.
  2. It sends the cue texts to Lingarr's `POST /api/translate/content` (`LINGARR_URL`, in-cluster `lingarr.lingarr.svc:9876`), source language to `TRANSLATE_TARGET` (`en`). Each cue is one line (newlines inside a cue become spaces), with the cue index as its position. The title is `whisper-asr - <video file name>`. The request carries the video's Sonarr episode id or Radarr movie id: the adapter parses the file name with Sonarr's, then Radarr's, `GET /api/v3/parse` and keeps the match only when the file is inside that series or movie folder. No match sends id `0`, which Lingarr logs as a Sonarr 404 but otherwise ignores. Lingarr uses its own configured translation service and batch size.
  3. It puts the translated lines back onto the original cue timings by position and returns the SRT. A cue that Lingarr returns no text for keeps its source text, and the adapter logs a warning with the count.
  4. The job counts as running for the whole job, including the Lingarr call; the whisper lock is only held while whisper transcribes.
  5. If Lingarr fails, times out (`LINGARR_TIMEOUT`, default 1920 s, just above Lingarr's own 30-minute job timeout) or returns nothing usable, the adapter returns `503` with an empty body. Bazarr rejects that download as an invalid subtitle and retries on a later search, without throttling the provider.
  6. If the source language already is `TRANSLATE_TARGET`, the transcript is returned as-is.
- **`forward`**: the adapter relays the whole request to `TRANSLATE_UPSTREAM` (the `whisper-asr-cpu` Service, where faster-whisper `medium` translates) and returns the response. The GPU pod stays Ready while it waits. If the CPU pool has no ready pod, it returns `503` with an empty body.
- **`reject`** (or `forward` without `TRANSLATE_UPSTREAM`): `501` with an empty body.

Per job, the adapter logs the file, source language, cue count, transcribe
seconds, translate seconds and the number of untranslated cues.

Lingarr keeps a translation request per title and language pair. It answers
`409` while an identical request is still running (the adapter then returns
`503`), and it reuses a completed request when the same lines come in again.

| Env var | Default | Used by |
|---|---|---|
| `TRANSLATE_MODE` | `lingarr` | all |
| `TRANSLATE_TARGET` | `en` | `lingarr` |
| `LINGARR_URL` | `http://lingarr.lingarr.svc:9876` | `lingarr` |
| `LINGARR_TIMEOUT` | `1920` | `lingarr` |
| `LINGARR_API_KEY` | empty (sent as `X-Api-Key` when set) | `lingarr` |
| `SONARR_URL` / `RADARR_URL` | `http://sonarr.sonarr.svc:8989` / `http://radarr.radarr.svc:7878` | `lingarr` |
| `SONARR_API_KEY` / `RADARR_API_KEY` | empty (lookup skipped); HelmRelease: from the `whisper-asr-arr` ExternalSecret | `lingarr` |
| `TRANSLATE_UPSTREAM` | empty | `forward` |
| `TRANSLATE_TIMEOUT` | `7200` (HelmRelease: `10800`) | `forward` |

The CPU pool is not part of Bazarr's Service, so every translation goes through
the GPU adapter and Lingarr. It is kept (at 0 replicas) only as the `forward`
fallback.

### Decoding settings

- `-bs 5 -bo 5` gives beam search with best-of 5. Greedy decoding produced runs of empty cues.
- `-mc 0` (max context 0) stops whisper from conditioning on previous text. This is what stops the repetition loops.
- There is no VAD. Silero VAD lost dialogue, which raised the WER.

`whisper-server` copies its command-line defaults for every request, and request
fields override them. The adapter sends `WHISPER_FIELDS` (default
`beam_size=5,best_of=5,max_context=0`) with every `/inference` call, so those
fields win. Keep the server args and `WHISPER_FIELDS` in sync. The adapter also
sends the requested `language` on every call, so the server's `-l en` is only a
fallback.

## Clean-up filter

The filter runs inside the adapter on SRT output. Set `FILTER_ENABLED=false` to
turn it off. Each change is logged at INFO with the file name, rule, cue time and
removed text. The rules run in this order:

1. **Phrase loop within a cue**
   - What it does: a phrase of 2 or more words that repeats 4 or more times in a row inside one cue (`FILTER_REPEAT_PHRASE`) is collapsed to a single copy.
   - What it keeps: repeats of a single word ("Baa baa baa", "No, no, no").
2. **Repeated cues**
   - What it does: 3 or more consecutive cues (`FILTER_REPEAT_CUES`) with the same normalised text are merged into one cue, from the first start to the last end.
   - It also counts a cue whose text contains the other's ("P." / "P, but P.") as the same text.
   - What it keeps: short real replies such as "Yes." / "Yes, sir.". The containment match needs at least 4 words in the shorter text (`FILTER_LOOP_MIN_WORDS`).
3. **Filler**
   - What it does: drops a cue that consists only of known filler ("Thank you.", "Thanks for watching", "The End", "We'll be right back", "Subtitles by...", and similar). It only drops it if the cue lasts longer than 5 s (`FILTER_FILLER_MAX_SECONDS`) or is the last cue.
   - What it keeps: a short "Thank you." in the middle of an episode, which is real dialogue.
4. **End clipping**
   - What it does: clips cue ends to the audio duration, drops cues that start after it, drops empty cues, and renumbers the result.

## Readiness probe (important)

Each GPU pod runs one whisper job at a time. Extra requests that reach a busy
pod wait in the adapter's queue; Bazarr waits up to its `whisperai.timeout`
(3 h) for the answer.

Busy pods deliberately **stay ready**. With only 3 pods and up to 10 concurrent
Bazarr jobs, taking busy pods out of the Service left it empty most of the time.
Bazarr treats the resulting connection error as a provider failure and throttles
`whisperai` for 5 minutes, so searches skipped whisper altogether.

- GPU pool: the adapter's `/ready` returns 503 only when `whisper-server` is not
  up (model still loading, or crashed). `whisper-server`'s own `/health`
  answers while it transcribes.
- CPU pool (0 replicas, `forward` fallback only): `/docs`, which stops answering
  while the pod transcribes.

Requests are spread over the ready pods by the Service, not by load, so a
request can wait behind a running job while another pod is idle. The adapter
answers every `POST` with `Connection: close`, so a client that keeps
connections alive doesn't send all its jobs to the same pod. There is
deliberately **no liveness probe**, because it would kill pods mid-transcription.

## Changing the model or flags

- **Model:** change `MODEL_FILE`, `MODEL_URL` and `MODEL_SHA256` in the `fetch-model` init container, and `--model` in the `app` args.
  - Use a model from the official repo (the SHA-256 is the LFS oid in the Hugging Face file listing).
  - Raise the `models` emptyDir `sizeLimit` if the new model is larger.
  - A model that can translate (for example, `medium` or `large-v3`) could translate on the GPU itself; the adapter would need a mode that passes `translate` to `whisper-server`.
- **Decoding flags:** change both the `app` args and `WHISPER_FIELDS`. Any `whisper-server` multipart field works in `WHISPER_FIELDS`, for example `temperature_inc` or `no_speech_thold`.
- **Filter:** change the `FILTER_*` env vars, or edit `adapter/whisper_adapter.py`. The kustomize `configMapGenerator` builds the ConfigMap from that file.

## Parallelism

Requests only spread across pods when several are in flight. Bazarr runs at most
`general.concurrent_jobs` jobs at once, and its scheduled missing-subtitle search
is a single job that processes episodes one after another. For bulk work,
trigger per-series searches (`PATCH /api/series` with `action=search-missing`)
from several threads.
