"""Isolated sessions (fleet.md R109): Claude Code in a rootless Docker container.

The container runs the host's own Claude binary, mounted read only, inside a tmux
session so cch can type into it. It sees the scope's worktree and the project's bare
repository, and nothing else of the host: no ssh, no ``~/.claude``, no secrets.

Its login is a copy of the host's with the refresh token removed. The host stays the
only refresher. ``sync`` rewrites each copy after the host refreshes, and deletes it
when the host login is gone or expired, so a fault signs out the container and never
the host (R109 items 4 and 5).

It reaches awm over HTTP MCP with a per-session caller token, because it has no
host pid the gateway could resolve. ``caller_of_token`` answers who holds a token.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from awm.config import SERVICES_DIR

log = logging.getLogger("awm.cch.isolated")

LABEL = "awm.cch.isolated"
PREFIX = "awm-iso-"
HOME_IN = "/home/agent"
CONFIG_IN = f"{HOME_IN}/.claude"
CLAUDE_IN = "/opt/claude/claude"
TMUX_SESSION = "main"

#: What a container copy of the login keeps. ``refreshToken`` is left out on purpose.
LOGIN_KEYS = ("accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier")

#: The awm verbs an isolated session holds for its normal work (R109). The gateway
#: refuses every other verb for a caller token, and refuses a scope other than the
#: session's own where a verb takes one.
WHITELIST: dict[str, tuple[str, ...]] = {
    "journal": ("render", "inbox", "fetch", "ack", "goal_read", "post"),
    "search": ("query", "get"),
    "precedence": ("search",),
    "reflection": ("whoami", "compact", "message_self", "pending", "mode"),
    "operations": ("track", "status", "request_human", "done"),
}

ROOTFUL_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")

Runner = Callable[[Sequence[str], dict], subprocess.CompletedProcess]


class IsolationError(RuntimeError):
    """An isolated session could not be started or reached."""


def root() -> Path:
    return Path(os.environ.get("AWM_ISOLATED_DIR") or SERVICES_DIR / "cch" / "isolated")


def session_dir(session_id: str) -> Path:
    return root() / session_id


def config_dir(session_id: str) -> Path:
    return session_dir(session_id) / "claude"


def host_login_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / ".credentials.json"


# --- the login copy -----------------------------------------------------------


def strip_login(host: Any) -> Optional[dict]:
    """The host login without its refresh token, or None when it holds no access token."""
    oauth = host.get("claudeAiOauth") if isinstance(host, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    return {"claudeAiOauth": {k: oauth[k] for k in LOGIN_KEYS if k in oauth}}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def sync_login(dest: Path, host_path: Optional[Path] = None, *,
               now_ms: Optional[int] = None) -> str:
    """Bring one container copy up to date with the host login.

    Returns ``written``, ``unchanged`` or ``signed_out``. A host login that is
    missing, unreadable, wiped or expired deletes the copy.
    """
    now = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        host = json.loads((host_path or host_login_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        host = None
    copy = strip_login(host)
    expires = (copy or {}).get("claudeAiOauth", {}).get("expiresAt")
    if copy is None or (isinstance(expires, (int, float)) and expires <= now):
        dest.unlink(missing_ok=True)
        return "signed_out"
    text = json.dumps(copy, indent=2)
    try:
        if dest.read_text(encoding="utf-8") == text:
            return "unchanged"
    except OSError:
        pass
    _write_private(dest, text)
    return "written"


def sync_all(host_path: Optional[Path] = None) -> dict[str, str]:
    """Sync the login copy of every isolated session on this node."""
    out = {}
    for rec in records():
        sid = rec["claude_session_id"]
        out[sid] = sync_login(config_dir(sid) / ".credentials.json", host_path)
    return out


def watch(host_path: Optional[Path] = None, *, every_s: float = 30.0,
          stop: Optional[threading.Event] = None) -> None:
    """Sync every copy whenever the host login file changes."""
    path = host_path or host_login_path()
    seen: Optional[tuple] = None
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            st = path.stat()
            now = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            now = None
        if now != seen:
            seen = now
            for sid, what in sync_all(path).items():
                if what != "unchanged":
                    log.info("cch: isolated %s login %s", sid, what)
        stop.wait(every_s)


# --- caller tokens ------------------------------------------------------------


def _tokens_path() -> Path:
    return root() / "tokens.json"


_tokens_lock = threading.Lock()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _read_tokens() -> dict:
    try:
        data = json.loads(_tokens_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mint(record: dict) -> str:
    """A new caller token for ``record``. Only its digest is kept."""
    token = secrets.token_urlsafe(32)
    with _tokens_lock:
        data = _read_tokens()
        data[_digest(token)] = record
        _write_private(_tokens_path(), json.dumps(data, indent=2))
    return token


def revoke(claude_session_id: str) -> int:
    with _tokens_lock:
        data = _read_tokens()
        kept = {d: r for d, r in data.items()
                if r.get("claude_session_id") != claude_session_id}
        if len(kept) != len(data):
            _write_private(_tokens_path(), json.dumps(kept, indent=2))
    return len(data) - len(kept)


def caller_of_token(token: Any) -> Optional[dict]:
    if not isinstance(token, str) or not token:
        return None
    return _read_tokens().get(_digest(token))


# --- the container ------------------------------------------------------------


def docker_host() -> str:
    """The rootless Docker socket. A rootful one is refused (R101, R109 item 6)."""
    host = (os.environ.get("AWM_ISOLATED_DOCKER_HOST")
            or f"unix:///run/user/{os.getuid()}/docker.sock")
    path = host.removeprefix("unix://")
    if path in ROOTFUL_SOCKETS:
        raise IsolationError(f"{host} is the rootful Docker daemon; an isolated "
                             f"session runs only on rootless Docker")
    return host


def _run(argv: Sequence[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), env=env, capture_output=True, text=True, timeout=120)


#: Overridable for tests.
runner: Runner = _run


def docker(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DOCKER_HOST": docker_host()}
    env.pop("SSH_AUTH_SOCK", None)
    return runner(["docker", *args], env)


def container_name(claude_session_id: str) -> str:
    return PREFIX + claude_session_id.replace("-", "")[:12]


def git_common_dir(worktree: Path) -> Optional[Path]:
    """The repository a worktree's commits land in, which the container must also see."""
    marker = worktree / ".git"
    if marker.is_dir():
        return None
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (worktree / gitdir).resolve()
    common = gitdir / "commondir"
    try:
        rel = common.read_text(encoding="utf-8").strip()
        return (gitdir / rel).resolve()
    except OSError:
        return gitdir.parent.parent


