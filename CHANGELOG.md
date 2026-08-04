# Changelog

## Unreleased

### Added
- Telemetry schema 1.1 sections for opt-in active-window, bounded file-audit, and screenshot data.
- Explicit employee-notice acknowledgement and size/count bounds for sensitive collectors.
- Pillow 12.3.0 in canonical package metadata and the hash-locked deployment dependency set.

### Fixed
- New collector data now survives payload construction and reports disabled or partial states honestly.
- Windows audit path parsing preserves drive letters by using the platform path-list separator.
- Screenshot and active-window failures no longer appear as successful empty captures.

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
