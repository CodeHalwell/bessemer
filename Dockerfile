# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
ARG RUST_IMAGE=rust:1.92.0-slim-bookworm@sha256:f1f73538ebe623fd3673a35aff3df358ae1084c64c55646516e5b17b321b6c9b
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
ARG BUILD_PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
FROM ${RUST_IMAGE} AS rust

FROM ${BUILD_PYTHON_IMAGE} AS build
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
ARG VERSION=1.0.0rc2
ARG VCS_REF=unknown
ARG SOURCE_SHA256=unknown
ARG TINI_VERSION=0.19.0-3+b8
ARG TINI_SHA256=a443b12107331f67836a64cb6efe1dd92d2765e15568eb4567dccb409a8cd2a0
LABEL org.opencontainers.image.title="Bessemer" \
    org.opencontainers.image.version=${VERSION} \
    org.opencontainers.image.revision=${VCS_REF} \
    org.bessemer.source.sha256=${SOURCE_SHA256}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BESSEMER_WORKERS=1 \
    BESSEMER_MAX_IN_FLIGHT=auto \
    BESSEMER_MAX_CONNECTIONS=256 \
    BESSEMER_REQUEST_TIMEOUT=10 \
    BESSEMER_GRACEFUL_TIMEOUT=20
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then set -- -o Acquire::https::CaInfo=/run/secrets/proxy_ca; else set --; fi; \
    apt-get "$@" update \
    && mkdir /tmp/tini-package \
    && cd /tmp/tini-package \
    && apt-get "$@" download "tini=${TINI_VERSION}" \
    && python -c "import hashlib,os; from pathlib import Path; files=list(Path('.').glob('*.deb')); valid=len(files)==1 and hashlib.sha256(files[0].read_bytes()).hexdigest()==os.environ['TINI_SHA256']; raise SystemExit(0 if valid else 'Tini archive checksum mismatch')" \
    && apt-get "$@" install --yes --no-install-recommends ./*.deb \
    && mkdir -p /usr/share/doc/tini/bessemer-provenance \
    && apt-cache show "tini=${TINI_VERSION}" > /usr/share/doc/tini/bessemer-provenance/package-metadata.txt \
    && apt-cache policy tini > /usr/share/doc/tini/bessemer-provenance/package-policy.txt \
    && cp /var/lib/apt/lists/*_InRelease /usr/share/doc/tini/bessemer-provenance/ \
    && python -c "import hashlib,json,os; from pathlib import Path; p=Path('/usr/bin/tini'); Path('/usr/share/doc/tini/bessemer-provenance/receipt.json').write_text(json.dumps({'package':'tini','version':os.environ['TINI_VERSION'],'architecture':'amd64','archive_sha256':os.environ['TINI_SHA256'],'binary':'/usr/bin/tini','binary_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'repository':'http://deb.debian.org/debian trixie/main','verification':'signed apt index plus explicit archive SHA256'},indent=2)+'\\n')" \
    && rm -rf /tmp/tini-package /var/lib/apt/lists/*
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
ENTRYPOINT ["/usr/bin/tini", "-s", "--", "bessemer"]
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
