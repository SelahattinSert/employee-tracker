from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from monitor_agent import logging_setup
from monitor_agent.config import AgentConfig, load_config
from monitor_agent.logging_setup import JsonFormatter, SecretFilter, configure_logging


@pytest.fixture(autouse=True)
def clean_agent_logger() -> Iterator[None]:
    logger = logging.getLogger("monitor_agent")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = []
    yield
    for handler in logger.handlers:
        handler.close()
    logger.handlers = original_handlers
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def config(tmp_path: Path, **values: str) -> AgentConfig:
    env = {
        "MONITOR_SPOOL_PATH": str(tmp_path / "spool"),
        **values,
    }
    return load_config(env, require_transport=False, platform_name="linux")


def record(message: str, args: object = ()) -> logging.LogRecord:
    log_args = (args,) if isinstance(args, Mapping) else args
    return logging.LogRecord(
        "monitor_agent.worker",
        logging.INFO,
        __file__,
        1,
        message,
        log_args,  # type: ignore[arg-type]
        None,
    )


def test_secret_filter_redacts_tuple_arguments_and_message_template() -> None:
    item = record("token=top-secret value=%s", ("top-secret",))

    assert SecretFilter(["top-secret"]).filter(item)

    assert item.msg == "token=[REDACTED] value=%s"
    assert item.args == ("[REDACTED]",)
    assert item.getMessage() == "token=[REDACTED] value=[REDACTED]"


def test_secret_filter_redacts_mapping_arguments_without_mutating_input() -> None:
    arguments = {"credential": "top-secret", "count": 4}
    item = record("credential=%(credential)s count=%(count)d", arguments)

    SecretFilter(["top-secret"]).filter(item)

    assert arguments == {"credential": "top-secret", "count": 4}
    assert item.args == {"credential": "[REDACTED]", "count": 4}
    assert item.getMessage() == "credential=[REDACTED] count=4"


def test_secret_filter_masks_mixed_case_bearer_headers_and_ignores_empty_secrets() -> None:
    item = record(
        "aUtHoRiZaTiOn: bEaReR opaque-value note=%s",
        ("AUTHORIZATION: BEARER another-value",),
    )

    SecretFilter(["", ""]).filter(item)

    assert "opaque-value" not in item.getMessage()
    assert "another-value" not in item.getMessage()
    assert item.getMessage() == (
        "aUtHoRiZaTiOn: bEaReR [REDACTED] "
        "note=AUTHORIZATION: BEARER [REDACTED]"
    )


def test_secret_filter_keeps_deferred_bearer_placeholder_valid() -> None:
    item = record("Authorization: Bearer %s", ("top-secret",))

    SecretFilter(["top-secret"]).filter(item)

    assert item.msg == "Authorization: Bearer %s"
    assert item.getMessage() == "Authorization: Bearer [REDACTED]"


@pytest.mark.parametrize(
    ("message", "secrets", "expected"),
    [
        ("Authorization: Bearer %s", [], "Authorization: Bearer [REDACTED]"),
        ("Authorization: Bearer %token", [], "Authorization: Bearer [REDACTED]"),
        ("Authorization: Bearer %%token", [], "Authorization: Bearer [REDACTED]"),
        ("Authorization: Bearer %2Fsecret", [], "Authorization: Bearer [REDACTED]"),
        ("configured=%s", ["%s"], "configured=[REDACTED]"),
    ],
)
def test_secret_filter_masks_literal_percent_text_without_arguments(
    message: str,
    secrets: list[str],
    expected: str,
) -> None:
    item = record(message)

    SecretFilter(secrets).filter(item)

    assert item.args == ()
    assert item.getMessage() == expected


