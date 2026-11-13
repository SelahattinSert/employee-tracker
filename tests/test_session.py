from __future__ import annotations

import base64
import json
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import monitor_agent.session as session
from monitor_agent.collectors import build_collectors
from monitor_agent.config import ConfigError, load_config
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CollectorPayload, CollectorStatus
from monitor_agent.session import (
    SessionActiveWindowCollector,
    SessionLocation,
    SessionScreenshotCollector,
    handle_request,
    parse_response,
    read_endpoint,
    request_capture,
    run_helper,
    session_locations,
    user_state_dir,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses Unix sockets")
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


def screenshot_response(data: bytes = PNG, size: int | None = None) -> bytes:
    return json.dumps(
        {
            "status": "success",
            "error_code": None,
            "error_message": None,
            "data": {
                "screenshot": {
                    "captured_at": "2099-07-20T12:00:00+00:00",
                    "format": "image/png",
                    "size_bytes": len(data) if size is None else size,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                }
            },
        }
    ).encode()


class FakeScreenshot:
    def __init__(self, enabled: bool, max_bytes: int) -> None:
        self.max_bytes = max_bytes

    def collect(self) -> CollectorPayload:
        return CollectorPayload(
            data={
                "screenshot": {
                    "captured_at": "2099-07-20T12:00:00+00:00",
                    "format": "image/png",
                    "size_bytes": len(PNG),
                    "data_b64": base64.b64encode(PNG).decode("ascii"),
                }
            }
        )


class FakeWindow:
    def __init__(self, enabled: bool) -> None:
        pass

    def collect(self) -> CollectorPayload:
        return CollectorPayload(data={"active_window": {"title": "Report", "app": "x", "pid": 7}})


@pytest.fixture
def helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionLocation]:
    monkeypatch.setattr(session, "ScreenshotCollector", FakeScreenshot)
    monkeypatch.setattr(session, "ActiveWindowCollector", FakeWindow)
    # AF_UNIX paths are limited to about 100 bytes.
    state_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"ma-test-{os.getpid()}"
    stop = threading.Event()
    ready = threading.Event()
    thread = threading.Thread(
        target=run_helper,
        kwargs={"stop": stop, "state_dir": state_dir, "ready": ready.set},
        daemon=True,
    )
    thread.start()
    assert ready.wait(timeout=5)
    location = SessionLocation(state_dir, os.getuid())
    try:
        yield location
    finally:
        stop.set()
        request_capture("active_window", max_bytes=1, timeout_sec=1, locations=lambda: [location])
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not (state_dir / "helper.json").exists()
        state_dir.rmdir()


@posix_only
def test_service_receives_screenshot_from_user_session_helper(helper: SessionLocation) -> None:
    payload = request_capture(
        "screenshot", max_bytes=1024, timeout_sec=5, locations=lambda: [helper]
    )

    assert payload is not None
    assert payload.status is CollectorStatus.SUCCESS
    assert payload.data == FakeScreenshot(True, 1024).collect().data


@posix_only
def test_service_receives_active_window_from_user_session_helper(helper: SessionLocation) -> None:
    collector = SessionActiveWindowCollector(
        True, 5, request=lambda *a, **k: request_capture(*a, **k, locations=lambda: [helper])
    )

    payload = collector.collect()

    assert payload.status is CollectorStatus.SUCCESS
    assert payload.data == {"active_window": {"title": "Report", "app": "x", "pid": 7}}


@posix_only
def test_helper_files_are_owner_only(helper: SessionLocation) -> None:
    assert (helper.state_dir.stat().st_mode & 0o777) == 0o700
    assert ((helper.state_dir / "helper.json").stat().st_mode & 0o777) == 0o600
    assert ((helper.state_dir / "helper.sock").stat().st_mode & 0o777) == 0o600


@posix_only
def test_wrong_authentication_key_gets_no_capture(helper: SessionLocation) -> None:
    discovery = helper.state_dir / "helper.json"
    original = discovery.read_text(encoding="utf-8")
    forged = json.loads(original) | {"authkey": "0" * 64}
    discovery.write_text(json.dumps(forged), encoding="utf-8")
    try:
        payload = request_capture(
            "screenshot", max_bytes=1024, timeout_sec=1, locations=lambda: [helper]
        )
    finally:
        discovery.write_text(original, encoding="utf-8")

    assert payload is None


@posix_only
def test_discovery_owned_by_another_user_is_rejected(helper: SessionLocation) -> None:
    other = SessionLocation(helper.state_dir, os.getuid() + 1)

    with pytest.raises(ValueError, match="not private"):
        read_endpoint(other)


