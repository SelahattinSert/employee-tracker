"""Build the Windows installation zip: Install.cmd, deployment scripts, lock file and wheel.

The package never contains a collector URL, API token or agent configuration; setup.ps1
asks for them at install time. A private CA certificate (public data) may be bundled with
--ca so agents can verify a server certificate issued by that CA.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "deploy" / "windows"
DEPLOY_FILES = (
    "install.ps1",
    "uninstall.ps1",
    "run-agent.ps1",
    "setup.ps1",
    "monitor-agent.env.example",
    "monitor_agent_task.xml",
    "monitor_agent_session_task.xml",
)
ROOT_LAUNCHERS = ("Install.cmd", "Uninstall.cmd")
_PEM_HEADER = b"-----BEGIN CERTIFICATE-----"
_PRIVATE_KEY_MARKER = b"PRIVATE KEY-----"


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


def build_package(output_dir: Path, *, wheel: Path | None = None, ca: Path | None = None) -> Path:
    version = _version()
    ca_data = _check_ca(ca) if ca is not None else None
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"MonitorAgent-Windows-{version}.zip"
    prefix = f"MonitorAgent-Windows-{version}/"
    with tempfile.TemporaryDirectory() as temporary:
        wheel_path = wheel if wheel is not None else _build_wheel(Path(temporary))
        if not wheel_path.name.startswith("monitor_agent-") or wheel_path.suffix != ".whl":
            raise PackageError("the wheel must be a monitor_agent .whl file")
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ROOT_LAUNCHERS:
                archive.write(WINDOWS / name, prefix + name)
            for name in DEPLOY_FILES:
                archive.write(WINDOWS / name, prefix + "deploy/windows/" + name)
            archive.write(ROOT / "requirements.lock", prefix + "requirements.lock")
            archive.write(wheel_path, prefix + "dist/" + wheel_path.name)
            if ca_data is not None:
                archive.writestr(prefix + "ca.crt", ca_data)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--wheel", type=Path, help="use an existing wheel instead of building")
    parser.add_argument("--ca", type=Path, help="private CA certificate (PEM) to bundle")
    args = parser.parse_args(argv)
    try:
        target = build_package(args.output, wheel=args.wheel, ca=args.ca)
    except PackageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
