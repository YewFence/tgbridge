# syntax=docker/dockerfile:1

# Tools come from mise.lock, Python dependencies from uv.lock; the image adds no version
# source of its own.  mise and uv stay in the build stages -- the runtime only needs the
# interpreter, the installed wheel, and the rclone binary.

# ---- tools: install the locked python/uv/rclone into mise's data dir ----
FROM ghcr.io/jdx/mise:2026.10.4-debian AS tools
# Pin the data dir explicitly instead of relying on the base image's internal layout.
ENV MISE_DATA_DIR=/mise-data
WORKDIR /src
COPY mise.toml mise.lock ./
# Only the tools the image needs; ruff/node/hk and friends stay out.
RUN mise trust && mise install --locked python uv rclone
# Flatten the tool paths so later stages copy fixed locations, not versioned ones.
RUN install -m 0755 "$(mise which rclone)" /usr/local/bin/rclone \
    && install -m 0755 "$(mise which uv)" /usr/local/bin/uv \
    && cp -a "$(mise where python)" /opt/python

# ---- deps: build the wheel and install it into a venv at its final path ----
FROM tools AS deps
# tgcrypto publishes no wheel for this interpreter, so it compiles from sdist here.
# Build tooling stays in this stage and never reaches the runtime image.
RUN apt-get update \
    && apt-get -y --no-install-recommends install build-essential \
    && rm -rf /var/lib/apt/lists/*
# The venv is created at the final path, so nothing is anchored to a build-time prefix.
RUN /opt/python/bin/python3 -m venv /opt/venv
WORKDIR /src
# Metadata first: lockfile changes re-resolve dependencies, source changes do not.
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
# The wheel is the same artifact a PyPI release would ship; install that, not the tree.
RUN uv build --out-dir /dist \
    && uv export --locked --no-dev --no-emit-project --format requirements.txt -o /dist/requirements.txt \
    && uv pip install --python /opt/venv/bin/python --require-hashes -r /dist/requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-deps /dist/*.whl

# ---- runtime: no mise, no uv ----
FROM debian:13-slim
RUN apt-get update \
    && apt-get -y --no-install-recommends install ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tools /opt/python /opt/python
COPY --from=deps /opt/venv /opt/venv
COPY --from=tools /usr/local/bin/rclone /usr/local/bin/rclone

# rclone reads its config from here.  Compose mounts a named volume that an init
# container fills, so the bot never writes into a host directory.
ENV RCLONE_CONFIG=/config/rclone/rclone.conf
ENV TMP_DIR=/tmp/tgbridge

# Pre-create the volume mountpoints owned by the runtime user, so a fresh named volume
# inherits that ownership instead of being created root-owned.
RUN mkdir -p /config/rclone /tmp/tgbridge /var/log/tgbridge \
    && chown 65534:65534 /config/rclone /tmp/tgbridge /var/log/tgbridge

USER nobody
ENTRYPOINT ["/opt/venv/bin/python", "-m", "tgbridge.entry"]
