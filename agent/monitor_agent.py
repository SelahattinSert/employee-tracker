# --- agent/monitor_agent.py ---
# Cross-platform employee monitoring agent
# Python 3.11+ | pip install psutil requests schedule
# Deploy via: systemd (Linux), launchd (macOS), NSSM/Task Scheduler (Windows)
# Requires: MONITOR_COLLECTOR_URI and MONITOR_API_TOKEN env vars

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import requests
import schedule

# ── Configuration ─────────────────────────────────────────────────────────────

COLLECTOR_URI  = os.environ.get("MONITOR_COLLECTOR_URI", "https://collector.internal/api/v1/telemetry")
API_TOKEN      = os.environ.get("MONITOR_API_TOKEN", "")
HEARTBEAT_SEC  = int(os.environ.get("MONITOR_HEARTBEAT_SEC", "300"))   # 5 min default
STARTUP_DELAY  = int(os.environ.get("MONITOR_STARTUP_DELAY_SEC", "30"))
LOG_PATH       = Path(os.environ.get("MONITOR_LOG_PATH", "/var/log/monitor_agent.log"
                      if sys.platform != "win32" else r"C:\ProgramData\Monitor\agent.log"))
MACHINE_ID     = str(uuid.UUID(hashlib.md5(socket.getfqdn().encode()).hexdigest()))

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("monitor_agent")

# ── Collectors ────────────────────────────────────────────────────────────────

def collect_system() -> dict[str, Any]:
    uname = platform.uname()
    boot  = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    return {
        "hostname":     socket.getfqdn(),
        "machine_id":   MACHINE_ID,
        "os":           uname.system,
        "os_release":   uname.release,
        "os_version":   uname.version,
        "architecture": uname.machine,
        "processor":    uname.processor,
        "python":       sys.version,
        "boot_time":    boot.isoformat(),
        "uptime_sec":   int(time.time() - psutil.boot_time()),
    }


def collect_users() -> list[dict[str, Any]]:
    users = []
    for u in psutil.users():
        users.append({
            "name":     u.name,
            "terminal": u.terminal,
            "host":     u.host,
            "started":  datetime.fromtimestamp(u.started, tz=timezone.utc).isoformat(),
            "pid":      u.pid,
        })
    return users


def collect_processes() -> list[dict[str, Any]]:
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "status",
                                   "cpu_percent", "memory_info", "exe",
                                   "cmdline", "create_time"]):
        try:
            info = p.info
            procs.append({
                "pid":        info["pid"],
                "name":       info["name"],
                "user":       info["username"],
                "status":     info["status"],
                "cpu_pct":    info["cpu_percent"],
                "mem_rss_kb": info["memory_info"].rss // 1024 if info["memory_info"] else 0,
                "exe":        info["exe"],
                "cmdline":    " ".join(info["cmdline"] or []),
                "started":    datetime.fromtimestamp(
                                  info["create_time"], tz=timezone.utc
                              ).isoformat() if info["create_time"] else None,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(procs, key=lambda x: x["mem_rss_kb"], reverse=True)[:100]


def collect_network() -> dict[str, Any]:
    adapters = []
    addrs    = psutil.net_if_addrs()
    stats    = psutil.net_if_stats()

    for iface, addr_list in addrs.items():
        st = stats.get(iface)
        if not st or not st.isup:
            continue
        ipv4 = next((a.address for a in addr_list if a.family == socket.AF_INET), None)
        mac  = next((a.address for a in addr_list
                     if a.family == psutil.AF_LINK), None)
        adapters.append({
            "interface": iface,
            "ipv4":      ipv4,
            "mac":       mac,
            "speed_mb":  st.speed,
            "mtu":       st.mtu,
        })

    connections = []
    for c in psutil.net_connections(kind="inet"):
        try:
            connections.append({
                "fd":     c.fd,
                "family": str(c.family),
                "type":   str(c.type),
                "laddr":  f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else None,
                "raddr":  f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else None,
                "status": c.status,
                "pid":    c.pid,
            })
        except Exception:
            continue

    counters = psutil.net_io_counters()
    return {
        "adapters":    adapters,
        "connections": connections,
        "io": {
            "bytes_sent":   counters.bytes_sent,
            "bytes_recv":   counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
            "errin":        counters.errin,
            "errout":       counters.errout,
        },
    }


def collect_cpu() -> dict[str, Any]:
    freq = psutil.cpu_freq()
    return {
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores":  psutil.cpu_count(logical=True),
        "percent_total":  psutil.cpu_percent(interval=1),
        "percent_per":    psutil.cpu_percent(interval=1, percpu=True),
        "freq_mhz":       freq.current if freq else None,
        "load_avg":       list(psutil.getloadavg()) if hasattr(psutil, "getloadavg") else None,
    }


def collect_memory() -> dict[str, Any]:
    vm  = psutil.virtual_memory()
    swp = psutil.swap_memory()
    return {
        "ram": {
            "total_kb":    vm.total     // 1024,
            "available_kb": vm.available // 1024,
            "used_kb":     vm.used      // 1024,
            "percent":     vm.percent,
        },
        "swap": {
            "total_kb":  swp.total  // 1024,
            "used_kb":   swp.used   // 1024,
            "percent":   swp.percent,
        },
    }


def collect_disks() -> list[dict[str, Any]]:
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device":       part.device,
                "mountpoint":   part.mountpoint,
                "fstype":       part.fstype,
                "total_gb":     round(usage.total  / 1024**3, 2),
                "used_gb":      round(usage.used   / 1024**3, 2),
                "free_gb":      round(usage.free   / 1024**3, 2),
                "percent":      usage.percent,
            })
        except (PermissionError, OSError):
            continue
    return disks


