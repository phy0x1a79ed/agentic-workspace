"""Tiny stdio MCP server exposing a single tool: show().

Side channel that lets the voice agent surface visual-only content
(code, paths, URLs) without sending it through TTS. The tool body is
a no-op; the useful signal is the tool_use event observed upstream of
MCP in the claude stream-json output.
"""

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("show")


@mcp.tool()
def show(content: str, kind: str = "text") -> str:
    """Display content on the user's screen WITHOUT speaking it aloud.

    Args:
        content: The text to display. May be multi-line.
        kind: One of "code" | "link" | "path" | "text".

    Returns:
        A short confirmation string (not spoken).
    """
    return "shown"


if __name__ == "__main__":
    mcp.run()
