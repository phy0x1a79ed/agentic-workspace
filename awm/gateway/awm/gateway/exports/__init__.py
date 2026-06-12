"""MCP-config export framework — public API."""

from awm.exports.mcp import EXPORTERS, MCPExporter, register, sync_mcp_configs

from awm.exports import backends  # noqa: F401  triggers backend registration

__all__ = ["EXPORTERS", "MCPExporter", "register", "sync_mcp_configs"]
