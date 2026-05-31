"""Tiny stdio MCP server exposing a single tool: show().

Purpose: give the voice agent a side channel for visual-only content.
When the agent calls show(content=..., kind=...), the call surfaces in
the claude stream-json output as a tool_use event. The voice demo
server detects this specific tool and renders the content in the
transcript with appropriate styling, WITHOUT routing it through TTS.

The tool itself does nothing — its return value is irrelevant. The
useful signal is the tool_use event captured upstream of MCP.
"""

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("show")


@mcp.tool()
def show(content: str, kind: str = "text") -> str:
    """Display content on the user's screen WITHOUT speaking it aloud.

    Use this any time you want the user to *see* something the voice
    channel can't carry well: code snippets, file paths, command lines,
    URLs, exact identifiers, error messages, structured output.

    Args:
        content: The text to display. May be multi-line.
        kind: Hint to the renderer. One of:
            - "code"    — fixed-width block (for code, shell commands, output)
            - "link"    — clickable URL (content should be the URL)
            - "path"    — a filesystem path (rendered inline-monospace)
            - "text"    — generic visual aside (default)

    Returns:
        A short confirmation string. The return value is not spoken.
    """
    return "shown"


if __name__ == "__main__":
    mcp.run()
