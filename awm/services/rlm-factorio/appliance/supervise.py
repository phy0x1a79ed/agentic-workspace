#!/usr/bin/env python3
"""Factorio appliance supervisor.

Owns the Factorio engine process inside the container and exposes a tiny HTTP
control surface (status / save / new / load). The engine loads exactly one world
per launch and has no in-game menu.

SACRED-SAVES MODEL. The live, ticking world and named saves are decoupled. The
engine ALWAYS runs on a private scratch file, ``_active.zip`` -- never on a named
save directly. A named save is therefore an immutable snapshot: nothing writes
``<name>.zip`` except an explicit ``save <name>``. This is the core invariant --
loading a save can never advance it, naming a save can never disturb another.

  * save <name> -> flush the live world to `_active` (`/server-save`, no restart),
                   then copy `_active` -> `<name>.zip`. Refuses to clobber an
                   existing name unless `overwrite` is set.
  * new [seed]  -> stop the engine, generate a fresh map straight into `_active`,
                   relaunch. Touches no named save.
  * load <name> -> stop the engine, copy `<name>.zip` -> `_active`, relaunch on
                   `_active`. The named save is read-only.

new/load cycle only the engine child process; the container (and this
supervisor) stay up the whole time. A connected Steam player drops to the menu
across a new/load and reconnects -- exactly as if the host clicked New/Load.
Switching worlds (new/load) discards unsaved progress in the live world, exactly
as the desktop UI does -- take a `save <name>` first to keep it.

This module is intentionally dependency-free (stdlib only) so the image stays
lean: Debian + Factorio headless + python3.
"""

import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- paths / config -------------------------------------------------------

FACTORIO_BIN = os.environ.get("FACTORIO_BIN", "/opt/factorio/bin/x64/factorio")
SAVES_DIR = os.environ.get("SAVES_DIR", "/factorio/saves")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/factorio/config")
MODS_DIR = os.environ.get("MODS_DIR", "/opt/factorio/mods")

GAME_PORT = int(os.environ.get("FACTORIO_PORT", "12140"))
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "12142"))

