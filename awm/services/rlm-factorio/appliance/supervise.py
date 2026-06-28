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
import shutil
import signal
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
            return {"world": name, "ready": ready}

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