def test_secret_filter_masks_unconsumed_bearer_after_active_placeholder() -> None:
    item = record(
        "state=%s Authorization: Bearer %s progress=100%%",
        ("ready",),
    )

    SecretFilter(["%"]).filter(item)

    assert item.msg == (
        "state=%s Authorization: Bearer [REDACTED] progress=100%%"
    )
    assert item.args == ("ready",)
    assert item.getMessage() == (
        "state=ready Authorization: Bearer [REDACTED] progress=100%"
    )


def test_secret_filter_preserves_percent_escape_when_formatting_is_active() -> None:
    item = record("progress=100%% state=%s", ("ready",))

    SecretFilter(["%"]).filter(item)

    assert item.msg == "progress=100%% state=%s"
    assert item.args == ("ready",)
    assert item.getMessage() == "progress=100% state=ready"


def test_secret_filter_masks_bearer_percent_escape_after_active_placeholder() -> None:
    item = record(
        "state=%s Authorization: Bearer %%token",
        ("ready",),
    )

    SecretFilter([]).filter(item)

    assert item.msg == "state=%s Authorization: Bearer [REDACTED]"
    assert item.args == ("ready",)
    assert item.getMessage() == "state=ready Authorization: Bearer [REDACTED]"


def test_secret_filter_discards_bearer_percent_escape_before_placeholder() -> None:
    item = record(
        "state=%s Authorization: Bearer %%%s",
        ("ready", "credential"),
    )

    SecretFilter([]).filter(item)

    assert item.msg == "state=%s Authorization: Bearer %s"
    assert item.args == ("ready", "[REDACTED]")
    assert item.getMessage() == "state=ready Authorization: Bearer [REDACTED]"


def test_secret_filter_preserves_unrelated_percent_escape_with_mapping_formatting() -> None:
    item = record(
        "progress=100%% state=%(state)s Authorization: Bearer %%token",
        {"state": "ready"},
    )

    SecretFilter([]).filter(item)

    assert item.msg == "progress=100%% state=%(state)s Authorization: Bearer [REDACTED]"
    assert item.args == {"state": "ready"}
    assert item.getMessage() == (
        "progress=100% state=ready Authorization: Bearer [REDACTED]"
    )


def test_secret_filter_masks_empty_secret_positional_bearer_after_placeholder() -> None:
    item = record(
        "state=%s Authorization: Bearer %s",
        ("ready", "unconfigured-positional-credential"),
    )

    SecretFilter([""]).filter(item)

    assert item.msg == "state=%s Authorization: Bearer %s"
    assert item.args == ("ready", "[REDACTED]")
    assert item.getMessage() == "state=ready Authorization: Bearer [REDACTED]"


def test_secret_filter_masks_empty_secret_mapping_bearer_after_placeholder() -> None:
    arguments = {
        "state": "ready",
        "credential": "unconfigured-mapping-credential",
    }
    item = record(
        "state=%(state)s Authorization: Bearer %(credential)s",
        arguments,
    )

    SecretFilter([""]).filter(item)

    assert item.msg == "state=%(state)s Authorization: Bearer %(credential)s"
    assert arguments["credential"] == "unconfigured-mapping-credential"
    assert item.args == {"state": "ready", "credential": "[REDACTED]"}
    assert item.getMessage() == "state=ready Authorization: Bearer [REDACTED]"


def test_secret_filter_keeps_positional_char_and_star_formatting_valid() -> None:
    arguments = (6, 3, "ready", "X")
    item = record(
        "state=%*.*s Authorization: Bearer %c",
        arguments,
    )

    SecretFilter([]).filter(item)

    assert arguments == (6, 3, "ready", "X")
    assert item.msg == "state=%*.*s Authorization: Bearer %c"
    assert item.args == (6, 3, "ready", "*")
    assert item.getMessage() == "state=   rea Authorization: Bearer *"


