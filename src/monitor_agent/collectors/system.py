from __future__ import annotations

import platform
import socket
import sys
import time
from datetime import UTC, datetime
from typing import cast

import psutil  # type: ignore[import-untyped]

from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CollectorPayload, JSONValue


class SystemCollector:
    name = "system"

    def __init__(self, machine_identity: MachineIdentity) -> None:
        self._machine_id = machine_identity.value

    def collect(self) -> CollectorPayload:
        uname = platform.uname()
        boot_timestamp = cast(float, psutil.boot_time())
        system: dict[str, JSONValue] = {
            "hostname": socket.getfqdn(),
            "machine_id": self._machine_id,
            "os": uname.system,
            "os_release": uname.release,
            "os_version": uname.version,
            "architecture": uname.machine,
            "processor": uname.processor,
            "python": sys.version,
            "boot_time": datetime.fromtimestamp(
                boot_timestamp,
                tz=UTC,
            ).isoformat(),
            "uptime_sec": max(0, int(time.time() - boot_timestamp)),
        }
        data: dict[str, JSONValue] = {"system": system}
        return CollectorPayload(data=data)
