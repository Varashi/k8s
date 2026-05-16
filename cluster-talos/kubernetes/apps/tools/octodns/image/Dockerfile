# renovate: datasource=docker depName=docker.io/library/python
ARG PYTHON_VERSION=3.14-slim
FROM docker.io/library/python:${PYTHON_VERSION}

# renovate: datasource=pypi depName=octodns
ARG OCTODNS_VERSION=1.17.0
# renovate: datasource=pypi depName=octodns-bind
ARG OCTODNS_BIND_VERSION=1.1.0
# renovate: datasource=pypi depName=octodns-netbox-dns
ARG OCTODNS_NETBOX_DNS_VERSION=0.3.16

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir \
      "octodns==${OCTODNS_VERSION}" \
      "octodns-bind==${OCTODNS_BIND_VERSION}" \
      "octodns-netbox-dns==${OCTODNS_NETBOX_DNS_VERSION}" \
 && useradd -u 1000 -m -s /bin/bash octodns

COPY octodns-sync-filtered /usr/local/bin/octodns-sync-filtered
RUN chmod +x /usr/local/bin/octodns-sync-filtered

USER 1000:1000
WORKDIR /home/octodns
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["octodns-sync-filtered", "--config-file", "/etc/octodns/config.yaml"]
