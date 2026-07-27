from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from monitor_agent.config import AgentConfig
from monitor_agent.models import JSONValue

_REDACTED = "[REDACTED]"
_BEARER_PATTERN = re.compile(
    r"(authorization\s*:\s*bearer\s+)(?!%)(\S+)",
    flags=re.IGNORECASE,
)
_BEARER_PLACEHOLDER_PREFIX = re.compile(
    r"authorization\s*:\s*bearer\s*$",
    flags=re.IGNORECASE,
)
_PRINTF_PATTERN = re.compile(
    r"%(?:\((?P<key>[^)]+)\))?"
    r"(?P<flags>[-#0 +]*)"
    r"(?P<width>\*|\d*)"
    r"(?:\.(?P<precision>\*|\d*))?"
    r"[hlL]?"
    r"(?P<conversion>[diouxXeEfFgGcrsa%])"
)
_NOFOLLOW = cast(int, getattr(os, "O_NOFOLLOW", 0))
_MAX_LOG_BYTES = 10_485_760
_BACKUP_COUNT = 5


def _utc_timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _supports_posix_permissions() -> bool:
    return os.name == "posix"


class SecretFilter(logging.Filter):
    """Redact configured credentials while retaining deferred formatting."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def _redact(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, _REDACTED)
        return _BEARER_PATTERN.sub(rf"\1{_REDACTED}", redacted)

    def _redact_template(self, template: str) -> str:
        parts: list[str] = []
        previous_end = 0
        for match in _PRINTF_PATTERN.finditer(template):
            parts.append(self._redact(template[previous_end : match.start()]))
            parts.append(match.group(0))
            previous_end = match.end()
        parts.append(self._redact(template[previous_end:]))
        return "".join(parts)

    @staticmethod
    def _bearer_placeholders(template: str) -> tuple[set[int], set[str]]:
        positional: set[int] = set()
        mapping: set[str] = set()
        argument_index = 0
        for match in _PRINTF_PATTERN.finditer(template):
            if match.group("conversion") == "%":
                continue
            key = match.group("key")
            is_bearer = _BEARER_PLACEHOLDER_PREFIX.search(
                template[: match.start()]
            )
            if key is not None:
                if is_bearer:
                    mapping.add(key)
                continue
            argument_index += int(match.group("width") == "*")
            argument_index += int(match.group("precision") == "*")
            if is_bearer:
                positional.add(argument_index)
            argument_index += 1
        return positional, mapping

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            positional_bearers, mapping_bearers = self._bearer_placeholders(
                record.msg
            )
            record.msg = self._redact_template(record.msg)
        else:
            positional_bearers, mapping_bearers = set(), set()
        if isinstance(record.args, tuple):
            record.args = tuple(
                (
                    _REDACTED
                    if index in positional_bearers
                    else self._redact(value)
                )
                if isinstance(value, str)
                else value
                for index, value in enumerate(record.args)
            )
        elif isinstance(record.args, Mapping):
            record.args = {
                key: (
                    _REDACTED
                    if key in mapping_bearers
                    else self._redact(value)
                )
                if isinstance(value, str)
                else value
                for key, value in record.args.items()
            }
        return True


class JsonFormatter(logging.Formatter):
    """Render one compact, structured, exception-text-free log record."""

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, JSONValue] = {
            "timestamp": _utc_timestamp(record.created),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        event_id = getattr(record, "event_id", None)
        if isinstance(event_id, str):
            event["event_id"] = event_id
        return json.dumps(event, separators=(",", ":"), ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return " ".join(
            (
                _utc_timestamp(record.created),
                record.levelname,
                record.name,
                record.getMessage(),
            )
        )


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    def _open(self) -> TextIOWrapper:
        descriptor = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW,
            0o600,
        )
        try:
            if _supports_posix_permissions():
                os.fchmod(descriptor, 0o600)
            return cast(
                TextIOWrapper,
                open(
                    descriptor,
                    self.mode,
                    encoding=self.encoding,
                    errors=self.errors,
                ),
            )
        except BaseException:
            os.close(descriptor)
            raise


def _prepare_log_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _supports_posix_permissions():
        path.parent.chmod(0o700)


def configure_logging(config: AgentConfig) -> None:
    """Configure the agent namespace without changing process-wide logging."""
    if config.log_path is None:
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
    else:
        _prepare_log_parent(config.log_path)
        handler = _OwnerOnlyRotatingFileHandler(
            config.log_path,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )

    handler.setFormatter(JsonFormatter() if config.log_format == "json" else _TextFormatter())
    secrets = () if config.api_token is None else (config.api_token,)
    handler.addFilter(SecretFilter(secrets))

    logger = logging.getLogger("monitor_agent")
    previous_handlers = logger.handlers[:]
    logger.handlers = [handler]
    logger.setLevel(logging.getLevelNamesMapping().get(config.log_level, logging.INFO))
    logger.propagate = False
    for previous in previous_handlers:
        previous.close()
