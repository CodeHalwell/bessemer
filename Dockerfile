# syntax=docker/dockerfile:1
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
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir maturin==1.9.6

WORKDIR /build
COPY Cargo.toml Cargo.lock build.rs pyproject.toml README.md ./
COPY src ./src
COPY python ./python
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export CARGO_HTTP_CAINFO=/run/secrets/proxy_ca; fi; \
    maturin build --release --locked --features extension-module --interpreter python --out /wheels

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --system bessemer \
    && useradd --system --gid bessemer --home-dir /app --no-create-home bessemer
COPY --from=build /wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --no-index /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

WORKDIR /app
COPY --chown=bessemer:bessemer examples ./examples
USER bessemer:bessemer
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=2).close()"
ENTRYPOINT ["bessemer"]
CMD ["run", "examples.basic:app", "--host", "0.0.0.0", "--port", "8000"]
