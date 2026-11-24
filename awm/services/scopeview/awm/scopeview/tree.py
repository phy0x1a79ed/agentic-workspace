"""One reader over a scope at its worktree or at any git ref.

Text comes from the worktree or from git. Data comes through the DVC pins at that ref: a pin names
md5s, and the bytes are read from the materialised file when it still matches or else from the
shared cache. Nothing here runs dvc.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from functools import cached_property
from pathlib import Path

import yaml


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Tree:
    def __init__(self, root: Path, ref: str | None = None):
        self.root, self.ref = root, ref or None
        self.commit = git(root, "rev-parse", ref or "HEAD").strip()

    @cached_property
    def _git_files(self) -> dict[str, str | None]:
        """Path to blob hash at the ref, or to None in the worktree (hashed on read)."""
        if self.ref:
            out = {}
            for ln in git(self.root, "ls-tree", "-r", "-z", self.commit).split("\0"):
                if ln:
                    meta, path = ln.split("\t", 1)
                    out[path] = meta.split()[2]
            return out
        listed = git(self.root, "ls-files", "-z", "-co", "--exclude-standard").split("\0")
        return {p: None for p in listed if p and (self.root / p).is_file()}

    def files(self, prefix: str) -> list[str]:
        return sorted(p for p in self._git_files if p.startswith(prefix))

    def exists(self, path: str) -> bool:
        return path in self._git_files or path in self.pins

    def read(self, path: str) -> bytes:
        if path in self.pins:
            return self.local(path).read_bytes()
        return self._read_git(path)

    def _read_git(self, path: str) -> bytes:
        if self.ref:
            return subprocess.run(["git", "-C", str(self.root), "cat-file", "blob", self._git_files[path]],
                                  capture_output=True, check=True).stdout
        return (self.root / path).read_bytes()

    def text(self, path: str) -> str:
        return self.read(path).decode()

    def hash(self, path: str) -> str | None:
        """Content hash: the DVC md5 for pinned data, the git blob hash for text, None when absent."""
        if path in self.pins:
            return self.pins[path]
        if path not in self._git_files:
            return None
        return self._git_files[path] or blob_hash(self._read_git(path))

    @cached_property
    def cache_dir(self) -> Path:
        for name in ("config.local", "config"):
            p = self.root / ".dvc" / name
            if p.exists():
                m = re.search(r"^\s*dir\s*=\s*(.+?)\s*$", p.read_text(), re.M)
                if m:
                    d = Path(m.group(1))
                    return d if d.is_absolute() else (p.parent / d).resolve()
        return self.root / ".dvc" / "cache"

    def _object(self, md5: str) -> Path:
        return self.cache_dir / "files" / "md5" / md5[:2] / md5[2:]

    @cached_property
    def _pinned(self) -> tuple[dict[str, str], set[str]]:
        pins, unlisted = {}, set()
        for pin in self.files("data/"):
            if not pin.endswith(".dvc"):
                continue
            base = pin.rsplit("/", 1)[0]
            for o in (yaml.safe_load(self._read_git(pin)) or {}).get("outs", []):
                top = f"{base}/{o['path']}"
                if not o["md5"].endswith(".dir"):
                    pins[top] = o["md5"]
                elif (obj := self._object(o["md5"])).exists():
                    pins.update({f"{top}/{e['relpath']}": e["md5"] for e in json.loads(obj.read_text())})
                else:
                    unlisted.add(top)
        return pins, unlisted

    @property
    def pins(self) -> dict[str, str]:
        """Every DVC-tracked file at this ref whose md5 is known: path to md5."""
        return self._pinned[0]

    @property
    def unlisted(self) -> set[str]:
        """Pinned folders whose listing is not in this cache."""
        return self._pinned[1]

    @cached_property
    def stamp(self) -> str:
        """Changes whenever what the graph reads changes: the commit, plus in the worktree each chain file and pin."""
        if self.ref:
            return self.commit
        st = [(p, s.st_mtime_ns, s.st_size) for p in self._git_files
              if (p.startswith("chain/") or p.endswith(".dvc")) and (s := (self.root / p).stat())]
        return hashlib.sha1(repr((self.commit, st)).encode()).hexdigest()

    def local(self, path: str) -> Path:
        """A readable file holding a pinned path's bytes at this ref."""
        md5 = self.pins[path]
        obj = self._object(md5)
        if obj.exists():
            return obj
        here = self.root / path
        if not self.ref and here.exists():
            return here
        raise FileNotFoundError(f"{path}: md5 {md5} is not in the DVC cache at {self.cache_dir}")

    def log(self, *paths: str, limit: int = 50, first_parent: bool = False) -> list[dict]:
        args = ["log", f"-{limit}", "--format=%H%x1f%h%x1f%aI%x1f%an%x1f%s"]
        if first_parent:
            args.append("--first-parent")
        out = git(self.root, *args, self.commit, "--", *paths)
        keys = ("commit", "short", "date", "author", "subject")
        return [dict(zip(keys, ln.split("\x1f"))) for ln in out.splitlines() if ln]
