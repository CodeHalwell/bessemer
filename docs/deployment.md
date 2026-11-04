# Container deployment

The images package Bessemer's current alpha runtime and examples. Their tests
qualify container behavior; they do not close the formal performance, 24-hour
soak or supported-platform gates in [production readiness](production-readiness.md).
The recipes below target CPython 3.12 on Linux x86_64. Nothing is deployed by
building an image or preparing these manifests.

## Build an inspectable artifact

The default Docker target, `runtime`, installs the framework and the basic
example without Pydantic. The optional `production` target adds the hash-pinned
Pydantic closure in [requirements-production.lock](../deploy/requirements-production.lock)
and defaults to [examples/production.py](../examples/production.py). That example
is a read-only demonstration; replace its catalog and host allowlist with your
application's configuration before using it as a service.

```sh
python3 deploy/build_image.py --target runtime --image bessemer:runtime-0.2.0a3 \
  --output work/container-runtime
python3 deploy/build_image.py --target production --image bessemer:production-0.2.0a3 \
  --output work/container-production
```

Use a new output directory for each build. The builder freezes the exact Docker
build inputs before building, hashes every file, labels the image with their
combined SHA-256 and the base Git revision, and records the immutable local
image ID. It also records whether the current working source still matches the
snapshot. `manifest.json`, `build.log`, `python-inventory.json` and
`os-packages.tsv` retain provenance and package inventory. A dirty Git tree is
identified explicitly; a Git revision alone is not proof of the built source.

Rust and Python base images are pinned by digest. Installer tools and the
optional Python closure are pinned by version and wheel SHA-256. Locks currently
contain the qualified x86_64 wheels; refresh and qualify them for another
architecture. Cargo uses `--locked`. Debian build packages still resolve from
their configured repositories, so this is not a bit-for-bit reproducible build.
Keep the inventories and vulnerability reports for the artifact you deploy.

The builder explicitly selects the local Docker Unix socket and preserves
registry credentials and proxy settings. In the managed cloud environment it
passes `CODEX_PROXY_CERT` as the BuildKit `proxy_ca` secret. The certificate is
mounted only in networked build steps and is not copied into an image layer.
Outside that environment, normal Docker builds can omit the secret:

```sh
docker build --platform linux/amd64 --target runtime -t bessemer:runtime-0.2.0a3 .
docker build --platform linux/amd64 --target production -t bessemer:production-0.2.0a3 .
```

## Run with resource and shutdown bounds

Images run as UID/GID `10001:10001`, use an exec-form Bessemer entrypoint, and
accept an explicit `run module:app` command. No application name is evaluated by
a shell. The default settings are one supervised worker, 64 in-flight requests,
256 connections, a 10-second handler deadline and a 20-second graceful deadline.
`BESSEMER_*` environment settings can override those defaults, and explicit CLI
flags take precedence over environment settings.

```sh
docker run --rm --name bessemer \
  --cpus 1 --memory 512m --pids-limit 128 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --stop-timeout 25 -p 127.0.0.1:8000:8000 \
  bessemer:runtime-0.2.0a3
```

The image health check probes native `/healthz` on loopback without using HTTP
proxy variables. An application that changes its health path or listening port
must supply a matching container health check. Put the service behind a trusted
TLS ingress. The loopback publishing example exposes it only to the local host.
Size worker counts and bounds for the real workload, including memory for a
replacement worker overlapping its predecessor. Memory and PID limits belong
to the container/cgroup; the framework's RSS recycling setting is not a hard cap.

For a bind-mounted application, UID 10001 must be able to read the source files
and traverse mounted directories. A host file with mode `0600` owned by another
UID is correctly rejected. Prefer building application code into the image with
appropriate ownership and read permissions; test the same permissions and
read-only mounts you will deploy.

Allow at least the graceful timeout, one second for reaping, and a scheduling
margin before the container platform sends SIGKILL. The supplied 25-second
window accommodates the default 20-second graceful deadline. The CLI supervisor
runs as PID 1 and handles TERM, worker recovery and reaping. See
[supervision](supervision.md) for cancellation, restart and descendant ownership.

