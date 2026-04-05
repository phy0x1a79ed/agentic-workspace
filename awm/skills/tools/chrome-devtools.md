---
name: Chrome DevTools MCP
type: tool
tags: [chrome, devtools, mcp, browser]
description: Chrome DevTools MCP server for browser control from WSL2
---

# Chrome DevTools MCP

## Connection

SSE transport connecting to Chrome's remote debugging protocol on Windows host.

```json
{
  "type": "sse",
  "url": "http://{windowsIP}:19222"
}
```

Replace `{windowsIP}` with your Windows host IP. Find it with:
```bash
cat /etc/resolv.conf | grep nameserver | awk '{print $2}'
```

**Chrome launch flags (Windows):**
```
chrome.exe --remote-debugging-port=9222
```

## Key Tools

### Navigation
- `navigate(url)` — open URL in browser
- `get_current_url` — current page URL
- `reload` — refresh the page

### Screenshots
- `screenshot` — capture current viewport
- `screenshot({ selector })` — capture specific element

### Console
- `evaluate(expression)` — run JS in page context
- `get_console_logs` — recent console output

### DOM
- `get_html(selector)` — get element HTML
- `click(selector)` — click an element

## WSL2 Usage

Navigate the browser to the Vite dev server:
```
navigate("http://localhost:5173")
```

Or use the WSL2 IP if localhost forwarding isn't configured:
```bash
# Get WSL2 IP
hostname -I | awk '{print $1}'
```
