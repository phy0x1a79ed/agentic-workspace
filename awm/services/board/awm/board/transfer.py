"""The guard on files that cross the board: what may leave this node, and where arrivals land.

**Out.** A file leaves only in two steps. ``check`` reads it and returns a
verdict and its sha256. ``send`` re-reads it and sends it only when the caller
passes that sha256 back as ``allow``, so the agent approves one exact content,
and a file that changed after the check is refused. Both steps refuse a file
that is a symlink to elsewhere, not a regular file, over the size cap, under a
denied path, of a denied kind, or that holds something shaped like a
credential, including the literal value of any secret in this service's own
environment. The denylist cannot be widened from a call: ``AWM_BOARD_FILE_DENY``
only adds patterns.

**In.** A fetched file lands under the quarantine directory, one directory per
file, mode 0444, never executable, checked against the sha256 the board
recorded. Nothing here opens, imports or runs it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
from pathlib import Path

from awm.board.files import DEFAULT_MAX_BYTES, clean_name

#: Path patterns no file may leave from. ``~`` is the home directory. Matched
#: against the resolved path, so a symlink into one of these is caught too.
DENY_PATHS = (
    "~/.awm-secrets/**", "~/.ssh/**", "~/.gnupg/**", "~/.aws/**", "~/.azure/**",
    "~/.kube/**", "~/.docker/**", "~/.config/gh/**", "~/.config/gcloud/**",
    "~/.claude/**", "~/.claude.json", "~/.netrc", "~/.git-credentials", "~/.pgpass",
    "~/lib/secrets/**", "~/capella-keep/**",
    "**/.awm/**", "**/.git/**", "**/.dvc/cache/**", "**/.dvc_cache/**",
    "**/data/models/**", "**/projects/avatar/**/data/**", "**/projects/avatar/**/raw/**",
    "/mnt/**", "/etc/**", "/root/**",
    "/proc/**", "/sys/**", "/dev/**",
)

#: File names no file may carry, whatever its directory.
DENY_NAMES = (
    ".env", ".env.*", "*.env", "env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.kdbx",
    "*.token", "*token*.json", "*secret*", "*credential*", "*password*", "id_rsa*",
    "id_ecdsa*", "id_ed25519*", "id_dsa*", "*.ovpn", "known_hosts", "authorized_keys",
    "*.db", "*.sqlite", "*.sqlite3",
)

#: Kinds that are game or model assets: licensed or ripped content stays home.
DENY_SUFFIXES = (
    ".glb", ".gltf", ".fbx", ".vrm", ".vrma", ".blend", ".blend1", ".obj", ".dae", ".pmx",
    ".pmd", ".vmd", ".unitypackage", ".unity3d", ".assets", ".bundle", ".pak", ".uasset",
    ".usm", ".acb", ".awb", ".wem", ".bnk", ".ktx2", ".dds",
)

#: Content shaped like a credential.
SECRET_PATTERNS = tuple(re.compile(p) for p in (
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    rb"-----BEGIN OPENSSH PRIVATE KEY-----",
    rb"\bawmut1[._-][A-Za-z0-9._-]{8,}",
    rb"\bawmpt1[._-][A-Za-z0-9._-]{8,}",
    rb"\bgh[pousr]_[A-Za-z0-9]{30,}",
    rb"\bgithub_pat_[A-Za-z0-9_]{30,}",
    rb"\bsk-ant-[A-Za-z0-9_-]{20,}",
    rb"\bsk-[A-Za-z0-9]{32,}",
    rb"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    rb"\bAKIA[0-9A-Z]{16}\b",
    rb"\bAIza[0-9A-Za-z_-]{35}\b",
    rb"\b[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",
    rb"(?i)\b(?:AWM_[A-Z_]*TOKEN|AWM_[A-Z_]*SECRET|api[_-]?key|client[_-]?secret)\s*[=:]\s*\S{8,}",
    rb"(?i)authorization:\s*bearer\s+[A-Za-z0-9._~+/-]{20,}",
))

#: Environment variable names whose values count as secrets for the literal scan.
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|KEY|CRED|COOKIE)", re.IGNORECASE)
_MIN_SECRET_LEN = 12


def max_bytes() -> int:
    try:
        return max(1, int(os.environ.get("AWM_BOARD_FILE_MAX") or DEFAULT_MAX_BYTES))
    except ValueError:
        return DEFAULT_MAX_BYTES


def _extra_deny() -> tuple[str, ...]:
    raw = os.environ.get("AWM_BOARD_FILE_DENY") or ""
    return tuple(p.strip() for p in re.split(r"[,:\s]+", raw) if p.strip())


def _expand(pattern: str) -> str:
    return str(Path(pattern).expanduser()) if pattern.startswith("~") else pattern


def _path_match(path: str, pattern: str) -> bool:
    """Glob match where ``**/`` may match no directory at all and ``/**`` the directory itself."""
    pattern = _expand(pattern)
    candidates = {pattern}
    if pattern.startswith("**/"):
        candidates.add("/" + pattern[3:])
    if pattern.endswith("/**"):
        candidates.add(pattern[:-3])
    for c in list(candidates):
        candidates.add(c.replace("/**/", "/"))
    return any(fnmatch.fnmatchcase(path, c) or fnmatch.fnmatchcase(path, c.replace("**", "*"))
               for c in candidates)


