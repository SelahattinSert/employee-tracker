# Changelog

## 2.0.0 - 2026-07-20

### Added
- Installable cross-platform package and operational CLI.
- Failure-isolated collectors with structured status metadata.
- Classified HTTP retries and bounded atomic offline spool.
- Hardened Linux, Windows, and macOS deployment workflows.
- Cross-platform tests, typing, linting, package, and dependency gates.

### Changed
- Production runtime target moves to Python 3.14.6.
- Machine identity now derives from hashed platform identifiers.
- Process command lines default to secret redaction.
- Telemetry scheduling uses a monotonic standard-library loop.

### Removed
- In-process `schedule` dependency.
- Inline service credentials and hard-coded Python 3.11 paths.
