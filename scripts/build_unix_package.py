"""Build the Linux or macOS installation archive: a one-click launcher, the deployment
scripts, the setup wizard, the lock file and the wheel.

The archive never contains a collector URL, API token or agent configuration; setup.sh asks
for them at install time. A private CA certificate (public data) may be bundled with --ca so
agents can verify a server certificate issued by that CA.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON_WIZARD = "deploy/common/setup-wizard.sh"
_PEM_HEADER = b"-----BEGIN CERTIFICATE-----"
_PRIVATE_KEY_MARKER = b"PRIVATE KEY-----"
_EXECUTABLE_SUFFIXES = (".sh", ".command")

# published path -> source path, relative to the repository root.
_OS_FILES: dict[str, dict[str, str]] = {
    "linux": {
        "Install.sh": "deploy/linux/Install.sh",
        "deploy/linux/install.sh": "deploy/linux/install.sh",
        "deploy/linux/uninstall.sh": "deploy/linux/uninstall.sh",
        "deploy/linux/setup.sh": "deploy/linux/setup.sh",
        "deploy/linux/monitor-agent.env.example": "deploy/linux/monitor-agent.env.example",
        "deploy/linux/monitor-agent.service": "deploy/linux/monitor-agent.service",
        "deploy/linux/monitor-agent-session.desktop": "deploy/linux/monitor-agent-session.desktop",
        COMMON_WIZARD: COMMON_WIZARD,
    },
    "macos": {
        "Install.command": "deploy/macos/Install.command",
        "deploy/macos/install.sh": "deploy/macos/install.sh",
        "deploy/macos/uninstall.sh": "deploy/macos/uninstall.sh",
        "deploy/macos/setup.sh": "deploy/macos/setup.sh",
        "deploy/macos/run-agent.sh": "deploy/macos/run-agent.sh",
        "deploy/macos/monitor-agent.env.example": "deploy/macos/monitor-agent.env.example",
        "deploy/macos/com.company.monitor-agent.plist": (
            "deploy/macos/com.company.monitor-agent.plist"
        ),
        "deploy/macos/com.company.monitor-agent.session.plist": (
            "deploy/macos/com.company.monitor-agent.session.plist"
        ),
        COMMON_WIZARD: COMMON_WIZARD,
    },
}
_OS_LABEL = {"linux": "Linux", "macos": "macOS"}


class PackageError(ValueError):
    """Raised when the package inputs are unsafe or incomplete."""


def _version() -> str:
    text = (ROOT / "src" / "monitor_agent" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise PackageError("monitor_agent.__version__ not found")
    return match.group(1)


def _check_ca(path: Path) -> bytes:
    data = path.read_bytes()
    if _PEM_HEADER not in data:
        raise PackageError("the CA file must be a PEM certificate")
    if _PRIVATE_KEY_MARKER in data:
        raise PackageError("the CA file contains a private key; pass only the certificate")
    return data


def _build_wheel(output: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(output), str(ROOT)],
        check=True,
    )
    wheels = sorted(output.glob("monitor_agent-*.whl"))
    if len(wheels) != 1:
        raise PackageError("expected exactly one monitor_agent wheel")
    return wheels[0]


def _add(archive: tarfile.TarFile, source: Path, arcname: str, *, executable: bool) -> None:
    info = archive.gettarinfo(str(source), arcname=arcname)
    info.mode = 0o755 if executable else 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    with source.open("rb") as handle:
        archive.addfile(info, handle)


def build_package(
    os_name: str, output_dir: Path, *, wheel: Path | None = None, ca: Path | None = None
) -> Path:
    if os_name not in _OS_FILES:
        raise PackageError(f"unsupported OS: {os_name}")
    version = _version()
    ca_data = _check_ca(ca) if ca is not None else None
    output_dir.mkdir(parents=True, exist_ok=True)
    label = _OS_LABEL[os_name]
    prefix = f"MonitorAgent-{label}-{version}/"
    target = output_dir / f"MonitorAgent-{label}-{version}.tar.gz"
    with tempfile.TemporaryDirectory() as temporary:
        wheel_path = wheel if wheel is not None else _build_wheel(Path(temporary))
        if not wheel_path.name.startswith("monitor_agent-") or wheel_path.suffix != ".whl":
            raise PackageError("the wheel must be a monitor_agent .whl file")
        with tarfile.open(target, "w:gz") as archive:
            for published, source in _OS_FILES[os_name].items():
                _add(
                    archive,
                    ROOT / source,
                    prefix + published,
                    executable=(
                        published.endswith(_EXECUTABLE_SUFFIXES) and published != COMMON_WIZARD
                    ),
                )
            _add(
                archive, ROOT / "requirements.lock", prefix + "requirements.lock", executable=False
            )
            _add(archive, wheel_path, prefix + "dist/" + wheel_path.name, executable=False)
            if ca_data is not None:
                info = tarfile.TarInfo(prefix + "ca.crt")
                info.size = len(ca_data)
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                archive.addfile(info, io.BytesIO(ca_data))
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--os", required=True, choices=sorted(_OS_FILES))
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--wheel", type=Path, help="use an existing wheel instead of building")
    parser.add_argument("--ca", type=Path, help="private CA certificate (PEM) to bundle")
    args = parser.parse_args(argv)
    try:
        target = build_package(args.os, args.output, wheel=args.wheel, ca=args.ca)
    except PackageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