@dataclass(frozen=True)
class Spec:
    claude_session_id: str
    name: str
    worktree: Path
    claude_bin: Path
    prompt: str = ""
    model: Optional[str] = None
    effort: Optional[str] = None
    resume: bool = False
    image: str = "awm-isolated:1"
    network: str = "bridge"
    cpus: str = "2"
    memory: str = "4g"
    pids: int = 512


def spec_from_env(**kw: Any) -> Spec:
    """A spec with the node's limits, image and network from the environment."""
    env = os.environ.get
    return Spec(image=env("AWM_ISOLATED_IMAGE") or "awm-isolated:1",
                network=env("AWM_ISOLATED_NETWORK") or "bridge",
                cpus=env("AWM_ISOLATED_CPUS") or "2",
                memory=env("AWM_ISOLATED_MEMORY") or "4g",
                pids=int(env("AWM_ISOLATED_PIDS") or 512), **kw)


def run_argv(spec: Spec) -> list[str]:
    """The ``docker run`` arguments for one isolated session."""
    name = container_name(spec.claude_session_id)
    mounts = ["-v", f"{spec.claude_bin}:{CLAUDE_IN}:ro",
              "-v", f"{config_dir(spec.claude_session_id)}:{CONFIG_IN}",
              "-v", f"{spec.worktree}:{spec.worktree}"]
    common = git_common_dir(spec.worktree)
    if common is not None:
        mounts += ["-v", f"{common}:{common}"]
    claude = [CLAUDE_IN, "--dangerously-skip-permissions",
              "--mcp-config", f"{CONFIG_IN}/awm-mcp.json", "--strict-mcp-config"]
    claude += (["--resume", spec.claude_session_id] if spec.resume
               else ["--session-id", spec.claude_session_id])
    if spec.model:
        claude += ["--model", spec.model]
    if spec.effort:
        claude += ["--effort", spec.effort]
    if spec.prompt:
        claude += ["--", spec.prompt]
    return ["run", "-d", "-t", "--name", name,
            "--label", f"{LABEL}={spec.claude_session_id}",
            "--cpus", spec.cpus, "--memory", spec.memory, "--memory-swap", spec.memory,
            "--pids-limit", str(spec.pids),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--network", spec.network, "--add-host", "host.docker.internal:host-gateway",
            *mounts, "-w", str(spec.worktree),
            "-e", f"HOME={HOME_IN}", "-e", f"CLAUDE_CONFIG_DIR={CONFIG_IN}",
            "-e", "IS_SANDBOX=1",
            spec.image, "tmux", "new-session", "-s", TMUX_SESSION, *claude]


def hub_url_inside() -> str:
    return os.environ.get("AWM_ISOLATED_HUB_URL") or "http://host.docker.internal:8765"


def _prepare(spec: Spec, token: str) -> None:
    """The container's own Claude config: login copy, MCP config, onboarding done."""
    cfg = config_dir(spec.claude_session_id)
    cfg.mkdir(parents=True, exist_ok=True)
    if sync_login(cfg / ".credentials.json") == "signed_out":
        raise IsolationError("the host has no live Claude login to copy; run "
                             "/login on the host first")
    mcp = {"mcpServers": {"awm": {"type": "http", "url": f"{hub_url_inside()}/mcp",
                                  "headers": {"X-Awm-Caller-Token": token}}}}
    _write_private(cfg / "awm-mcp.json", json.dumps(mcp, indent=2))
    state = cfg / ".claude.json"
    try:
        known = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    known["hasCompletedOnboarding"] = True
    known.setdefault("projects", {}).setdefault(str(spec.worktree), {})[
        "hasTrustDialogAccepted"] = True
    _write_private(state, json.dumps(known, indent=2))


