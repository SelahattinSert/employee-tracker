# Network payload envelope fix

Status: DONE

Base: `0856423`

## Defect

`NetworkCollector.collect()` returned `adapters`, `connections`, and `io` at the
top level of its collector data. `build_payload()` only copies known payload
sections such as `network`, so successful and partial network telemetry was
silently replaced by the empty payload default.

## TDD evidence

RED:

```text
PYTHONPATH=src .venv/bin/pytest --no-cov \
  tests/test_payload.py::test_payload_includes_network_collector_output -q

AssertionError:
{'adapters': [], 'connections': [], 'io': {}} !=
{'adapters': [{'interface': 'eth0'}], 'connections': [], 'io': {'bytes_recv': 42}}
```

GREEN:

```text
PYTHONPATH=src .venv/bin/pytest --no-cov \
  tests/test_payload.py::test_payload_includes_network_collector_output \
  tests/collectors/test_network.py -q

7 passed
```

## Implementation

- Wrapped successful, partial, and disabled collector data in the canonical
  `network` section.
- Updated collector contract tests for the canonical envelope.
- Added a collector-to-payload regression test that proves real network
  telemetry reaches `payload["network"]`.

## Verification

```text
PYTHONPATH=src .venv/bin/pytest -q
638 passed, 2 skipped
95.96% coverage

.venv/bin/ruff check src tests
All checks passed!

MYPYPATH=src .venv/bin/mypy src/monitor_agent
Success: no issues found in 20 source files

git diff --check
clean
```