def _secret_values() -> list[bytes]:
    out = []
    for name, value in os.environ.items():
        if _SECRET_ENV.search(name) and len(value or "") >= _MIN_SECRET_LEN:
            out.append(value.encode())
    return out


def check(path: str) -> dict:
    """Whether the file at ``path`` may leave this node: a verdict, its reasons and its sha256."""
    reasons: list[str] = []
    raw = Path(os.path.expanduser(str(path or "")))
    if not raw.is_absolute():
        return {"ok": False, "allowed": False, "reasons": ["the path must be absolute"]}
    try:
        resolved = raw.resolve(strict=True)
        st = os.stat(resolved)
    except OSError as exc:
        return {"ok": False, "allowed": False, "reasons": [f"cannot read the file: {exc.strerror}"]}
    shown = str(resolved)
    if not stat.S_ISREG(st.st_mode):
        reasons.append("not a regular file")
    for candidate in {str(raw), shown}:
        for pattern in DENY_PATHS + _extra_deny():
            if _path_match(candidate, pattern):
                reasons.append(f"the path is denied ({pattern})")
    name = resolved.name.lower()
    for pattern in DENY_NAMES:
        if fnmatch.fnmatchcase(name, pattern):
            reasons.append(f"the file name is denied ({pattern})")
    for suffix in DENY_SUFFIXES:
        if name.endswith(suffix):
            reasons.append(f"model or game assets stay home ({suffix})")
    cap = max_bytes()
    if st.st_size > cap:
        reasons.append(f"larger than the {cap}-byte cap")
    if st.st_size == 0:
        reasons.append("the file is empty")
    data = b""
    if not reasons:
        with open(resolved, "rb") as fh:
            data = fh.read(cap + 1)
        if len(data) > cap:
            reasons.append(f"larger than the {cap}-byte cap")
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                reasons.append("the content looks like it holds a credential")
                break
        if any(value in data for value in _secret_values()):
            reasons.append("the content holds a secret from this node's environment")
    reasons = list(dict.fromkeys(reasons))
    out = {"ok": True, "allowed": not reasons, "path": shown, "name": clean_name(resolved.name),
           "size": st.st_size, "reasons": reasons}
    if not reasons:
        out["sha256"] = hashlib.sha256(data).hexdigest()
        out["data"] = data
    return out


def quarantine_dir() -> Path:
    override = os.environ.get("AWM_BOARD_QUARANTINE")
    if override:
        return Path(override)
    from awm.config import AWM_DIR

    return Path(AWM_DIR) / "board" / "quarantine"


def landing(card_id: str, file_id: str, name: str) -> Path:
    """Where one fetched file goes: its own new directory under the quarantine."""
    if not (re.fullmatch(r"[0-9a-f]{32}", card_id) and re.fullmatch(r"[0-9a-f]{32}", file_id)):
        raise ValueError("card_id and file_id must be 32 hex digits")
    return quarantine_dir() / card_id / file_id / clean_name(name)


def seal(path: Path) -> None:
    """Make an arrival read-only and never executable, and its directory closed to others."""
    os.chmod(path, 0o444)
    os.chmod(path.parent, 0o700)
