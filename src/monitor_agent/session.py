"""Relay screen and active-window capture from a user's desktop session to the service.

The service runs as root, SYSTEM, or a LaunchDaemon and cannot see a user's desktop. A
``session-helper`` process started inside each graphical login captures on request with the
existing collectors. The helper publishes an owner-only discovery file that names its local
socket or named pipe and a random authentication key. Both sides authenticate with HMAC
challenges and exchange bounded JSON bytes only; nothing is unpickled.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import stat
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib import import_module
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Protocol, cast

import psutil  # type: ignore[import-untyped]

from monitor_agent.collectors.screenshot import ScreenshotCollector
from monitor_agent.collectors.window import ActiveWindowCollector
from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue

logger = logging.getLogger(__name__)

DISCOVERY_FILE = "helper.json"
SOCKET_FILE = "helper.sock"
PIPE_PREFIX = "\\\\.\\pipe\\monitor-agent-"
SCREENSHOT = "screenshot"
ACTIVE_WINDOW = "active_window"

_PROTOCOL_VERSION = 1
_MAX_REQUEST_BYTES = 4096
_MAX_DISCOVERY_BYTES = 4096
_RESPONSE_OVERHEAD_BYTES = 65536
_REQUEST_WAIT_SEC = 5.0
_MAX_TEXT = 4096
_MAX_ERROR_TEXT = 200
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PIPE_PATTERN = re.compile(r"^\\\\\.\\pipe\\monitor-agent-[0-9a-f]{32}$")
_AUTHKEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NOFOLLOW = cast(int, getattr(os, "O_NOFOLLOW", 0))
_CLOEXEC = cast(int, getattr(os, "O_CLOEXEC", 0))
_STATUSES = {status.value: status for status in CollectorStatus}


class _PasswordEntry(Protocol):
    pw_uid: int
    pw_dir: str


class _PasswordModule(Protocol):
    def getpwnam(self, name: str) -> _PasswordEntry: ...


@dataclass(frozen=True, slots=True)
class SessionLocation:
    """Where one logged-in user's helper publishes its discovery file."""

    state_dir: Path
    uid: int | None


@dataclass(frozen=True, slots=True)
class HelperEndpoint:
    family: str
    address: str
    authkey: bytes


def _is_windows(platform_name: str) -> bool:
    return platform_name == "win32"


def user_state_dir(
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the current user's private helper directory."""
    platform = sys.platform if platform_name is None else platform_name
    env = os.environ if environ is None else environ
    if _is_windows(platform):
        local_app_data = env.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is unavailable")
        return Path(local_app_data) / "MonitorAgent"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MonitorAgent" / "Session"
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime_dir) / "monitor-agent"


def _password_module() -> _PasswordModule:
    return cast(_PasswordModule, import_module("pwd"))


def _logged_in_names() -> list[str]:
    sessions = sorted(
        cast(Iterable[object], psutil.users()),
        key=lambda session: float(getattr(session, "started", 0.0) or 0.0),
        reverse=True,
    )
    names: list[str] = []
    for session in sessions:
        name = getattr(session, "name", None)
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def session_locations(
    platform_name: str | None = None,
    *,
    names: Callable[[], list[str]] = _logged_in_names,
    environ: Mapping[str, str] | None = None,
) -> list[SessionLocation]:
    """Return helper directories for logged-in users, most recent login first."""
    platform = sys.platform if platform_name is None else platform_name
    env = os.environ if environ is None else environ
    locations: list[SessionLocation] = []
    for name in names():
        if "/" in name or "\\" in name or name in {".", ".."}:
            continue
        if _is_windows(platform):
            users_root = Path(env.get("SystemDrive", "C:") + "\\") / "Users"
            locations.append(
                SessionLocation(users_root / name / "AppData" / "Local" / "MonitorAgent", None)
            )
            continue
        try:
            entry = _password_module().getpwnam(name)
        except (KeyError, ImportError):
            continue
        if platform == "darwin":
            state_dir = (
                Path(entry.pw_dir) / "Library" / "Application Support" / "MonitorAgent" / "Session"
            )
        else:
            state_dir = Path(f"/run/user/{entry.pw_uid}") / "monitor-agent"
        locations.append(SessionLocation(state_dir, entry.pw_uid))
    return locations