def test_secret_filter_keeps_mapping_char_formatting_valid_without_mutation() -> None:
    arguments = {"credential": "X", "count": 4}
    item = record(
        "Authorization: Bearer %(credential)c count=%(count)d",
        arguments,
    )

    SecretFilter([]).filter(item)

    assert arguments == {"credential": "X", "count": 4}
    assert item.msg == "Authorization: Bearer %(credential)c count=%(count)d"
    assert item.args == {"credential": "*", "count": 4}
    assert item.getMessage() == "Authorization: Bearer * count=4"


def test_secret_filter_keeps_configured_secret_char_formatting_valid() -> None:
    arguments = {"credential": "X", "count": 1}
    item = record("credential=%(credential)c count=%(count)d", arguments)

    SecretFilter(["X"]).filter(item)

    assert arguments == {"credential": "X", "count": 1}
    assert item.args == {"credential": "*", "count": 1}
    assert item.getMessage() == "credential=* count=1"


@pytest.mark.parametrize(
    ("conversion", "argument", "expected"),
    [
        ("d", 123, "0"),
        ("x", 255, "0"),
        ("f", 1.25, "0.000000"),
        ("c", "X", "*"),
        ("s", "secret", "[REDACTED]"),
        ("r", "secret", "[REDACTED]"),
        ("a", "secret", "[REDACTED]"),
    ],
)
def test_secret_filter_uses_type_compatible_bearer_sentinels(
    conversion: str,
    argument: object,
    expected: str,
) -> None:
    item = record(f"Authorization: Bearer %{conversion}", (argument,))

    SecretFilter([]).filter(item)

    assert item.getMessage() == f"Authorization: Bearer {expected}"


@pytest.mark.parametrize(
    ("secret", "value", "expected"),
    [
        ("%s", "%s", "[REDACTED]"),
        ("s", "status", "[REDACTED]tatu[REDACTED]"),
    ],
)
def test_secret_filter_never_corrupts_overlapping_positional_format_tokens(
    secret: str,
    value: str,
    expected: str,
) -> None:
    item = record(
        "status=%s Authorization: Bearer %s",
        (value, "unconfigured-bearer-credential"),
    )

    SecretFilter([secret]).filter(item)

    assert item.msg == (
        "[REDACTED]tatu[REDACTED]=%s Authorization: Bearer %s"
        if secret == "s"
        else "status=%s Authorization: Bearer %s"
    )
    assert item.getMessage() == (
        f"{'[REDACTED]tatu[REDACTED]' if secret == 's' else 'status'}="
        f"{expected} Authorization: Bearer [REDACTED]"
    )


def test_secret_filter_never_corrupts_overlapping_mapping_format_tokens() -> None:
    item = record(
        "status=%(state)s Authorization: Bearer %(credential)s",
        {"state": "status", "credential": "unconfigured-bearer-credential"},
    )

    SecretFilter(["s"]).filter(item)

    assert item.msg == (
        "[REDACTED]tatu[REDACTED]=%(state)s "
        "Authorization: Bearer %(credential)s"
    )
    assert item.getMessage() == (
        "[REDACTED]tatu[REDACTED]=[REDACTED]tatu[REDACTED] "
        "Authorization: Bearer [REDACTED]"
    )


def test_secret_filter_preserves_non_string_message_without_arguments() -> None:
    item = record("placeholder")
    marker = object()
    item.msg = marker
    item.args = None  # type: ignore[assignment]

    SecretFilter(["top-secret"]).filter(item)

    assert item.msg is marker
    assert item.args is None


def test_json_formatter_emits_compact_utc_fields_and_optional_event_id() -> None:
    item = record("cycle %s", ("complete",))
    item.created = 1_721_376_000.125
    item.event_id = "event-123"

    rendered = JsonFormatter().format(item)
    decoded = json.loads(rendered)

    assert ": " not in rendered
    assert ", " not in rendered
    assert decoded == {
        "timestamp": "2024-07-19T08:00:00.125000Z",
        "level": "INFO",
        "component": "monitor_agent.worker",
        "message": "cycle complete",
        "event_id": "event-123",
    }

    without_event = JsonFormatter().format(record("ready"))
    assert "event_id" not in json.loads(without_event)