def collect_software() -> list[dict[str, Any]]:
    """
    Cross-platform installed software snapshot.
    Windows: registry uninstall keys
    Linux: dpkg or rpm
    macOS: /Applications + brew list
    """
    packages = []

    if sys.platform == "win32":
        import winreg
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, path in reg_paths:
            try:
                key = winreg.OpenKey(hive, path)
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                        name    = _reg_val(sub, "DisplayName")
                        version = _reg_val(sub, "DisplayVersion")
                        if name:
                            packages.append({"name": name, "version": version, "source": "windows_registry"})
                    except OSError:
                        continue
            except OSError:
                continue

    elif sys.platform == "linux":
        import subprocess
        for cmd, src in [
            (["dpkg-query", "-W", "-f=${Package}\t${Version}\n"], "dpkg"),
            (["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}\n"], "rpm"),
        ]:
            try:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
                for line in out.strip().splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        packages.append({"name": parts[0], "version": parts[1], "source": src})
                break
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue

    elif sys.platform == "darwin":
        import subprocess
        app_dir = Path("/Applications")
        for app in app_dir.glob("*.app"):
            packages.append({"name": app.stem, "version": None, "source": "macos_applications"})
        try:
            out = subprocess.check_output(["brew", "list", "--versions"],
                                          stderr=subprocess.DEVNULL, text=True)
            for line in out.strip().splitlines():
                parts = line.split(" ", 1)
                packages.append({"name": parts[0],
                                  "version": parts[1] if len(parts) > 1 else None,
                                  "source": "homebrew"})
        except (FileNotFoundError, Exception):
            pass

    return packages


def collect_active_window() -> dict[str, Any]:
    """
    Active foreground window title and application tracking.
    High-signal compliance alternative to raw keystroke logging.
    """
    win_info: dict[str, Any] = {"title": None, "app": None, "pid": None}
    try:
        if sys.platform == "linux":
            import subprocess
            out = subprocess.check_output(["xdotool", "getactivewindow", "getwindowname"],
                                           stderr=subprocess.DEVNULL, text=True).strip()
            win_info["title"] = out if out else None
        elif sys.platform == "win32":
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            win_info["title"] = buf.value if buf.value else None
        elif sys.platform == "darwin":
            import subprocess
            cmd = 'tell application "System Events" to get name of first process whose frontmost is true'
            out = subprocess.check_output(["osascript", "-e", cmd],
                                           stderr=subprocess.DEVNULL, text=True).strip()
            win_info["app"] = out if out else None
    except Exception:
        pass
    return win_info


