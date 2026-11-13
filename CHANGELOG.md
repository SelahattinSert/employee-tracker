# Changelog

## Unreleased

### Added
- Telemetry schema 1.1 sections for opt-in active-window, bounded file-audit, and screenshot data.
- Explicit employee-notice acknowledgement and size/count bounds for sensitive collectors.
- Pillow 13.0.0 in canonical package metadata and the hash-locked deployment dependency set.
- One-click Windows package (`scripts/build_windows_package.py`, `Install.cmd`, `setup.ps1`) that asks for the server address and API token at install time and ships no secrets.
- One-click Linux and macOS packages (`scripts/build_unix_package.py`, `Install.sh`/`Install.command`, `setup.sh`) that ask for the server address, API token and a one-time employee-notice confirmation (default yes, enabling screenshots, active window and file audit) and ship no secrets.
- `release` workflow that, on a version tag, builds a single-file installer per platform (Windows self-extracting `.exe`, Linux `.run`, macOS `.dmg`) and attaches them to the GitHub Release; `scripts/make_selfextract.sh` builds the Linux self-extractor. Installers ship no secrets.

### Fixed
- Windows `setup.ps1` honours `-NoEmployeesNotified` again (it had been inverted to force sensitive collection on); the opt-out now disables screenshots, active window and file audit as documented, and a regression test guards it.
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
