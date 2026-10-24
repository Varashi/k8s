# whisper-asr

[openai-whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice)
(`faster_whisper`, `medium` model), used by Bazarr's `whisperai` provider to
transcribe (or translate to English) episodes without any usable subtitle source.

## Layout

| Controller | Nodes | Replicas | CPU limit | Notes |
|---|---|---|---|---|
| `whisper-asr` | non-GPU workers | 2 (one per node) | 2 | |
| `gpu` (`whisper-asr-gpu`) | GPU workers | 3 (one per node) | 1 | CPU only; `background-low` PriorityClass |

- `faster_whisper` uses CUDA only, so the Intel GPU workers contribute CPU, not
  GPU. The 1-core cap leaves headroom for transcodes and kubelet on 4-vCPU nodes.
- A single Service (`rawResources.service`) selects both controllers.
- Model cache is an `emptyDir`: re-downloadable, and no PVC pins pods to a node
  or blocks GPU-worker drains.
- Runs 24/7; idle pods are cheap once the library backlog is processed.

## Readiness probe (important)

The webservice handles one request at a time and does not answer while it is
transcribing. Without a readiness probe, the Service routes new requests to busy
pods; Bazarr treats the connection error as a provider failure and throttles
`whisperai` for 5 minutes, leaving the idle pods unused. The `/docs` readiness
probe removes busy pods from the endpoints. There is deliberately **no liveness
probe** (it would kill pods mid-transcription).

## Parallelism

Requests only spread across pods when several are in flight. Bazarr runs at most
`general.concurrent_jobs` jobs at once (set to 6), and its scheduled
missing-subtitle search is a single job that processes episodes serially. For
bulk work, trigger per-series searches (`PATCH /api/series` with
`action=search-missing`) from several threads.

Throughput with `medium`: ~20 minutes per 24-minute episode per pod.
