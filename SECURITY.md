# Security policy

## Supported versions

Security fixes are made for the latest minor release only. Pre-releases
(alpha, beta and release candidates) are superseded by the next release.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/CodeHalwell/bessemer/security/advisories/new)
rather than in a public issue. Include the Bessemer version, Python version,
platform, a minimal reproduction and the impact you observed.

You should get an acknowledgement within five working days. Fixes are released
as a patch version with an advisory that credits the reporter unless you ask
otherwise.

## Scope

In scope: the `bessemer` package, its native extension, the CLI supervisor and
the supplied container image. Application code, ingress/TLS configuration and
operating-system packages in custom images are the deployer's responsibility.
See [production controls](docs/production-controls.md) and
[deployment](docs/deployment.md) for the operational boundaries Bessemer
enforces and those it leaves to infrastructure.