def _owned_private_path(path: Path, uid: int | None, *, kind: int) -> bool:
    try:
        path_stat = os.lstat(path)
    except OSError:
        return False
    if stat.S_IFMT(path_stat.st_mode) != kind:
        return False
    if uid is None:
        return True
    return path_stat.st_uid == uid and not path_stat.st_mode & 0o022


def read_endpoint(location: SessionLocation, platform_name: str | None = None) -> HelperEndpoint:
    """Read and validate one helper discovery file without following links."""
    platform = sys.platform if platform_name is None else platform_name
    if not _owned_private_path(location.state_dir, location.uid, kind=stat.S_IFDIR):
        raise ValueError("helper directory is not private")
    discovery = location.state_dir / DISCOVERY_FILE
    if not _owned_private_path(discovery, location.uid, kind=stat.S_IFREG):
        raise ValueError("helper discovery file is not private")
    descriptor = os.open(discovery, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(_MAX_DISCOVERY_BYTES + 1)
    if len(raw) > _MAX_DISCOVERY_BYTES:
        raise ValueError("helper discovery file is too large")
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict) or decoded.get("version") != _PROTOCOL_VERSION:
        raise ValueError("unsupported helper discovery file")
    family = decoded.get("family")
    address = decoded.get("address")
    authkey = decoded.get("authkey")
    if not isinstance(authkey, str) or _AUTHKEY_PATTERN.fullmatch(authkey) is None:
        raise ValueError("invalid helper authentication key")
    if _is_windows(platform):
        if family != "AF_PIPE" or not isinstance(address, str):
            raise ValueError("invalid helper address")
        if _PIPE_PATTERN.fullmatch(address) is None:
            raise ValueError("invalid helper address")
    else:
        socket_path = location.state_dir / SOCKET_FILE
        if family != "AF_UNIX" or address != str(socket_path):
            raise ValueError("invalid helper address")
        if not _owned_private_path(socket_path, location.uid, kind=stat.S_IFSOCK):
            raise ValueError("helper socket is not private")
    return HelperEndpoint(cast(str, family), cast(str, address), bytes.fromhex(authkey))


def _bounded_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid text field")
    return value


def _validated_screenshot(data: object, max_bytes: int) -> JSONValue:
    if not isinstance(data, dict) or set(data) != {SCREENSHOT}:
        raise ValueError("invalid screenshot data")
    shot = data[SCREENSHOT]
    if shot is None:
        return None
    if not isinstance(shot, dict) or set(shot) != {
        "captured_at",
        "format",
        "size_bytes",
        "data_b64",
    }:
        raise ValueError("invalid screenshot data")
    captured_at = _bounded_text(shot["captured_at"], 64)
    size_bytes = shot["size_bytes"]
    encoded = shot["data_b64"]
    if (
        captured_at is None
        or shot["format"] != "image/png"
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or not 0 < size_bytes <= max_bytes
        or not isinstance(encoded, str)
        or len(encoded) > (max_bytes + 2) // 3 * 4
    ):
        raise ValueError("invalid screenshot data")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("invalid screenshot data") from error
    if len(decoded) != size_bytes or not decoded.startswith(_PNG_SIGNATURE):
        raise ValueError("invalid screenshot data")
    return {
        "captured_at": captured_at,
        "format": "image/png",
        "size_bytes": size_bytes,
        "data_b64": encoded,
    }