## Authenticated example and orchestration

The production example requires `BESSEMER_EXAMPLE_API_KEY` from a secret manager.
Use a strong value of 16–4096 visible ASCII characters without spaces; never
bake it into the image or commit it into a manifest. Clients send it as
`X-API-Key`. `/healthz` and `/readyz` are explicitly public. API documentation is
disabled unless `BESSEMER_EXAMPLE_DOCS=1`; enabling it deliberately makes the
example's documentation paths public.

After building and verifying the production image, set `BESSEMER_IMAGE` to that
image and make the API key available to the deployment process, then run:

```sh
docker compose --file deploy/compose.yaml config --quiet
docker compose --file deploy/compose.yaml up --detach --no-build
```

[compose.yaml](../deploy/compose.yaml) sets CPU, memory, PID, filesystem,
capability, log rotation and stop bounds. Keep its secret environment value out
of generated configuration dumps. For a registry deployment, select the tested
immutable registry digest and verify that it corresponds to the recorded image.
Do not rely on a mutable tag to identify a qualified release.

[kubernetes.yaml](../deploy/kubernetes.yaml) is a preparation template, not an
applied deployment. Replace the image placeholder with a qualified registry
digest, configure the application's allowed host, and provision the referenced
`bessemer-api-key` Secret through your cluster's secret-management system. It
includes two replicas, rolling replacement, startup/readiness/liveness probes,
CPU and memory limits, a read-only root filesystem, dropped capabilities and a
memory-backed temporary directory. Probes explicitly use an allowed Host so a
Pod IP does not fail the application's host policy. Configure pod PID bounds in
the node/runtime policy and add network policy and ingress for your cluster.
The template has not been validated against a live Kubernetes cluster.

Configure proxy trust only for actual ingress addresses. Preserve the intended
Host and validate it through `allowed_hosts`. The example's health exemptions
and API key do not substitute for application authorization. See
[native controls](production-controls.md) for exact matching, secret rotation,
CORS, rate-limit and logging behavior. Rate quotas are per worker/replica. The
current metrics endpoint requires one worker per process and one scrape target
per replica.

## Qualification

```sh
python3 tests/deploy/qualify_container.py --image bessemer:runtime-0.2.0a3 \
  --output work/container-tests
```

The harness pins each run to the inspected immutable image ID, checks the
configured resource limits inside the container, and saves its manifest and
logs. It checks the untouched default application, native and Python HTTP,
non-root execution, worker crash recovery, in-flight graceful shutdown, hard
termination of a blocked loop, and container teardown after PID 1 is killed.
Qualification fixtures are mounted read-only for tests and are not included in
the runtime image. Its generated manifest is the evidence for the tested image;
the presence of the harness alone does not establish a pass.

The recorded [4 November a2 qualification](../deploy/qualification/2026-11-04-a2.json)
contains the image IDs, exact source-file hashes, package inventories and full
test manifests. Both 512 MiB and 256 MiB runs passed native/Python HTTP, worker
crash recovery, live SIGHUP replacement, normal lifespan cleanup, hard deadlines
and PID 1 teardown. The observed approximately 58.6 MiB overlap peak applies to
that small fixture. The production image passed authentication, model, host,
missing-secret and shutdown checks. These historical a2 results must not be
applied to a later image without rerunning qualification.

OS advisory scanning is separate from Python dependency auditing. The pinned
Trivy scanner in `deploy/scan_image.py` scans a read-only exported image without
access to the Docker socket and records its database timestamp and SHA-256.
Its database and temporary downloads need several GiB of free disk space.
`completed_with_findings` is a report status, not a release pass. Preserve
affected components, available fixes and applicability analysis rather than
treating an absent fix as a dismissal. Pip and Debian-package scans do not
establish vulnerability coverage of the official image's `/usr/local` CPython
interpreter, arbitrary application code or newly disclosed vulnerabilities.
