from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from monitor_agent.config import AgentConfig
from monitor_agent.models import JSONValue

_REDACTED = "[REDACTED]"
_BEARER_PATTERN = re.compile(
    r"(authorization\s*:\s*bearer\s+)(\S+)",
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
_ACTIVE_MARKER_PATTERN = re.compile(r"\x00(?P<index>\d+)\x00")
_NOFOLLOW = cast(int, getattr(os, "O_NOFOLLOW", 0))
_MAX_LOG_BYTES = 10_485_760
_BACKUP_COUNT = 5
_INTEGER_CONVERSIONS = frozenset("diouxX")
_FLOAT_CONVERSIONS = frozenset("eEfFgG")


@dataclass(frozen=True)
class _ActiveConversion:
    start: int
    end: int
    conversion: str
    argument_index: int | None = None
    key: str | None = None


class _SafeRepresentation:
    def __repr__(self) -> str:
        return _REDACTED


_SAFE_REPRESENTATION = _SafeRepresentation()


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

    @staticmethod
    def _active_conversions(
        template: str,
        args: tuple[object, ...] | Mapping[str, object],
    ) -> tuple[list[_ActiveConversion], int]:
        active: list[_ActiveConversion] = []
        if isinstance(args, tuple):
            if not args:
                return active, 0
            argument_index = 0
            arguments_exhausted = False
            for match in _PRINTF_PATTERN.finditer(template):
                if match.group("conversion") == "%":
                    active.append(
                        _ActiveConversion(
                            match.start(),
                            match.end(),
                            match.group("conversion"),
                        )
                    )
                    continue
                if arguments_exhausted:
                    continue
                if match.group("key") is not None:
                    arguments_exhausted = True
                    continue
                star_count = int(match.group("width") == "*") + int(
                    match.group("precision") == "*"
                )
                if argument_index + star_count >= len(args):
                    arguments_exhausted = True
                    continue
                active.append(
                    _ActiveConversion(
                        match.start(),
                        match.end(),
                        match.group("conversion"),
                        argument_index=argument_index + star_count,
                    )
                )
                argument_index += star_count + 1
            return active, argument_index

        if not args:
            return active, 0
        for match in _PRINTF_PATTERN.finditer(template):
            if match.group("conversion") == "%":
                active.append(
                    _ActiveConversion(
                        match.start(),
                        match.end(),
                        match.group("conversion"),
                    )
                )
                continue
            key = match.group("key")
            if (
                key is None
                or match.group("width") == "*"
                or match.group("precision") == "*"
                or key not in args
            ):
                continue
            active.append(
                _ActiveConversion(
                    match.start(),
                    match.end(),
                    match.group("conversion"),
                    key=key,
                )
            )
        return active, 0

    def _redact_secrets_preserving_markers(self, value: str) -> str:
        parts: list[str] = []
        previous_end = 0
        for match in _ACTIVE_MARKER_PATTERN.finditer(value):
            part = value[previous_end : match.start()]
            for secret in self._secrets:
                part = part.replace(secret, _REDACTED)
            parts.append(part)
            parts.append(match.group(0))
            previous_end = match.end()
        part = value[previous_end:]
        for secret in self._secrets:
            part = part.replace(secret, _REDACTED)
        parts.append(part)
        return "".join(parts)

    def _redact_template(
        self,
        template: str,
        active: list[_ActiveConversion],
    ) -> tuple[str, set[int]]:
        marked_parts: list[str] = []
        previous_end = 0
        for index, conversion in enumerate(active):
            marked_parts.append(template[previous_end : conversion.start])
            marked_parts.append(f"\x00{index}\x00")
            previous_end = conversion.end
        marked_parts.append(template[previous_end:])
        marked = "".join(marked_parts)
        bearer_conversions: set[int] = set()

        def redact_bearer(match: re.Match[str]) -> str:
            markers = list(_ACTIVE_MARKER_PATTERN.finditer(match.group(2)))
            bearer_conversions.update(
                int(marker.group("index")) for marker in markers
            )
            if not markers:
                return f"{match.group(1)}{_REDACTED}"
            return match.group(1) + "".join(marker.group(0) for marker in markers)

        marked = _BEARER_PATTERN.sub(redact_bearer, marked)
        marked = self._redact_secrets_preserving_markers(marked)

        def restore_conversion(match: re.Match[str]) -> str:
            conversion = active[int(match.group("index"))]
            return template[conversion.start : conversion.end]

        return _ACTIVE_MARKER_PATTERN.sub(restore_conversion, marked), bearer_conversions

    @staticmethod
    def _safe_sentinel(conversions: set[str]) -> object:
        if conversions & _INTEGER_CONVERSIONS:
            return 0
        if conversions & _FLOAT_CONVERSIONS:
            return 0.0
        if "c" in conversions:
            return "*"
        if conversions & {"r", "a"}:
            return _SAFE_REPRESENTATION
        return _REDACTED

    @staticmethod
    def _argument_conversions(
        active: list[_ActiveConversion],
        bearer_conversions: set[int],
    ) -> tuple[dict[int, set[str]], dict[str, set[str]], set[int], set[str]]:
        positional: dict[int, set[str]] = {}
        mapping: dict[str, set[str]] = {}
        positional_bearers: set[int] = set()
        mapping_bearers: set[str] = set()
        for conversion_index, conversion in enumerate(active):
            if conversion.argument_index is not None:
                positional.setdefault(conversion.argument_index, set()).add(
                    conversion.conversion
                )
                if conversion_index in bearer_conversions:
                    positional_bearers.add(conversion.argument_index)
            elif conversion.key is not None:
                mapping.setdefault(conversion.key, set()).add(conversion.conversion)
                if conversion_index in bearer_conversions:
                    mapping_bearers.add(conversion.key)
        return positional, mapping, positional_bearers, mapping_bearers

    def _redact_argument(self, value: object, conversions: set[str]) -> object:
        if not isinstance(value, str):
            return value
        redacted = self._redact(value)
        if redacted == value:
            return value
        return self._safe_sentinel(conversions) if "c" in conversions else redacted

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            return True
        args = record.args
        if not isinstance(args, (tuple, Mapping)):
            record.msg = self._redact(record.msg)
            return True
        active, consumed_arguments = self._active_conversions(record.msg, args)
        record.msg, bearer_conversions = self._redact_template(record.msg, active)
        (
            positional_conversions,
            mapping_conversions,
            positional_bearers,
            mapping_bearers,
        ) = self._argument_conversions(active, bearer_conversions)

        if isinstance(args, tuple):
            redacted_args: list[object] = []
            for index, value in enumerate(args[:consumed_arguments]):
                conversions = positional_conversions.get(index, set())
                redacted_args.append(
                    self._safe_sentinel(conversions)
                    if index in positional_bearers
                    else self._redact_argument(value, conversions)
                )
            record.args = tuple(redacted_args)
        else:
            record.args = {
                key: (
                    self._safe_sentinel(mapping_conversions.get(key, set()))
                    if key in mapping_bearers
                    else self._redact_argument(
                        value,
                        mapping_conversions.get(key, set()),
                    )
                )
                for key, value in args.items()
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
