FROM python:3.11-slim-bookworm AS build

WORKDIR /opt/CTFd

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

# Survive a slow or flaky connection.
#
# pip defaults to a 15-second read timeout, which is fine on a fast link and
# not fine on a venue or home connection running at a few hundred KB/s: a
# single large wheel (cryptography, pandas, Pillow) stalls past 15s, pip gives
# up, and the whole build fails after ten minutes of successful downloading.
# Observed exactly that -- ReadTimeoutError from files.pythonhosted.org at
# 639 seconds in.
#
# Raising the timeout costs nothing when the network is healthy; it only
# changes how long pip is willing to wait before calling a slow read dead.
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

COPY . /opt/CTFd

# --mount=type=cache keeps pip's download cache OUTSIDE the layer, so a build
# that fails partway does not throw away everything it already fetched. On a
# slow link that turns a failed build from "start again from zero" into
# "resume". The cache never lands in the published image, so this does the job
# --no-cache-dir was there for without paying its price on every retry.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
    && for d in CTFd/plugins/*; do \
        if [ -f "$d/requirements.txt" ]; then \
            pip install -r "$d/requirements.txt";\
        fi; \
    done;


FROM python:3.11-slim-bookworm AS release
WORKDIR /opt/CTFd

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libffi8 \
        libssl3 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=1001:1001 . /opt/CTFd

RUN useradd \
    --no-log-init \
    --shell /bin/bash \
    -u 1001 \
    ctfd \
    && mkdir -p /var/log/CTFd /var/uploads \
    && chown -R 1001:1001 /var/log/CTFd /var/uploads /opt/CTFd \
    && chmod +x /opt/CTFd/docker-entrypoint.sh

COPY --chown=1001:1001 --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

USER 1001
EXPOSE 8000
ENTRYPOINT ["/opt/CTFd/docker-entrypoint.sh"]