def collect_file_audit() -> list[dict[str, Any]]:
    """
    File Integrity Monitoring (FIM) and content metadata collector for audit paths.
    Configured via MONITOR_AUDIT_PATHS environment variable (colon-separated).
    """
    audit_paths_env = os.environ.get("MONITOR_AUDIT_PATHS", "")
    if not audit_paths_env:
        return []
    
    records = []
    paths = [Path(p.strip()) for p in audit_paths_env.split(":") if p.strip()]
    for p in paths:
        if not p.exists():
            continue
        try:
            target_files = [p] if p.is_file() else list(p.rglob("*"))[:50]
            for f in target_files:
                if f.is_file():
                    st = f.stat()
                    sha = hashlib.sha256()
                    with open(f, "rb") as fh:
                        while chunk := fh.read(8192):
                            sha.update(chunk)
                    records.append({
                        "path": str(f),
                        "size_bytes": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        "sha256": sha.hexdigest(),
                    })
        except Exception as e:
            log.warning("File audit error on %s: %s", p, e)
    return records


def collect_screenshot() -> dict[str, Any] | None:
    """
    Desktop snapshot collector for visual audit trails.
    Enabled via MONITOR_SCREENSHOT_ENABLED=1.
    """
    if os.environ.get("MONITOR_SCREENSHOT_ENABLED", "0") != "1":
        return None
    try:
        import base64
        import io
        from PIL import ImageGrab
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return {
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
            "format": "image/png",
            "size_bytes": buf.tell(),
            "data_b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
        }
    except Exception as e:
        log.warning("Screenshot capture failed: %s", e)
        return None


# ── Payload assembly ──────────────────────────────────────────────────────────

def build_payload(event: str) -> dict[str, Any]:
    return {
        "schema_version": "1.1",
        "event":          event,
        "timestamp":      datetime.now(tz=timezone.utc).isoformat(),
        "machine_id":     MACHINE_ID,
        "system":         collect_system(),
        "users":          collect_users(),
        "cpu":            collect_cpu(),
        "memory":         collect_memory(),
        "disks":          collect_disks(),
        "network":        collect_network(),
        "processes":      collect_processes(),
        "software":       collect_software(),
        "active_window":  collect_active_window(),
        "file_audit":     collect_file_audit(),
        "screenshot":     collect_screenshot(),
    }


# ── Transport ─────────────────────────────────────────────────────────────────

def transmit(payload: dict[str, Any]) -> bool:
    if not API_TOKEN:
        log.warning("MONITOR_API_TOKEN not set — skipping transmit")
        return False

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {API_TOKEN}",
        "X-Machine-ID":  MACHINE_ID,
        "X-Event-Type":  payload["event"],
        "X-Schema-Ver":  payload["schema_version"],
    }

    try:
        resp = requests.post(
            COLLECTOR_URI,
            data=json.dumps(payload, default=str),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Telemetry sent [%s] → HTTP %d", payload["event"], resp.status_code)
        return True
    except requests.exceptions.Timeout:
        log.warning("Transmit timeout — collector unreachable")
    except requests.exceptions.ConnectionError as e:
        log.warning("Connection error: %s", e)
    except requests.exceptions.HTTPError as e:
        log.error("HTTP error %s: %s", resp.status_code, e)
    except Exception as e:
        log.error("Unexpected transmit error: %s", e)
    return False


# ── Scheduled jobs ────────────────────────────────────────────────────────────

def job_heartbeat() -> None:
    log.info("Heartbeat tick")
    transmit(build_payload("heartbeat"))


def job_startup() -> None:
    log.info("Startup event — sleeping %ds for network stabilization", STARTUP_DELAY)
    time.sleep(STARTUP_DELAY)
    transmit(build_payload("startup"))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Monitor agent starting — machine_id=%s", MACHINE_ID)

    job_startup()

    schedule.every(HEARTBEAT_SEC).seconds.do(job_heartbeat)
    log.info("Heartbeat scheduled every %ds", HEARTBEAT_SEC)

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()