# RCON: the Lua transport for live perception + body control. Container-internal
# ONLY -- the supervisor reaches it on container-local loopback and the port is
# deliberately NOT published by docker-compose (security by non-exposure). The
# password is generated ONCE per supervisor process (not per engine re-exec, or
# reconnect after new/load would break) and reused across every engine launch.
RCON_PORT = int(os.environ.get("RCON_PORT", "12199"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD") or secrets.token_hex(16)

SERVER_SETTINGS = os.path.join(CONFIG_DIR, "server-settings.json")
MAP_GEN_SETTINGS = os.path.join(CONFIG_DIR, "map-gen-settings.json")

DEFAULT_WORLD = os.environ.get("DEFAULT_WORLD", "world")

# The private scratch file the engine always runs on. Named saves are immutable
# snapshots copied to/from this file; the engine never runs on a named save
# directly, so a named save is only ever written by an explicit `save <name>`.
ACTIVE = os.environ.get("ACTIVE_WORLD", "_active")

READY_TIMEOUT = float(os.environ.get("READY_TIMEOUT", "180"))
SAVE_TIMEOUT = float(os.environ.get("SAVE_TIMEOUT", "60"))

# Engine stdout markers. The multiplayer manager flips to (InGame) once the
# world is loaded and the server is accepting connections.
RE_READY = re.compile(r"changing state from\(.*?\) to\(InGame\)")
RE_SAVE_DONE = re.compile(r"Saving finished")
RE_SAVE_FAIL = re.compile(r"Saving failed|Can't save")


def log(msg):
    sys.stdout.write(f"[supervisor] {msg}\n")
    sys.stdout.flush()


def save_path(name):
    return os.path.join(SAVES_DIR, f"{name}.zip")


def list_saves():
    if not os.path.isdir(SAVES_DIR):
        return []
    # `_`-prefixed files are supervisor internals (the `_active` scratch file,
    # `_autosaveN`) -- not user-facing named saves.
    return sorted(
        f[:-4] for f in os.listdir(SAVES_DIR)
        if f.endswith(".zip") and not f.startswith("_")
    )


def newest_named_save():
    """The user-facing named save most recently written, or None."""
    saves = [
        (os.path.getmtime(os.path.join(SAVES_DIR, f"{n}.zip")), n)
        for n in list_saves()
    ]
    return max(saves)[1] if saves else None


# --- RCON client ----------------------------------------------------------

class RconError(RuntimeError):
    """An RCON auth/connect/command failure."""


class Rcon:
    """Minimal Source-RCON client (stdlib socket only).

    Factorio speaks the Source RCON wire protocol: each packet is a
    little-endian int32 length followed by ``id`` (int32), ``type`` (int32), a
    null-terminated ascii body, and a trailing null byte. We authenticate once
    on connect, then run commands synchronously. A trailing empty packet acts as
    a sentinel so multi-packet (>4 KB) responses are fully drained before we
    return. All socket I/O is serialized by a lock because the HTTP control
    surface is a ``ThreadingHTTPServer`` -- concurrent control requests must not
    interleave on the one socket.
    """

    AUTH = 3
    AUTH_RESPONSE = 2
    EXECCOMMAND = 2
    RESPONSE_VALUE = 0

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.lock = threading.Lock()
        self._id = 0

    def _next_id(self) -> int:
        self._id = (self._id + 1) & 0x7FFFFFFF
        return self._id

    def _send(self, req_id: int, typ: int, body: str) -> None:
        payload = struct.pack("<ii", req_id, typ) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("rcon socket closed mid-read")
            buf += chunk
        return buf

    def _recv_packet(self):
        length = struct.unpack("<i", self._recv_exact(4))[0]
        payload = self._recv_exact(length)
        req_id, typ = struct.unpack("<ii", payload[:8])
        body = payload[8:-2]  # drop the body terminator + packet terminator nulls
        return req_id, typ, body.decode("utf-8", "replace")

    def connect(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self.sock = s
        auth_id = self._next_id()
        self._send(auth_id, self.AUTH, self.password)
        # The server may emit an empty RESPONSE_VALUE before the auth reply;
        # loop until the AUTH_RESPONSE. id == -1 means the password was rejected.
        while True:
            req_id, typ, _ = self._recv_packet()
            if typ == self.AUTH_RESPONSE:
                if req_id == -1:
                    raise RconError("rcon auth failed (bad password)")
                return

    def command(self, cmd: str) -> str:
        with self.lock:
            if self.sock is None:
                raise RconError("rcon not connected")
            req_id = self._next_id()
            self._send(req_id, self.EXECCOMMAND, cmd)
            # Factorio replies to each EXECCOMMAND with exactly ONE packet bearing
            # the same id (probed: no empty-sentinel echo, unlike Source servers).
            # A >4 KB reply is split into multiple same-id packets that arrive
            # back-to-back, so read the first match at the full timeout, then
            # briefly drain any continuation fragments.
            chunks = []
            self.sock.settimeout(self.timeout)
            while True:
                try:
                    rid, _typ, body = self._recv_packet()
                except socket.timeout:
                    break
                if rid == req_id:
                    chunks.append(body)
                    self.sock.settimeout(0.3)   # short drain for fragments
            self.sock.settimeout(self.timeout)
            if not chunks:
                raise RconError("no rcon response")
            return "".join(chunks)

    def close(self) -> None:
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None


# --- engine management ----------------------------------------------------

class Engine:
    """Owns the single Factorio child process and serializes control ops."""

    def __init__(self):
        self.proc = None
        self.current_world = None
        self.lock = threading.Lock()           # one control op at a time
        self._ready = threading.Event()
        self._save_done = threading.Event()
        self._save_ok = True
        # RCON client to the live engine, (re)connected after every (re-)exec.
        # Gameplay/perceive ops use this; they must NOT take self.lock (which
        # serializes world re-execs) -- the Rcon's own I/O lock guards them.
        self.rcon = None

    # -- lifecycle --

    def _common_args(self):
        args = ["--mod-directory", MODS_DIR]
        return args

    def _spawn(self, label):
        """Launch the engine on the `_active` scratch file and start the stdout
        pump. `label` is the human name of the named save `_active` was derived
        from (None for a freshly generated world); it is only metadata for
        status -- the engine always runs on `_active`."""
        self._ready.clear()
        cmd = [
            FACTORIO_BIN,
            "--start-server", save_path(ACTIVE),
            "--server-settings", SERVER_SETTINGS,
            "--port", str(GAME_PORT),
            "--rcon-port", str(RCON_PORT),
            "--rcon-password", RCON_PASSWORD,
        ] + self._common_args()
        log(f"launching engine: active<-{label!r} port={GAME_PORT}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )
        self.current_world = label
        threading.Thread(target=self._pump_stdout, args=(self.proc,), daemon=True).start()

    def _pump_stdout(self, proc):
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if RE_READY.search(line):
                self._ready.set()
            elif RE_SAVE_DONE.search(line):
                self._save_ok = True
                self._save_done.set()
            elif RE_SAVE_FAIL.search(line):
                self._save_ok = False
                self._save_done.set()
        log("engine stdout closed (process exited)")

    def _console(self, command):
        """Write a console command to the engine's stdin."""
        if not self.is_running():
            raise RuntimeError("engine is not running")
        self.proc.stdin.write(command.rstrip("\n") + "\n")
        self.proc.stdin.flush()

    def _stop(self, save_first=True):
        if self.rcon is not None:
            self.rcon.close()
            self.rcon = None
        if not self.is_running():
            return
        log("stopping engine")
        try:
            if save_first:
                self._save_blocking()  # best-effort flush before shutdown
            self._console("/quit")
        except Exception as e:
            log(f"clean stop failed ({e}); sending SIGTERM")
            try:
                self.proc.terminate()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log("engine did not exit; killing")
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout=READY_TIMEOUT):
        return self._ready.wait(timeout=timeout)

    # -- rcon --

    def _connect_rcon(self, timeout=30.0):
        """(Re)connect the RCON client to the freshly-(re-)exec'd engine.

        The RCON listener opens a beat after the (InGame) marker, so retry for a
        short window. Always called right after a successful ``wait_ready``; a
        failure is logged but non-fatal (world ops keep working; gameplay verbs
        report 'engine/RCON not ready' until a reconnect lands)."""
        if self.rcon is not None:
            self.rcon.close()
            self.rcon = None
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if not self.is_running():
                return
            client = Rcon("127.0.0.1", RCON_PORT, RCON_PASSWORD)
            try:
                client.connect()
                # Warm up the Lua command channel: RCON accepts auth a beat
                # before the first command reliably executes (the very first
                # command right after (InGame) can come back empty). Poll until a
                # string round-trips, so callers only see a fully-usable channel.
                for _ in range(40):
                    if client.command('/silent-command rcon.print("ready")').strip() == "ready":
                        self.rcon = client
                        log(f"rcon connected on :{RCON_PORT}")
                        return
                    time.sleep(0.25)
                client.close()
                last = RuntimeError("rcon warmup did not confirm")
            except Exception as e:
                client.close()
                last = e
                time.sleep(1.0)
        log(f"WARNING: rcon did not connect within {timeout:.0f}s ({last!r})")

    def _rcon_cmd(self, command):
        if self.rcon is None:
            raise RuntimeError("engine/RCON not ready")
        return self.rcon.command(command)

    def _lua_json_call(self, iface_fn, args_lua):
        """Run ``remote.call('game_bot', <fn>, <args>)`` and JSON-decode its
        returned table. The table_to_json flavor is resolved inline (2.0 moved it
        from ``game`` to ``helpers``) so the mod stays a pure library."""
        lua = (
            "local r=remote.call('game_bot','%s',%s); "
            "rcon.print((helpers and helpers.table_to_json or game.table_to_json)(r))"
            % (iface_fn, args_lua)
        )
        out = self._rcon_cmd("/silent-command " + lua).strip()
        try:
            return json.loads(out)
        except Exception:
            # A lua error comes back as a plain (non-JSON) string on the channel.
            raise RuntimeError(out or "empty rcon response")

    # -- map creation --

    def _create_map(self, world, seed=None):
        """Generate a fresh map save with `--create` (uses freeplay defaults)."""
        mapgen = MAP_GEN_SETTINGS if os.path.exists(MAP_GEN_SETTINGS) else None
        if seed is not None and mapgen:
            # Patch seed into an active copy so we don't mutate the template.
            with open(mapgen) as f:
                data = json.load(f)
            data["seed"] = int(seed)
            mapgen = os.path.join(CONFIG_DIR, ".map-gen-active.json")
            with open(mapgen, "w") as f:
                json.dump(data, f)
        cmd = [FACTORIO_BIN, "--create", save_path(world)] + self._common_args()
        if mapgen:
            cmd += ["--map-gen-settings", mapgen]
        log(f"creating map: world={world!r} seed={seed}")
        res = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write(res.stdout)
        if res.returncode != 0:
            sys.stdout.write(res.stderr)
            raise RuntimeError(f"map creation failed (exit {res.returncode})")

    # -- save flush --

    def _save_blocking(self, timeout=SAVE_TIMEOUT):
        """Trigger /server-save and wait for the engine to confirm it flushed."""
        self._save_done.clear()
        self._save_ok = True
        self._console("/server-save")
        if not self._save_done.wait(timeout=timeout):
            raise RuntimeError("timed out waiting for save to flush")
        if not self._save_ok:
            raise RuntimeError("engine reported save failure")

    # -- public ops (each serialized by self.lock) --

    def op_status(self):
        return {
            "running": self.is_running(),
            "ready": self._ready.is_set(),
            "current_world": self.current_world,
            "saves": list_saves(),
            "game_port": GAME_PORT,
        }

    def op_save(self, name=None, overwrite=False):
        """Snapshot the live world to an immutable named save.

        Flushes the running world to `_active`, then copies `_active` ->
        `<name>.zip`. The named save is the ONLY file this writes; it is never
        touched by load/new. Refuses to clobber an existing name unless
        `overwrite` is set, so a snapshot is never silently lost."""
        if not name:
            raise ValueError("save requires a 'name'")
        if name.startswith("_"):
            raise ValueError("save names cannot start with '_' (reserved)")
        with self.lock:
            if not self.is_running():
                raise RuntimeError("engine is not running")
            exists = os.path.exists(save_path(name))
            if exists and not overwrite:
                raise FileExistsError(
                    f"save {name!r} already exists; pass overwrite=true to replace it"
                )
            self._save_blocking()                       # live -> _active
            shutil.copy(save_path(ACTIVE), save_path(name))   # _active -> name
            self.current_world = name
            result = {"saved": name, "replaced": exists}
            log(f"save complete: {result}")
            return result

    def op_new(self, seed=None):
        """Generate a fresh world straight into `_active` and relaunch. Discards
        the live world's unsaved progress (as the desktop UI does); no named save
        is touched."""
        with self.lock:
            self._stop(save_first=False)               # discarding _active anyway
            self._create_map(ACTIVE, seed=seed)
            self._spawn(label=None)
            ready = self.wait_ready()
            self._connect_rcon()
            return {"world": None, "seed": seed, "ready": ready}

    def op_load(self, name):
        """Restore a named save: copy `<name>.zip` -> `_active`, relaunch on
        `_active`. The named save is read-only -- loading can never advance it."""
        with self.lock:
            if not os.path.exists(save_path(name)):
                raise FileNotFoundError(f"no save named {name!r}")
            self._stop(save_first=False)               # discarding _active anyway
            shutil.copy(save_path(name), save_path(ACTIVE))   # name -> _active
            self._spawn(label=name)
            ready = self.wait_ready()
            self._connect_rcon()
            return {"world": name, "ready": ready}

    # -- gameplay / perceive ops (RCON-backed; NOT under self.lock) --
    #
    # These reach the live engine over RCON and deliberately do NOT take
    # self.lock -- that lock serializes world re-execs, and sharing it would make
    # a gameplay call block on an in-flight new/load. The Rcon I/O lock already
    # serializes the socket.

    def op_observe(self, radius=None):
        """Full world/body snapshot via the companion mod's observe interface."""
        args_lua = "{radius=%d}" % int(radius) if radius is not None else "{}"
        return self._lua_json_call("observe", args_lua)

    def op_exec_lua(self, code):
        """Run arbitrary Lua via /silent-command; return whatever it printed.

        To get a value back the caller's code must ``rcon.print(...)`` -- the
        engine returns the console output verbatim (empty string if nothing was
        printed)."""
        if not code:
            raise ValueError("exec_lua requires 'code'")
        return {"output": self._rcon_cmd("/silent-command " + code)}

    def op_pause(self, paused):
        """Toggle game.tick_paused and report the resulting state."""
        val = "true" if paused else "false"
        out = self._rcon_cmd(
            "/silent-command game.tick_paused=%s rcon.print(game.tick_paused)" % val
        ).strip()
        return {"paused": out == "true"}

    def op_body_spawn(self, surface=None, x=0, y=0):
        surf = surface or "nauvis"
        args_lua = "{surface=%s,x=%s,y=%s}" % (
            json.dumps(str(surf)), float(x), float(y),
        )
        return self._lua_json_call("spawn", args_lua)

    def op_body_move(self, x, y):
        args_lua = "{x=%s,y=%s}" % (float(x), float(y))
        return self._lua_json_call("set_target", args_lua)

    def op_body_stop(self):
        return self._lua_json_call("stop", "{}")

    # -- boot --

    def boot(self):
        """Bring the engine up at container start on the `_active` scratch file.

          * `_active` exists       -> resume it (normal restart).
          * else a named save exists -> adopt the newest one into `_active`
                                        (cold start / first boot after upgrade,
                                        so an existing world survives).
          * else                    -> generate a fresh default into `_active`.
        """
        if os.path.exists(save_path(ACTIVE)):
            label = None
            log("resuming live world from _active")
        elif (newest := newest_named_save()) is not None:
            label = newest
            log(f"no _active; adopting newest named save into _active: {newest!r}")
            shutil.copy(save_path(newest), save_path(ACTIVE))
        else:
            label = None
            log("no saves found; creating default world")
            self._create_map(ACTIVE)
        self._spawn(label=label)
        if self.wait_ready():
            log("engine ready")
            self._connect_rcon()
        else:
            log("WARNING: engine did not reach ready state within timeout")


# --- HTTP control surface -------------------------------------------------

ENGINE = Engine()


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "factorio-appliance/1.0"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or "{}")

    def log_message(self, fmt, *args):  # quieter; supervisor logs ops itself
        pass

    def do_GET(self):
        if self.path.rstrip("/") in ("/status", ""):
            return self._send(200, ENGINE.op_status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.rstrip("/")
        try:
            body = self._read_json()
            if route == "/save":
                result = ENGINE.op_save(body.get("name"), bool(body.get("overwrite", False)))
                return self._send(200, {"ok": True, "result": result})
            if route == "/new":
                result = ENGINE.op_new(body.get("seed"))
                return self._send(200, {"ok": True, "result": result})
            if route == "/load":
                name = body.get("name")
                if not name:
                    return self._send(400, {"ok": False, "error": "missing 'name'"})
                return self._send(200, {"ok": True, "result": ENGINE.op_load(name)})
            # ---- gameplay / perceive (RCON-backed) ----
            if route == "/observe":
                return self._send(200, {"ok": True, "result": ENGINE.op_observe(body.get("radius"))})
            if route == "/exec-lua":
                return self._send(200, {"ok": True, "result": ENGINE.op_exec_lua(body.get("code"))})
            if route == "/pause":
                return self._send(200, {"ok": True, "result": ENGINE.op_pause(bool(body.get("paused")))})
            if route == "/body/spawn":
                result = ENGINE.op_body_spawn(body.get("surface"), body.get("x", 0), body.get("y", 0))
                return self._send(200, {"ok": True, "result": result})
            if route == "/body/move":
                if body.get("x") is None or body.get("y") is None:
                    return self._send(400, {"ok": False, "error": "move requires x and y"})
                return self._send(200, {"ok": True, "result": ENGINE.op_body_move(body["x"], body["y"])})
            if route == "/body/stop":
                return self._send(200, {"ok": True, "result": ENGINE.op_body_stop()})
            return self._send(404, {"ok": False, "error": "not found"})
        except FileNotFoundError as e:
            return self._send(404, {"ok": False, "error": str(e)})
        except (ValueError, FileExistsError) as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            return self._send(500, {"ok": False, "error": str(e)})


def main():
    os.makedirs(SAVES_DIR, exist_ok=True)

    def handle_term(signum, frame):
        log(f"received signal {signum}; shutting down engine")
        try:
            ENGINE._stop(save_first=True)
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    ENGINE.boot()

    httpd = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    log(f"control surface listening on :{CONTROL_PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
