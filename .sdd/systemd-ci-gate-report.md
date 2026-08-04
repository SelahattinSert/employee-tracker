# Systemd CI Gate Report

Base: `bcf9128`

## Change

The Linux deployment job now verifies `monitor-agent.service` against a temporary
filesystem root that contains the deployment contract's absolute executable and
environment-file paths. The verifier uses `--recursive-errors=yes`, so unit-file
warnings cannot silently pass with the default zero exit status.

The staged executable is `/bin/true` copied to the exact `ExecStart` and
`ExecStartPre` path. This isolates unit-path validation from the undeployed host
filesystem while ensuring a future service-file path change fails unless the CI
contract is updated deliberately.

## TDD Evidence

RED:

- Added
  `test_systemd_unit_is_verified_against_staged_runtime_paths`.
- `python -m pytest tests/test_ci_workflow.py -q --no-cov` failed exactly because
  the workflow still used direct host verification.

GREEN:

- `python -m pytest tests/test_ci_workflow.py tests/deploy/test_linux.py -q
  --no-cov`: `161 passed`.
- `bash -n deploy/linux/install.sh`: passed.
- `bash -n deploy/linux/uninstall.sh`: passed.
- `shellcheck deploy/linux/install.sh deploy/linux/uninstall.sh`: passed.
- `python -m ruff format --check tests/test_ci_workflow.py`: passed.
- `python -m ruff check tests/test_ci_workflow.py`: passed.
- `git diff --check -- .github/workflows/ci.yml tests/test_ci_workflow.py`:
  passed.

## Local Host Limitation

This restricted development host makes `systemd-analyze verify` exit `1` after
failing to change credential-passing socket options, both with and without
`--recursive-errors=yes`. The staged unit emits no missing executable or
environment-file diagnostic. The unrestricted Ubuntu 24.04 CI runner executes
the strict verification gate.

Status: DONE