def test_configure_logging_uses_stdout_text_utc_level_and_no_propagation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_config = config(tmp_path, MONITOR_LOG_LEVEL="warning")

    configure_logging(agent_config)
    logger = logging.getLogger("monitor_agent.worker")
    logger.info("hidden")
    logger.warning("ready")

    output = capsys.readouterr().out
    assert "hidden" not in output
    assert "WARNING monitor_agent.worker ready" in output
    assert output.split()[0].endswith("Z")
    assert logging.getLogger("monitor_agent").propagate is False


def test_configure_logging_replaces_its_handler_without_duplicates(
    tmp_path: Path,
) -> None:
    agent_config = config(tmp_path)

    configure_logging(agent_config)
    first = logging.getLogger("monitor_agent").handlers[0]
    configure_logging(agent_config)

    handlers = logging.getLogger("monitor_agent").handlers
    assert len(handlers) == 1
    assert handlers[0] is not first


def test_configure_logging_observably_closes_previous_handler(tmp_path: Path) -> None:
    logger = logging.getLogger("monitor_agent")

    class TrackingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    previous = TrackingHandler()
    logger.handlers = [previous]

    configure_logging(config(tmp_path))

    assert previous.close_calls == 1


def test_configure_logging_preserves_root_and_unrelated_loggers(tmp_path: Path) -> None:
    root = logging.getLogger()
    unrelated = logging.getLogger("unrelated")
    root_handler = logging.NullHandler()
    unrelated_handler = logging.NullHandler()
    root.addHandler(root_handler)
    unrelated.addHandler(unrelated_handler)
    try:
        configure_logging(config(tmp_path))

        assert root_handler in root.handlers
        assert unrelated_handler in unrelated.handlers
    finally:
        root.removeHandler(root_handler)
        unrelated.removeHandler(unrelated_handler)


def test_configure_logging_file_rotation_permissions_and_secret_filter(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "private" / "agent.log"
    agent_config = config(
        tmp_path,
        MONITOR_API_TOKEN="top-secret",
        MONITOR_LOG_PATH=str(log_path),
        MONITOR_LOG_FORMAT="json",
    )

    configure_logging(agent_config)
    handler = logging.getLogger("monitor_agent").handlers[0]
    logging.getLogger("monitor_agent.transport").warning(
        "Authorization: Bearer other token=%s",
        "top-secret",
    )
    handler.flush()

    assert handler.maxBytes == 10_485_760  # type: ignore[attr-defined]
    assert handler.backupCount == 5  # type: ignore[attr-defined]
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
    if os.name == "posix":
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    contents = log_path.read_text(encoding="utf-8")
    assert "top-secret" not in contents
    assert "other" not in contents
    assert json.loads(contents)["message"] == (
        "Authorization: Bearer [REDACTED] token=[REDACTED]"
    )


def test_file_logging_does_not_follow_symlink_target(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform does not support no-follow file opening")
    target = tmp_path / "target.log"
    target.write_text("unchanged", encoding="utf-8")
    log_path = tmp_path / "agent.log"
    log_path.symlink_to(target)

    with pytest.raises(OSError):
        configure_logging(config(tmp_path, MONITOR_LOG_PATH=str(log_path)))

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_file_logging_avoids_posix_mode_calls_on_other_platforms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "portable" / "agent.log"
    monkeypatch.setattr(logging_setup, "_supports_posix_permissions", lambda: False)
    monkeypatch.setattr(
        logging_setup.os,
        "fchmod",
        lambda *args: pytest.fail("non-POSIX logging must not call fchmod"),
    )

    configure_logging(config(tmp_path, MONITOR_LOG_PATH=str(log_path)))

    logging.getLogger("monitor_agent").handlers[0].close()
    assert log_path.is_file()
