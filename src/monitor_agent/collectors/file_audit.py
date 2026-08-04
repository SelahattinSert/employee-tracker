from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from monitor_agent.models import CollectorPayload, JSONValue


class FileAuditCollector:
    name = "file_audit"

    def collect(self) -> CollectorPayload:
        audit_paths_env = os.environ.get("MONITOR_AUDIT_PATHS", "")
        if not audit_paths_env:
            return CollectorPayload(data={"file_audit": []})

        records: list[JSONValue] = []
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
                        records.append(
                            {
                                "path": str(f),
                                "size_bytes": st.st_size,
                                "modified": datetime.fromtimestamp(
                                    st.st_mtime, tz=UTC
                                ).isoformat(),
                                "sha256": sha.hexdigest(),
                            }
                        )
            except Exception:
                continue
        return CollectorPayload(data={"file_audit": records})
