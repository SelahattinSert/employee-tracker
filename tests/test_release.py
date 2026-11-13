from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKE_SELFEXTRACT = ROOT / "scripts" / "make_selfextract.sh"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _make_archive(path: Path, launcher: str, marker_line: str) -> None:
    top = "MonitorAgent-Linux-2.0.0"
    launcher_script = (
        "#!/bin/sh\n"
        f'printf "%s args=[%s]\\n" "{marker_line}" "$*" > "$MARKER"\n'
        'printf "deploy=%s\\n" "$([ -d "$(dirname "$0")/deploy" ] && echo yes || echo no)" '
        '>> "$MARKER"\n'
    )
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(f"{top}/{launcher}")
        data = launcher_script.encode()
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
        marker = tarfile.TarInfo(f"{top}/deploy/x.txt")
        payload = b"payload\n"
        marker.size = len(payload)
        archive.addfile(marker, io.BytesIO(payload))


@pytest.mark.skipif(os.name == "nt", reason="POSIX self-extractor is executed on Unix only")
def test_selfextract_runs_launcher_with_args(tmp_path: Path) -> None:
    archive = tmp_path / "pkg.tar.gz"
    _make_archive(archive, "Install.sh", "LAUNCHED")
    run = tmp_path / "MonitorAgent-Linux-2.0.0.run"
    result = subprocess.run(
        ["sh", str(MAKE_SELFEXTRACT), str(archive), "Install.sh", str(run)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert run.exists()
    assert run.stat().st_mode & 0o111
    assert result.stdout.strip() == str(run)

    marker = tmp_path / "marker.txt"
    execution = subprocess.run(
        ["sh", str(run), "--server-url", "https://host", "--no-employees-notified"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "MARKER": str(marker)},
    )
    assert execution.returncode == 0
    body = marker.read_text()
    assert "LAUNCHED args=[--server-url https://host --no-employees-notified]" in body
    assert "deploy=yes" in body


@pytest.mark.skipif(os.name == "nt", reason="POSIX self-extractor is executed on Unix only")
def test_selfextract_rejects_bad_arguments(tmp_path: Path) -> None:
    archive = tmp_path / "pkg.tar.gz"
    _make_archive(archive, "Install.sh", "X")
    # Missing output argument.
    missing = subprocess.run(
        ["sh", str(MAKE_SELFEXTRACT), str(archive), "Install.sh"],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    # Launcher with a path separator is refused.
    bad_launcher = subprocess.run(
        ["sh", str(MAKE_SELFEXTRACT), str(archive), "sub/Install.sh", str(tmp_path / "o.run")],
        capture_output=True,
        text=True,
    )
    assert bad_launcher.returncode == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX self-extractor is executed on Unix only")
def test_selfextract_detects_incomplete_payload(tmp_path: Path) -> None:
    archive = tmp_path / "pkg.tar.gz"
    _make_archive(archive, "Install.sh", "X")
    run = tmp_path / "out.run"
    subprocess.run(
        ["sh", str(MAKE_SELFEXTRACT), str(archive), "Missing.sh", str(run)],
        check=True,
        capture_output=True,
    )
    # The launcher named at build time is absent from the payload, so running must fail.
    execution = subprocess.run(["sh", str(run)], capture_output=True, text=True)
    assert execution.returncode != 0
    assert "incomplete" in execution.stderr


def test_release_workflow_publishes_one_file_installer_per_os() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert 'tags:\n      - "v*"' in workflow
    assert "permissions:\n  contents: write" in workflow
    # One single-file installer per OS, each uploaded to the release.
    assert "runs-on: ubuntu-24.04" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "runs-on: macos-15" in workflow
    assert "scripts/make_selfextract.sh" in workflow
    assert "7z.sfx" in workflow
    assert "hdiutil create" in workflow
    assert workflow.count("gh release upload") == 3
    assert "gh release create" in workflow
    # Actions are pinned to a commit SHA.
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405" in workflow


def test_release_installers_ship_no_secret() -> None:
    # The self-extractor stub embeds only the launcher name, never a token or URL.
    stub = MAKE_SELFEXTRACT.read_text(encoding="utf-8")
    assert "mat_" not in stub
    assert "MONITOR_API_TOKEN" not in stub
    assert "MONITOR_COLLECTOR_URI" not in stub
