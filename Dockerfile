# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
ARG RUST_IMAGE=rust:1.92.0-slim-bookworm@sha256:f1f73538ebe623fd3673a35aff3df358ae1084c64c55646516e5b17b321b6c9b
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
FROM ${RUST_IMAGE} AS rust

FROM ${PYTHON_IMAGE} AS build
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:$PATH
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/requirements-installer.lock /installer/requirements-installer.lock
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip download --only-binary=:all: --require-hashes \
      --dest /installer/wheels --requirement /installer/requirements-installer.lock \
    && python -m pip install --no-cache-dir --no-index --find-links /installer/wheels \
      --require-hashes --requirement /installer/requirements-installer.lock

WORKDIR /build
RUN --mount=type=secret,id=proxy_ca \
    --mount=type=bind,target=/source,readonly \
    --mount=type=cache,id=bessemer-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=bessemer-target-amd64,target=/build/target,sharing=locked \
    if [ -f /run/secrets/proxy_ca ]; then export CARGO_HTTP_CAINFO=/run/secrets/proxy_ca; fi; \
    cd /source \
    && CARGO_TARGET_DIR=/build/target maturin build --release --locked \
      --features extension-module --interpreter python --out /wheels

FROM ${PYTHON_IMAGE} AS production-dependencies
COPY deploy/requirements-production.lock /production/requirements-production.lock
RUN --mount=type=secret,id=proxy_ca \
    --mount=from=build,source=/installer/wheels,target=/installer,readonly \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir --no-index /installer/pip-*.whl \
    && python -m pip download --only-binary=:all: --require-hashes \
      --dest /production/wheels --requirement /production/requirements-production.lock

FROM ${PYTHON_IMAGE} AS runtime-base
ARG VERSION=0.2.0a2
ARG VCS_REF=unknown
ARG SOURCE_SHA256=unknown
LABEL org.opencontainers.image.title="Bessemer" \
    org.opencontainers.image.version=${VERSION} \
    org.opencontainers.image.revision=${VCS_REF} \
    org.bessemer.source.sha256=${SOURCE_SHA256}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BESSEMER_WORKERS=1 \
    BESSEMER_MAX_IN_FLIGHT=64 \
    BESSEMER_MAX_CONNECTIONS=256 \
    BESSEMER_REQUEST_TIMEOUT=10 \
    BESSEMER_GRACEFUL_TIMEOUT=20
RUN groupadd --gid 10001 bessemer \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home \
      --shell /usr/sbin/nologin bessemer
RUN --mount=from=build,source=/installer,target=/installer,readonly \
    --mount=from=build,source=/wheels,target=/wheels,readonly \
    python -m pip install --no-cache-dir --no-index /installer/wheels/pip-*.whl /wheels/*.whl \
    && python -m pip check

WORKDIR /app
COPY --chown=bessemer:bessemer examples ./examples
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "from urllib.request import build_opener, ProxyHandler; build_opener(ProxyHandler({})).open('http://127.0.0.1:8000/healthz', timeout=2).close()"]
ENTRYPOINT ["bessemer"]
CMD ["run", "examples.basic:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime-base AS production
USER 0:0
RUN --mount=from=production-dependencies,source=/production,target=/production,readonly \
    python -m pip install --no-cache-dir --no-index --find-links /production/wheels \
      --require-hashes --requirement /production/requirements-production.lock \
    && python -m pip check
USER 10001:10001
CMD ["run", "examples.production:app", "--host", "0.0.0.0", "--port", "8000"]

# Keep the default image minimal; opt in to Pydantic with --target production.
FROM runtime-base AS runtime