@posix_only
def test_discovery_symlink_is_rejected(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    (state_dir / "helper.json").symlink_to(target)

    with pytest.raises(ValueError, match="not private"):
        read_endpoint(SessionLocation(state_dir, os.getuid()))


@posix_only
def test_discovery_address_outside_state_dir_is_rejected(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    discovery = state_dir / "helper.json"
    discovery.write_text(
        json.dumps(
            {"version": 1, "family": "AF_UNIX", "address": "/tmp/x.sock", "authkey": "a" * 64}
        ),
        encoding="utf-8",
    )
    discovery.chmod(0o600)

    with pytest.raises(ValueError, match="invalid helper address"):
        read_endpoint(SessionLocation(state_dir, os.getuid()))


def test_windows_discovery_requires_monitor_agent_pipe(tmp_path: Path) -> None:
    discovery = tmp_path / "helper.json"
    document = {"version": 1, "family": "AF_PIPE", "authkey": "a" * 64}
    discovery.write_text(json.dumps(document | {"address": "\\\\.\\pipe\\other"}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid helper address"):
        read_endpoint(SessionLocation(tmp_path, None), "win32")

    address = "\\\\.\\pipe\\monitor-agent-" + "b" * 32
    discovery.write_text(json.dumps(document | {"address": address}), encoding="utf-8")
    endpoint = read_endpoint(SessionLocation(tmp_path, None), "win32")
    assert endpoint.address == address
    assert endpoint.authkey == bytes.fromhex("a" * 64)


def test_parse_response_accepts_bounded_png() -> None:
    payload = parse_response(screenshot_response(), "screenshot", 1024)

    assert payload.status is CollectorStatus.SUCCESS
    assert payload.data["screenshot"]["size_bytes"] == len(PNG)  # type: ignore[index,call-overload]


@pytest.mark.parametrize(
    "raw",
    [
        screenshot_response(b"not a png"),
        screenshot_response(size=len(PNG) + 1),
        screenshot_response(PNG * 200),
        b"[]",
        json.dumps({"status": "bogus", "data": {"screenshot": None}}).encode(),
        json.dumps({"status": "success", "data": {"screenshot": None, "extra": 1}}).encode(),
    ],
)
def test_parse_response_rejects_untrusted_screenshot(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_response(raw, "screenshot", 1024)


@pytest.mark.parametrize(
    "window",
    [
        {"title": "x" * 5000, "app": None, "pid": None},
        {"title": None, "app": None, "pid": -1},
        {"title": None, "app": None, "pid": True},
        {"title": None, "app": None},
    ],
)
def test_parse_response_rejects_untrusted_window(window: object) -> None:
    raw = json.dumps({"status": "success", "data": {"active_window": window}}).encode()

    with pytest.raises(ValueError):
        parse_response(raw, "active_window", 1)


def test_handle_request_uses_existing_collectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "ScreenshotCollector", FakeScreenshot)
    monkeypatch.setattr(session, "ActiveWindowCollector", FakeWindow)

    shot = json.loads(handle_request(b'{"collector": "screenshot", "max_bytes": 1024}'))
    window = json.loads(handle_request(b'{"collector": "active_window", "max_bytes": 1}'))

    assert shot["status"] == "success"
    assert shot["data"]["screenshot"]["format"] == "image/png"
    assert window["data"] == {"active_window": {"title": "Report", "app": "x", "pid": 7}}


@pytest.mark.parametrize(
    "raw",
    [b"not json", b'{"collector": "keystrokes"}', b'{"collector": "screenshot", "max_bytes": 0}'],
)
def test_handle_request_rejects_invalid_requests(raw: bytes) -> None:
    response = json.loads(handle_request(raw))

    assert response["status"] == "failed"
    assert response["error_code"] == "invalid_request"


def test_relay_collectors_report_disabled_and_missing_helper() -> None:
    def no_helper(*args: object, **kwargs: object) -> None:
        return None

    assert SessionScreenshotCollector(False, 10, 1, request=no_helper).collect() == (
        CollectorPayload(data={"screenshot": None}, status=CollectorStatus.DISABLED)
    )
    missing = SessionScreenshotCollector(True, 10, 1, request=no_helper).collect()
    assert missing.status is CollectorStatus.PARTIAL
    assert missing.error_code == "session_helper_unavailable"
    window = SessionActiveWindowCollector(True, 1, request=no_helper).collect()
    assert window.data == {"active_window": {"title": None, "app": None, "pid": None}}
    assert window.error_code == "session_helper_unavailable"
    assert SessionActiveWindowCollector(False, 1, request=no_helper).collect().status is (
        CollectorStatus.DISABLED
    )
    with pytest.raises(ValueError, match="max_bytes"):
        SessionScreenshotCollector(True, 0, 1)


def test_request_prefers_success_and_falls_back_to_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = CollectorPayload(data={"screenshot": None}, status=CollectorStatus.PARTIAL)
    monkeypatch.setattr(session, "read_endpoint", lambda location, platform: location)

    class FakeConnection:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def send_bytes(self, data: bytes) -> None:
            pass

        def poll(self, timeout: float) -> bool:
            return True

        def recv_bytes(self, limit: int) -> bytes:
            return self.raw

    responses = {
        "partial": json.dumps(
            {"status": "partial", "error_code": "x", "data": {"screenshot": None}}
        ).encode(),
        "success": screenshot_response(),
    }
    monkeypatch.setattr(
        session,
        "Client",
        lambda address, family, authkey: FakeConnection(responses[address]),
    )
    locations = [
        SimpleNamespace(address="partial", family="AF_UNIX", authkey=b""),
        SimpleNamespace(address="success", family="AF_UNIX", authkey=b""),
    ]

    best = request_capture(
        "screenshot",
        max_bytes=1024,
        timeout_sec=1,
        locations=lambda: locations,  # type: ignore[arg-type,return-value]
    )
    only_partial = request_capture(
        "screenshot",
        max_bytes=1024,
        timeout_sec=1,
        locations=lambda: locations[:1],  # type: ignore[arg-type,return-value]
    )

    assert best is not None and best.status is CollectorStatus.SUCCESS
    assert only_partial is not None and only_partial.status is partial.status


def test_user_state_dir_per_platform() -> None:
    assert user_state_dir("linux", {"XDG_RUNTIME_DIR": "/run/user/1000"}) == Path(
        "/run/user/1000/monitor-agent"
    )
    assert user_state_dir("win32", {"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"}) == (
        Path("C:\\Users\\a\\AppData\\Local") / "MonitorAgent"
    )
    assert user_state_dir("darwin", {}).parts[-4:] == (
        "Library",
        "Application Support",
        "MonitorAgent",
        "Session",
    )
    with pytest.raises(RuntimeError):
        user_state_dir("win32", {})


def test_session_locations_map_logged_in_users(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = SimpleNamespace(pw_uid=1000, pw_dir="/Users/ayse")

    class FakePwd:
        @staticmethod
        def getpwnam(name: str) -> SimpleNamespace:
            if name == "ghost":
                raise KeyError(name)
            return entry

    monkeypatch.setattr(session, "_password_module", lambda: FakePwd)
    names = lambda: ["ayse", "ghost", "../x"]  # noqa: E731

    assert session_locations("linux", names=names) == [
        SessionLocation(Path("/run/user/1000/monitor-agent"), 1000)
    ]
    assert session_locations("darwin", names=names) == [
        SessionLocation(Path("/Users/ayse/Library/Application Support/MonitorAgent/Session"), 1000)
    ]
    windows = session_locations("win32", names=lambda: ["ayse"], environ={"SystemDrive": "D:"})
    assert windows == [
        SessionLocation(
            Path("D:\\") / "Users" / "ayse" / "AppData" / "Local" / "MonitorAgent", None
        )
    ]


def test_logged_in_names_are_most_recent_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session.psutil,
        "users",
        lambda: [
            SimpleNamespace(name="old", started=1.0),
            SimpleNamespace(name="new", started=5.0),
            SimpleNamespace(name="new", started=4.0),
            SimpleNamespace(name=None, started=6.0),
        ],
    )

    assert session._logged_in_names() == ["new", "old"]


def config_env(**extra: str) -> dict[str, str]:
    return {
        "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
        "MONITOR_API_TOKEN": "token",
        **extra,
    }


def test_helper_session_source_selects_relay_collectors() -> None:
    identity = MachineIdentity("id", "test")
    direct = build_collectors(load_config(config_env(), platform_name="linux"), identity)
    relayed = build_collectors(
        load_config(config_env(MONITOR_SESSION_SOURCE="helper"), platform_name="linux"),
        identity,
    )

    assert [type(item).__name__ for item in direct][-3::2] == [
        "ActiveWindowCollector",
        "ScreenshotCollector",
    ]
    assert [type(item).__name__ for item in relayed][-3::2] == [
        "SessionActiveWindowCollector",
        "SessionScreenshotCollector",
    ]
    assert [item.name for item in direct] == [item.name for item in relayed]


def test_invalid_session_source_is_rejected() -> None:
    with pytest.raises(ConfigError, match="MONITOR_SESSION_SOURCE must be direct or helper"):
        load_config(config_env(MONITOR_SESSION_SOURCE="desktop"), platform_name="linux")
