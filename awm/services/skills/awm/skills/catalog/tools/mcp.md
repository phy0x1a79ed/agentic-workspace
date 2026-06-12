---
name: mcp
tags: [mcp, integration, tools, setup]
requires: []
description: Register an existing MCP server in the workspace .mcp.json
---

# Add MCP Server

Register an MCP server that already exists as a runnable command. Out of scope: writing a new server, security-vetting untrusted code, or reviewing the server's implementation quality.

## Steps

1. **Identify the entry point.** Find the command that starts the server (console script, `python -m …`, `npx …`). Note the transport (stdio is the default) and any required env vars or CLI args — library paths, API keys, workspace root, etc. The project's own README or setup.py / package.json usually has this.

2. **Skim the tool surface.** Read the server's tool list (source file or docs) so you know what you're signing up for. Confirm you're not reusing an existing server-name key in `.mcp.json` — Claude Code exposes tools as `mcp__<server-name>__<tool>`, so unique keys prevent collisions automatically.

3. **Register in `.mcp.json`.** Add an entry under `mcpServers` with `command`, `args`, and (if needed) `env`. Match existing conventions:
   - Python servers installed in a conda env use the `mamba run -n <env> <command>` wrapper.
   - Node servers use `npx -y <package>`.
   - Keep secrets out of the file; reference them through env vars.

   Example shape (see `/home/tony/agentic_workspace/.mcp.json` for live examples):
   ```json
   {
     "mcpServers": {
       "myserver": {
         "command": "mamba",
         "args": ["run", "-n", "myenv", "myserver-mcp"],
         "env": { "MYSERVER_CONFIG": "/path/to/config" }
       }
     }
   }
   ```

4. **Verify (automated, host-independent).** Probe the server directly over stdio using the exact `command`/`args`/`env` you just wrote. Do not rely on restarting the host agent. Send an MCP `initialize` → `notifications/initialized` → `tools/list` handshake and confirm the response contains a non-empty `tools` array:

   ```bash
   printf '%s\n%s\n%s\n' \
     '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}' \
     '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
     '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
     | <command> <args...>
   ```

   A successful response means the server launches under its configured env, completes the MCP handshake, and enumerates tools. That's everything a host needs from it.
