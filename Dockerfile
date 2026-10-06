# GlueOps toolbox - the platform CLIs, preconfigured to authenticate through the
# oauth2-proxy edge. Developers run this instead of installing anything locally.
FROM debian:12-slim

ARG ARGOCD_VERSION=v3.3.12
ARG OPENBAO_VERSION=2.4.4
ARG PROMETHEUS_VERSION=3.14.0
ARG LOKI_VERSION=3.7.7
ARG TEMPO_VERSION=3.0.3
# Match the helm that ArgoCD's repo-server renders with (argocd version reports
# it), so a local render agrees with what the cluster will get.
ARG HELM_VERSION=3.19.4
ARG DYFF_VERSION=1.12.0

# Supplied automatically by BuildKit for the platform being built. Deliberately
# left without a default: a default would silently produce an arm64 image full of
# amd64 binaries when someone builds natively on an Apple Silicon Mac.
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl python3 python3-yaml jq less git bash \
 && rm -rf /var/lib/apt/lists/*

# argocd is installed off PATH as argocd.real; bin/argocd wraps it to attach the
# edge token and to refuse anything that is not read-only.
RUN set -eux; \
    : "${TARGETARCH:?BuildKit must supply TARGETARCH - build with docker buildx}"; \
    mkdir -p /usr/local/libexec/toolbox; \
    curl -fsSL -o /usr/local/libexec/toolbox/argocd.real \
      "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-linux-${TARGETARCH}"; \
    chmod +x /usr/local/libexec/toolbox/argocd.real; \
    /usr/local/libexec/toolbox/argocd.real version --client >/dev/null

# OpenBao names its tarballs by uname -m (x86_64), not by Docker's TARGETARCH (amd64).
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) BAO_ARCH=x86_64 ;; \
      arm64) BAO_ARCH=arm64 ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/bao.tar.gz \
      "https://github.com/openbao/openbao/releases/download/v${OPENBAO_VERSION}/bao_${OPENBAO_VERSION}_Linux_${BAO_ARCH}.tar.gz"; \
    tar -xzf /tmp/bao.tar.gz -C /usr/local/bin bao; \
    chmod +x /usr/local/bin/bao; \
    rm -f /tmp/bao.tar.gz; \
    /usr/local/bin/bao version >/dev/null

# promtool (metrics, alert state and rule listings) - queries Thanos through Grafana's
# datasource proxy. Only promtool is kept; the tarball also ships the server binaries.
RUN set -eux; \
    curl -fsSL -o /tmp/prom.tar.gz \
      "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION}.linux-${TARGETARCH}.tar.gz"; \
    tar -xzf /tmp/prom.tar.gz --strip-components=1 -C /usr/local/bin --wildcards '*/promtool'; \
    mv /usr/local/bin/promtool /usr/local/libexec/toolbox/promtool.real; \
    chmod +x /usr/local/libexec/toolbox/promtool.real; \
    rm -f /tmp/prom.tar.gz; \
    /usr/local/libexec/toolbox/promtool.real --version >/dev/null

# logcli (logs). Ships as a zip of a single arch-suffixed binary.
RUN set -eux; \
    curl -fsSL -o /tmp/logcli.zip \
      "https://github.com/grafana/loki/releases/download/v${LOKI_VERSION}/logcli-linux-${TARGETARCH}.zip"; \
    (cd /tmp && jar xf logcli.zip 2>/dev/null || python3 -c "import zipfile;zipfile.ZipFile('/tmp/logcli.zip').extractall('/tmp')"); \
    mv "/tmp/logcli-linux-${TARGETARCH}" /usr/local/libexec/toolbox/logcli.real; \
    chmod +x /usr/local/libexec/toolbox/logcli.real; \
    rm -f /tmp/logcli.zip; \
    /usr/local/libexec/toolbox/logcli.real --version >/dev/null

