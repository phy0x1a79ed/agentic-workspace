// Minimal Node backend for the hello stripe. The hub injects
// AWM_SERVICE_PORT before spawn; we bind there and serve /healthz +
// /echo. All responses include the bound port so the frontend can
// confirm it really is talking to the supervised process.

import { createServer } from "node:http";
import process from "node:process";

const port = Number.parseInt(process.env.AWM_SERVICE_PORT ?? "", 10);
if (!Number.isFinite(port)) {
  console.error("AWM_SERVICE_PORT missing or not an int — refusing to start");
  process.exit(1);
}

const server = createServer((req, res) => {
  const url = new URL(req.url ?? "/", `http://127.0.0.1:${port}`);
  if (url.pathname === "/healthz") {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("ok");
    return;
  }
  if (url.pathname === "/echo") {
    const msg = url.searchParams.get("msg") ?? "hi";
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ echoed: msg, port, pid: process.pid }));
    return;
  }
  res.writeHead(404, { "content-type": "text/plain" });
  res.end("not found");
});

server.listen(port, "127.0.0.1", () => {
  console.error(`[hello] backend listening on 127.0.0.1:${port} pid=${process.pid}`);
});

// SIGTERM is what the hub sends on lease close. Default Node handler
// already exits on it; the explicit close call drains in-flight
// requests rather than ripping them mid-write.
process.on("SIGTERM", () => {
  server.close(() => process.exit(0));
});
