# renovate: datasource=docker depName=docker.io/netboxcommunity/netbox
ARG NETBOX_VERSION=v4.5.10-4.0.2
FROM docker.io/netboxcommunity/netbox:${NETBOX_VERSION}

USER root

# renovate: datasource=pypi depName=netbox-plugin-dns
ARG NETBOX_PLUGIN_DNS_VERSION=1.5.7

ENV VIRTUAL_ENV=/opt/netbox/venv
RUN /usr/local/bin/uv pip install --no-cache \
    "netbox-plugin-dns==${NETBOX_PLUGIN_DNS_VERSION}"

USER unit:root