def _record_path(claude_session_id: str) -> Path:
    return session_dir(claude_session_id) / "record.json"


def records() -> list[dict]:
    out = []
    try:
        dirs = sorted(p for p in root().iterdir() if p.is_dir())
    except OSError:
        return out
    for d in dirs:
        try:
            rec = json.loads((d / "record.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("claude_session_id"):
            out.append(rec)
    return out


def find(ref: str) -> Optional[dict]:
    """The isolated session ``ref`` names: its session id, container or name."""
    text = (ref or "").strip().lower()
    if not text:
        return None
    for rec in records():
        sid = rec["claude_session_id"]
        if text in (sid, sid.replace("-", ""), rec.get("container"),
                    str(rec.get("name") or "").lower()):
            return rec
    return None


def _launch(spec: Spec, base: dict) -> dict:
    revoke(spec.claude_session_id)
    token = mint({**base, "trust": "untrusted", "tools": {k: list(v) for k, v in WHITELIST.items()}})
    try:
        _prepare(spec, token)
        docker("rm", "-f", container_name(spec.claude_session_id))
        out = docker(*run_argv(spec))
    except Exception:
        revoke(spec.claude_session_id)
        raise
    if out.returncode != 0:
        revoke(spec.claude_session_id)
        raise IsolationError(f"docker run failed: {(out.stderr or out.stdout).strip()[:400]}")
    return base


def start(*, project: str, scope: str, worktree: Path, name: str, prompt: str = "",
          model: Optional[str] = None, effort: Optional[str] = None,
          claude_bin: Optional[Path] = None) -> dict:
    """Start one isolated session. Returns its record."""
    sid = str(uuid.uuid4())
    binary = Path(claude_bin or _claude_bin()).resolve()
    if not binary.is_file():
        raise IsolationError(f"the Claude binary {binary} is not a file")
    spec = spec_from_env(claude_session_id=sid, name=name, worktree=Path(worktree),
                         claude_bin=binary, prompt=prompt, model=model, effort=effort)
    rec = {"claude_session_id": sid, "container": container_name(sid), "name": name,
           "project": project, "scope": scope, "cwd": str(worktree),
           "model": model, "effort": effort, "claude_bin": str(binary),
           "started_ms": int(time.time() * 1000)}
    session_dir(sid).mkdir(parents=True, exist_ok=True)
    _write_private(_record_path(sid), json.dumps(rec, indent=2))
    return _launch(spec, rec)


def resume(rec: dict, *, prompt: str = "") -> dict:
    sid = rec["claude_session_id"]
    spec = spec_from_env(claude_session_id=sid, name=rec.get("name") or "",
                         worktree=Path(rec["cwd"]), claude_bin=Path(rec["claude_bin"]),
                         prompt=prompt, model=rec.get("model"), effort=rec.get("effort"),
                         resume=True)
    return _launch(spec, rec)


def _claude_bin() -> str:
    from awm.cch import config

    return config.claude_bin()


def running(rec: dict) -> bool:
    out = docker("inspect", "-f", "{{.State.Running}}", rec["container"])
    return out.returncode == 0 and out.stdout.strip() == "true"


def stop(rec: dict, *, keep: bool = True) -> dict:
    """Remove the container and its caller token. With keep the transcript stays."""
    docker("rm", "-f", rec["container"])
    revoke(rec["claude_session_id"])
    if not keep:
        import shutil

        shutil.rmtree(session_dir(rec["claude_session_id"]), ignore_errors=True)
    return {"ok": True, "stopped": True, "removed": not keep, "kept": keep}


def send(rec: dict, text: str, *, submit: bool = True) -> dict:
    if "\n" in text or "\r" in text:
        return {"ok": False, "reason": "an isolated session takes one line at a time"}
    target = rec["container"]
    out = docker("exec", target, "tmux", "send-keys", "-t", TMUX_SESSION, "-l", text)
    if out.returncode == 0 and submit:
        out = docker("exec", target, "tmux", "send-keys", "-t", TMUX_SESSION, "Enter")
    if out.returncode != 0:
        return {"ok": False, "reason": f"could not type into {target}: "
                                       f"{(out.stderr or out.stdout).strip()[:300]}"}
    return {"ok": True, "submitted": submit, "text": text}


def interrupt(rec: dict) -> dict:
    out = docker("exec", rec["container"], "tmux", "send-keys", "-t", TMUX_SESSION, "Escape")
    return {"ok": out.returncode == 0}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m awm.cch.isolated sync [--watch]``: the host helper of R109 item 4."""
    import argparse

    ap = argparse.ArgumentParser(prog="awm.cch.isolated")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="rewrite each container's login copy from the host's")
    s.add_argument("--watch", action="store_true", help="keep syncing on each host refresh")
    s.add_argument("--every", type=float, default=30.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.watch:
        watch(every_s=args.every)
        return 0
    for sid, what in sync_all().items():
        print(f"{sid} {what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
