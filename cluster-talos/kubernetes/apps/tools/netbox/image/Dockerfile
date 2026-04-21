# renovate: datasource=docker depName=docker.io/netboxcommunity/netbox
ARG NETBOX_VERSION=v4.5.8-4.0.2
FROM docker.io/netboxcommunity/netbox:${NETBOX_VERSION}

USER root

# renovate: datasource=pypi depName=netbox-plugin-dns
ARG NETBOX_PLUGIN_DNS_VERSION=1.5.7

RUN pip install --no-cache-dir \
    "netbox-plugin-dns==${NETBOX_PLUGIN_DNS_VERSION}"

USER unit:root
