"""Host telemetry collector contracts and implementations."""

from __future__ import annotations

from monitor_agent.collectors.base import Collector
from monitor_agent.collectors.file_audit import FileAuditCollector
from monitor_agent.collectors.network import NetworkCollector
from monitor_agent.collectors.processes import ProcessesCollector
from monitor_agent.collectors.resources import ResourceCollector
from monitor_agent.collectors.screenshot import ScreenshotCollector
from monitor_agent.collectors.software import SoftwareCollector
from monitor_agent.collectors.system import SystemCollector
from monitor_agent.collectors.users import UsersCollector
from monitor_agent.collectors.window import ActiveWindowCollector
from monitor_agent.config import AgentConfig
from monitor_agent.identity import MachineIdentity


def build_collectors(
    config: AgentConfig,
    identity: MachineIdentity,
) -> list[Collector]:
    return [
        SystemCollector(identity),
        UsersCollector(),
        ResourceCollector(),
        NetworkCollector(config.include_network_connections),
        ProcessesCollector(config.process_cmdline_mode),
        SoftwareCollector(config.include_software),
        ActiveWindowCollector(config.include_active_window),
        FileAuditCollector(
            config.audit_paths,
            config.audit_max_files,
            config.audit_max_file_bytes,
        ),
        ScreenshotCollector(
            config.screenshot_enabled,
            config.screenshot_max_bytes,
        ),
    ]
