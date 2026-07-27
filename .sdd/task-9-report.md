# Task 9: Atomic Bounded Offline Spool

## Delivered

- Added immutable `SpoolStats` and `RetentionResult` result models.
- Added `Spool`, which creates owner-only spool/dead-letter directories and writes compact,
  deterministic UTF-8 JSON through same-directory temp files, file fsync, chmod, atomic replace,
  and POSIX directory fsync.
- Validates timezone-aware timestamps, UUID string event IDs, mapping payloads, and JSON
  serialization before persistence. Temporary files and reserved destinations are cleaned on errors.
- Provides deterministic oldest-first replay, logical-record-time age retention, byte retention,
  owner-only record modes, dead-letter handling, idempotent ack/reject behavior, safe collision
  handling, exact statistics, and an `RLock` around each public operation.
- Rejects paths outside the pending spool root, symlinks, directories, and other non-regular files
  so ack/load/reject cannot act on arbitrary filesystem targets.

## Test-first evidence

1. Before implementation, `./.venv/bin/python -m pytest tests/test_spool.py -q` failed at
   collection because the required spool result models did not exist.
2. After adding only those models, the same command failed with
   `ModuleNotFoundError: No module named 'monitor_agent.spool'`.
3. The focused test suite was then implemented and driven green.

## Verification

| Check | Result |
| --- | --- |
| `./.venv/bin/python -m pytest tests/test_spool.py -q --no-cov` | 26 passed |
| `./.venv/bin/python -m ruff check src/monitor_agent/models.py src/monitor_agent/spool.py tests/test_spool.py` | passed |
| `./.venv/bin/python -m mypy src/monitor_agent/models.py src/monitor_agent/spool.py` | passed, no issues in 2 source files |
| Focused spool line coverage (`--cov=monitor_agent.spool`) | 269 / 280 = 96.07% |
| Focused spool branch coverage (`--cov=monitor_agent.spool --cov-branch`) | 60 / 66 = 90.91% |
| Focused combined coverage | 95.09% |
| `./.venv/bin/python -m pytest -q` (run once when ready) | 223 passed; global line+branch coverage 96.50% |

The focused tests cover atomic cleanup, permissions, Unicode JSON, ordering/ties, historical age
retention, size accounting, corrupt/non-mapping/invalid-ID dead letters, ack/reject idempotency,
outside-path/symlink protections, collision handling, concurrency, and both directory-fsync
portability branches.
