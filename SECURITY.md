# Security Policy

## Transport and trust

Monitor Agent sends telemetry only to an HTTPS collector. TLS certificate verification cannot be disabled. If your collector uses a private PKI, deploy the CA chain as an owner-restricted regular file and reference it through `MONITOR_CA_BUNDLE`.

The collector URI cannot embed credentials. Keep the API token in the protected platform environment file or inject it from the approved secret manager. The runtime and log filters use secret-safe handling so known token values and Bearer credentials are redacted from logs.

## Privileges and local storage

Linux runs as root, Windows runs as SYSTEM, and macOS runs as root because host-wide process, network, software, service, and protected system facts require that scope. The deployment definitions limit the service surface and protect the runtime, configuration, log, and spool locations with owner-only permissions. Do not relax those permissions to solve a collector error.

The spool contains undelivered telemetry. Treat it as sensitive operational data, keep it owner-only, and do not copy record bodies into tickets or chat.

## Dependency and release checks

Run these checks before approving a build:

```bash
python -m pip install --dry-run --require-hashes -r requirements.lock
pip-audit -r requirements.lock --disable-pip
python -m build
python -m twine check dist/*
```

## Report a vulnerability

Use the repository Security tab's private vulnerability report; never open a public issue for an undisclosed vulnerability. Include the affected release, deployment surface, reproduction steps, impact, and a safe contact path. Do not include API tokens, spool records, raw machine identifiers, or customer telemetry.
