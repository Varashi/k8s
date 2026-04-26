# renovate: datasource=docker depName=docker.io/library/debian
ARG DEBIAN_VERSION=trixie-slim
FROM docker.io/library/debian:${DEBIAN_VERSION}

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8

RUN sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      tini ca-certificates curl jq rsync openssh-client sshpass \
      ffmpeg mkvtoolnix mediainfo \
      vainfo intel-media-va-driver-non-free libigfxcmrt7 \
      python3 python3-pip python3-venv \
      bash less procps file tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      "faster-whisper==1.2.0" \
      "pysubs2==1.8.0" \
      "requests==2.32.5" \
 && useradd -u 1000 -m -s /bin/bash toolkit \
 && mkdir -p /scratch && chown toolkit:toolkit /scratch

USER 1000:1000
WORKDIR /home/toolkit
ENV TZ=Europe/Brussels
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
