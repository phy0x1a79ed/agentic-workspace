"""MCP-config export framework — public API."""

from awm.gateway.exports.mcp import EXPORTERS, MCPExporter, register, sync_mcp_configs

from awm.gateway.exports import backends  # noqa: F401  triggers backend registration

__all__ = ["EXPORTERS", "MCPExporter", "register", "sync_mcp_configs"]
