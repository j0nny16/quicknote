# Headless anytype-heart via the official CLI (single static Go binary).
FROM debian:13-slim

ARG ANYTYPE_CLI_VERSION=v0.3.6
ARG TARGETARCH=amd64

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl iptables \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/anytype-cli.tar.gz \
      "https://github.com/anyproto/anytype-cli/releases/download/${ANYTYPE_CLI_VERSION}/anytype-cli-${ANYTYPE_CLI_VERSION}-linux-${TARGETARCH}.tar.gz" \
 && tar xzf /tmp/anytype-cli.tar.gz -C /usr/local/bin anytype \
 && chmod +x /usr/local/bin/anytype \
 && rm /tmp/anytype-cli.tar.gz \
 && anytype --version

# Account key, local object DB and logs all live under $HOME -> keep it on a volume.
ENV HOME=/data
WORKDIR /data

COPY docker/anytype-entrypoint.sh /usr/local/bin/anytype-entrypoint.sh
RUN chmod +x /usr/local/bin/anytype-entrypoint.sh

# 0.0.0.0 is safe here: no port is published to the host, so this listener is
# only reachable from inside the compose network.
CMD ["/usr/local/bin/anytype-entrypoint.sh"]