# tempo-cli (traces). `query api` is a TraceQL client; the rest of the binary is
# backend tooling we do not use.
RUN set -eux; \
    curl -fsSL -o /tmp/tempo.tar.gz \
      "https://github.com/grafana/tempo/releases/download/v${TEMPO_VERSION}/tempo_${TEMPO_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    tar -xzf /tmp/tempo.tar.gz -C /usr/local/bin tempo-cli; \
    mv /usr/local/bin/tempo-cli /usr/local/libexec/toolbox/tempo-cli.real; \
    chmod +x /usr/local/libexec/toolbox/tempo-cli.real; \
    rm -f /tmp/tempo.tar.gz

# helm (rendering deployment configs locally). Verified against the published
# checksum, which sits next to the tarball.
RUN set -eux; \
    f="helm-v${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    curl -fsSL -o "/tmp/$f" "https://get.helm.sh/$f"; \
    curl -fsSL -o "/tmp/$f.sha256sum" "https://get.helm.sh/$f.sha256sum"; \
    (cd /tmp && sha256sum -c "$f.sha256sum"); \
    tar -xzf "/tmp/$f" --strip-components=1 -C /usr/local/bin "linux-${TARGETARCH}/helm"; \
    chmod +x /usr/local/bin/helm; \
    rm -f "/tmp/$f" "/tmp/$f.sha256sum"; \
    /usr/local/bin/helm version --short >/dev/null

# dyff (Kubernetes-aware YAML diffs of rendered manifests). Verified against the
# release's checksums.txt.
RUN set -eux; \
    f="dyff_${DYFF_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    curl -fsSL -o "/tmp/$f" "https://github.com/homeport/dyff/releases/download/v${DYFF_VERSION}/$f"; \
    curl -fsSL "https://github.com/homeport/dyff/releases/download/v${DYFF_VERSION}/checksums.txt" \
      | grep " $f\$" > /tmp/dyff.sha256; \
    (cd /tmp && sha256sum -c dyff.sha256); \
    tar -xzf "/tmp/$f" -C /usr/local/bin dyff; \
    chmod +x /usr/local/bin/dyff; \
    rm -f "/tmp/$f" /tmp/dyff.sha256; \
    /usr/local/bin/dyff version >/dev/null

COPY lib/ /opt/toolbox/lib/
COPY bin/ /usr/local/bin/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY bin/toolbox-env /etc/toolbox-env.sh
# `docker exec` bypasses the ENTRYPOINT, so wire the same environment into shells
# started that way - interactive ones read .bashrc, login ones read profile.d.
RUN printf '. /etc/toolbox-env.sh\n' > /etc/profile.d/toolbox.sh
RUN chmod +x /usr/local/bin/toolbox-token /usr/local/bin/toolbox-proxy \
             /usr/local/bin/toolbox-login /usr/local/bin/argocd \
             /usr/local/bin/promtool /usr/local/bin/logcli \
             /usr/local/bin/tempo-cli /usr/local/bin/grafana-ds \
             /usr/local/bin/toolbox-app /usr/local/bin/toolbox-preflight \
             /usr/local/bin/toolbox-watch /usr/local/bin/toolbox-propose \
             /usr/local/bin/entrypoint.sh

# Unprivileged, with the token-cache directory created up front and owned by the
# runtime user - otherwise a mounted volume lands root-owned and the cache write
# fails. Deliberately no VOLUME directive: it would create a fresh anonymous
# volume on every `docker run`, so the cache would never survive a restart and
# developers would re-authenticate every time. Persistence is opt-in, by mounting
# a named volume over this path (see HUMANS.md).
RUN useradd -m -u 1000 -s /bin/bash toolbox \
 && mkdir -p /home/toolbox/.config/glueops \
 && printf '. /etc/toolbox-env.sh\n' >> /home/toolbox/.bashrc \
 && chown -R toolbox:toolbox /home/toolbox
USER toolbox
WORKDIR /home/toolbox

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
