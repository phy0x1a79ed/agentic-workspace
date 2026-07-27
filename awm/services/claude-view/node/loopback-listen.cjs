// Force every server this Node process opens onto the loopback interface.
//
// claude-view's Rust server spawns a Node sidecar for its CLI-control bridge
// (`/api/sidecar/*`, `/ws/chat/*`). The sidecar's bundle calls
// `.listen(SIDECAR_PORT)` with no host argument, which makes Node bind every
// interface — and it reads no host or bind variable, so there is no knob to
// turn. That leaves an unauthenticated control plane for spawning and driving
// Claude Code sessions listening on the LAN.
//
// It is not mesh-reachable today, but only because the Windows portproxy
// happens to forward 12100..12150 and the sidecar sits on 3001. That is
// reachability decided by an unrelated config file, which is not a security
// boundary. This makes the loopback bind explicit instead.
//
// Preloaded via NODE_OPTIONS=--require, so it runs before the bundle is
// evaluated and needs no change to the vendored release asset — which is the
// official prebuilt sidecar we checksum against upstream's checksums.txt, and
// which we would otherwise have to re-patch on every version bump.
//
// Patching `net` rather than `http`: http.Server extends net.Server and
// delegates listen() to it, so this one seam covers HTTP, HTTPS and raw TCP.
// A CommonJS preload reaches the same module instance the ESM bundle imports.

'use strict';

const net = require('net');

const LOOPBACK = '127.0.0.1';
const original = net.Server.prototype.listen;

net.Server.prototype.listen = function listen(...args) {
  // listen() is heavily overloaded. The only forms that can bind a wildcard
  // are (port[, host][, backlog][, cb]) and (options[, cb]) — a path (unix
  // socket), a handle, or an options object carrying `path`/`fd` never opens
  // a TCP port, so they are passed through untouched.
  if (typeof args[0] === 'number' || typeof args[0] === 'string') {
    const port = args[0];
    const rest = args.slice(1);
    // A host is present only if the next argument is a string. Anything else
    // there is a backlog or a callback, so the host slot is empty and Node
    // would default to the wildcard.
    if (typeof rest[0] === 'string') {
      rest[0] = LOOPBACK;
      return original.call(this, port, ...rest);
    }
    return original.call(this, port, LOOPBACK, ...rest);
  }

  if (args[0] && typeof args[0] === 'object' && args[0].path === undefined
      && args[0].fd === undefined && args[0].port !== undefined) {
    const opts = { ...args[0], host: LOOPBACK };
    return original.call(this, opts, ...args.slice(1));
  }

  return original.apply(this, args);
};
