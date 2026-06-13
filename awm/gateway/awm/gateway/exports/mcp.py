"""MCP-config export framework.

Reads the canonical workspace ``.mcp.json`` (Claude Code's MCP registry) and
fans it out to backend-specific config files for other CLI agents that don't
read ``.mcp.json`` natively (opencode, codex, …).

The framework is intentionally protocol-agnostic: each exporter is a plain
module exposing ``name: str`` and ``export(canonical: dict) -> (Path, str)``.
The framework writes whatever bytes the exporter returns to whatever path it
chooses. Schema translation lives entirely in the exporter.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Protocol

from awm.config import WORKSPACE_ROOT


class MCPExporter(Protocol):
    name: str

    def export(self, canonical: dict) -> tuple[Path, str]: ...


EXPORTERS: list[MCPExporter] = []


def register(exporter: MCPExporter) -> MCPExporter:
    """Register an exporter. Safe to call repeatedly (deduped by name)."""
    for existing in EXPORTERS:
        if existing.name == exporter.name:
            EXPORTERS.remove(existing)
            break
    EXPORTERS.append(exporter)
    return exporter


def _atomic_write(path: Path, content: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = content.encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return len(data)


def sync_mcp_configs(workspace_root: Path | None = None) -> list[dict]:
    """Read .mcp.json and run every registered exporter against it.

    Returns a per-exporter report. One bad backend never blocks others — each
    exporter's exception is caught and surfaced in its report entry.
    """
    root = Path(workspace_root) if workspace_root else WORKSPACE_ROOT
    mcp_json = root / ".mcp.json"

    if not mcp_json.exists():
        return [{"ok": False, "error": f"no .mcp.json at {mcp_json}"}]

    try:
        canonical = json.loads(mcp_json.read_text())
    except json.JSONDecodeError as e:
        return [{"ok": False, "error": f"invalid .mcp.json: {e}"}]

    report: list[dict] = []
    for exporter in EXPORTERS:
        entry: dict = {"name": exporter.name}
        try:
            out_path, content = exporter.export(canonical)
            bytes_written = _atomic_write(out_path, content)
            entry.update(ok=True, path=str(out_path), bytes=bytes_written)
        except Exception as e:
            entry.update(ok=False, error=f"{type(e).__name__}: {e}")
        report.append(entry)
    return report