def _validated_window(data: object) -> JSONValue:
    if not isinstance(data, dict) or set(data) != {ACTIVE_WINDOW}:
        raise ValueError("invalid active-window data")
    window = data[ACTIVE_WINDOW]
    if not isinstance(window, dict) or set(window) != {"title", "app", "pid"}:
        raise ValueError("invalid active-window data")
    pid = window["pid"]
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid < 0):
        raise ValueError("invalid active-window data")
    return {
        "title": _bounded_text(window["title"], _MAX_TEXT),
        "app": _bounded_text(window["app"], _MAX_TEXT),
        "pid": pid,
    }


def parse_response(raw: bytes, collector: str, max_bytes: int) -> CollectorPayload:
    """Turn untrusted helper bytes into a validated collector payload."""
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("invalid helper response")
    status = _STATUSES.get(cast(str, decoded.get("status")))
    if status is None:
        raise ValueError("invalid helper status")
    error_code = _bounded_text(decoded.get("error_code"), _MAX_ERROR_TEXT)
    error_message = _bounded_text(decoded.get("error_message"), _MAX_ERROR_TEXT)
    if collector == SCREENSHOT:
        section: JSONValue = _validated_screenshot(decoded.get("data"), max_bytes)
    else:
        section = _validated_window(decoded.get("data"))
    return CollectorPayload(
        data={collector: section},
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def request_capture(
    collector: str,
    *,
    max_bytes: int,
    timeout_sec: float,
    platform_name: str | None = None,
    locations: Callable[[], list[SessionLocation]] | None = None,
) -> CollectorPayload | None:
    """Ask logged-in users' helpers for one capture; prefer the most recent success."""
    platform = sys.platform if platform_name is None else platform_name
    candidates = session_locations(platform) if locations is None else locations()
    request = json.dumps({"collector": collector, "max_bytes": max_bytes}).encode("utf-8")
    response_limit = (max_bytes + 2) // 3 * 4 + _RESPONSE_OVERHEAD_BYTES
    fallback: CollectorPayload | None = None
    for location in candidates:
        try:
            endpoint = read_endpoint(location, platform)
            with Client(endpoint.address, endpoint.family, authkey=endpoint.authkey) as conn:
                conn.send_bytes(request)
                if not conn.poll(timeout_sec):
                    continue
                payload = parse_response(conn.recv_bytes(response_limit), collector, max_bytes)
        except (AuthenticationError, OSError, EOFError, ValueError) as error:
            logger.debug("session helper skipped error=%s", type(error).__name__)
            continue
        if payload.status is CollectorStatus.SUCCESS:
            return payload
        fallback = fallback or payload
    return fallback


def handle_request(raw: bytes) -> bytes:
    """Capture with the existing collectors in this user session."""
    try:
        request = json.loads(raw.decode("utf-8"))
        collector = request.get("collector") if isinstance(request, dict) else None
        max_bytes = request.get("max_bytes") if isinstance(request, dict) else None
        if collector == SCREENSHOT:
            if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
                raise ValueError("invalid max_bytes")
            payload = ScreenshotCollector(True, max_bytes).collect()
        elif collector == ACTIVE_WINDOW:
            payload = ActiveWindowCollector(True).collect()
        else:
            raise ValueError("invalid collector")
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return json.dumps(
            {
                "status": CollectorStatus.FAILED.value,
                "error_code": "invalid_request",
                "error_message": "session helper request was invalid",
                "data": None,
            }
        ).encode("utf-8")
    return json.dumps(
        {
            "status": payload.status.value,
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "data": payload.data,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _prepare_state_dir(state_dir: Path, platform_name: str) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _is_windows(platform_name):
        state_dir.chmod(0o700)


def _publish_discovery(state_dir: Path, document: dict[str, JSONValue], platform: str) -> None:
    target = state_dir / DISCOVERY_FILE
    temporary = state_dir / f".{DISCOVERY_FILE}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream)
        stream.flush()
        os.fsync(stream.fileno())
    if not _is_windows(platform):
        temporary.chmod(0o600)
    os.replace(temporary, target)


def _remove_own_discovery(state_dir: Path, authkey_hex: str) -> None:
    target = state_dir / DISCOVERY_FILE
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(current, dict) and current.get("authkey") == authkey_hex:
        target.unlink(missing_ok=True)


def _serve_connection(conn: Connection) -> None:
    if not conn.poll(_REQUEST_WAIT_SEC):
        return
    conn.send_bytes(handle_request(conn.recv_bytes(_MAX_REQUEST_BYTES)))


def run_helper(
    *,
    stop: threading.Event | None = None,
    platform_name: str | None = None,
    state_dir: Path | None = None,
    ready: Callable[[], None] | None = None,
) -> int:
    """Serve capture requests for the system service until stopped."""
    platform = sys.platform if platform_name is None else platform_name
    directory = user_state_dir(platform) if state_dir is None else state_dir
    stop_event = threading.Event() if stop is None else stop
    _prepare_state_dir(directory, platform)
    authkey = secrets.token_bytes(32)
    if _is_windows(platform):
        family = "AF_PIPE"
        address = PIPE_PREFIX + secrets.token_hex(16)
    else:
        family = "AF_UNIX"
        socket_path = directory / SOCKET_FILE
        if socket_path.is_socket():
            socket_path.unlink()
        address = str(socket_path)

    with Listener(address, family, authkey=authkey) as listener:
        if not _is_windows(platform):
            os.chmod(address, 0o600)
        _publish_discovery(
            directory,
            {
                "version": _PROTOCOL_VERSION,
                "family": family,
                "address": address,
                "authkey": authkey.hex(),
                "pid": os.getpid(),
            },
            platform,
        )
        if ready is not None:
            ready()
        try:
            while not stop_event.is_set():
                try:
                    conn = listener.accept()
                except (AuthenticationError, OSError, EOFError) as error:
                    logger.debug("session helper rejected peer error=%s", type(error).__name__)
                    continue
                with conn:
                    try:
                        _serve_connection(conn)
                    except (OSError, EOFError) as error:
                        logger.debug("session helper request failed error=%s", type(error).__name__)
        finally:
            _remove_own_discovery(directory, authkey.hex())
    return 0


RequestCapture = Callable[..., CollectorPayload | None]


class SessionScreenshotCollector:
    """Service-side screenshot collector that asks the user-session helper to capture."""

    name = SCREENSHOT

    def __init__(
        self,
        enabled: bool,
        max_bytes: int,
        timeout_sec: float,
        *,
        request: RequestCapture = request_capture,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self._enabled = enabled
        self._max_bytes = max_bytes
        self._timeout_sec = timeout_sec
        self._request = request

    def collect(self) -> CollectorPayload:
        if not self._enabled:
            return CollectorPayload(data={SCREENSHOT: None}, status=CollectorStatus.DISABLED)
        payload = self._request(
            SCREENSHOT, max_bytes=self._max_bytes, timeout_sec=self._timeout_sec
        )
        if payload is None:
            return CollectorPayload(
                data={SCREENSHOT: None},
                status=CollectorStatus.PARTIAL,
                error_code="session_helper_unavailable",
                error_message="no user-session helper responded",
            )
        return payload


class SessionActiveWindowCollector:
    """Service-side active-window collector that asks the user-session helper."""

    name = ACTIVE_WINDOW

    def __init__(
        self,
        enabled: bool,
        timeout_sec: float,
        *,
        request: RequestCapture = request_capture,
    ) -> None:
        self._enabled = enabled
        self._timeout_sec = timeout_sec
        self._request = request

    def collect(self) -> CollectorPayload:
        empty: dict[str, JSONValue] = {"title": None, "app": None, "pid": None}
        if not self._enabled:
            return CollectorPayload(data={ACTIVE_WINDOW: empty}, status=CollectorStatus.DISABLED)
        payload = self._request(ACTIVE_WINDOW, max_bytes=1, timeout_sec=self._timeout_sec)
        if payload is None:
            return CollectorPayload(
                data={ACTIVE_WINDOW: empty},
                status=CollectorStatus.PARTIAL,
                error_code="session_helper_unavailable",
                error_message="no user-session helper responded",
            )
        return payload
