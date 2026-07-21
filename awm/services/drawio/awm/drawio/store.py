"""The versioned diagram store: one git repository holding the whole diagram tree.

Every accepted write — browser autosave, agent edit, script import — is
normalized (see :mod:`awm.drawio.xmlmodel`) and committed. Revisions are
commits, history is the log, restore is a forward-only rewrite. The roughly
fifty hand-named ``.bak`` files that the prototype accumulated stop having a
reason to exist.

**Why one repo and not one per diagram.** The working tree *is* the folder
structure the reception page shows, so the on-disk layout and the logical
layout are the same thing and neither can drift from the other. Sharing a repo
across diagrams would normally couple them — an agent holding a checkout of
diagram A would be blocked by unrelated commits to diagram B — but nothing here
uses repo-wide ancestry. Every concurrency question is asked *per path*:
"has this diagram changed since the revision my checkout is based on?" is
``git log <base>..HEAD -- <path>``, and landing is a guarded single-file
update rather than a branch fast-forward. Diagrams are therefore independent
in practice while still living in one coherent tree.

**Why saves usually do not commit.** Normalization strips viewport state, so a
tab that is merely scrolled or page-switched re-saves to byte-identical content
and :meth:`Store.write` returns ``changed=False`` without touching history.
Editing bursts are folded further by amending the tip when the same author saves
again inside :data:`SETTLE_SECONDS` — but only when no live checkout is based on
that commit, since amending would move a sha somebody is holding.

Git identity is passed per invocation rather than read from user config: the
service must behave identically under systemd, in a test, and on a developer's
machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import xmlmodel

#: How long an editing burst by one author folds into a single revision.
SETTLE_SECONDS = 180

#: Commit identity. Authorship is carried in the commit *message* trailer
#: instead of the author field so that "who" survives amending and stays
#: greppable without a second git call.
GIT_NAME = "awm-drawio"
GIT_EMAIL = "drawio@awm.local"

EMPTY_DIAGRAM = """<mxfile>
  <diagram id="page-1" name="Page-1">
    <mxGraphModel grid="1" gridSize="10" guides="1" tooltips="1" connect="1" \
arrows="1" fold="1" page="1" pageScale="1" pageWidth="850" pageHeight="1100" \
math="0" shadow="0">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


class StoreError(RuntimeError):
    """A store operation could not be completed."""


class UnknownDiagram(StoreError):
    """No diagram exists at that path."""


@dataclass(frozen=True)
class Revision:
    rev: str
    author: str
    label: str
    when: int

    def as_dict(self) -> dict:
        return {"rev": self.rev, "author": self.author, "label": self.label,
                "when": self.when}


def normalize_save_path(save: str) -> str:
    """Validate and canonicalize a logical diagram path.

    Paths are repo-relative, POSIX, and always end in ``.drawio``. Anything
    that could escape the tree or collide with git's own directory is refused
    outright rather than sanitized — a silently rewritten path would let two
    logical names alias the same file.
    """
    if not isinstance(save, str) or not save.strip():
        raise StoreError("diagram path is required")
    raw = save.strip().replace("\\", "/")
    # Refused, not stripped: quietly turning "/etc/passwd.drawio" into a
    # relative path would let an absolute-looking name alias a tree entry.
    if raw.startswith("/"):
        raise StoreError(f"diagram path must be relative to the store: {save!r}")
    if not raw.endswith(".drawio"):
        raw += ".drawio"
    parts = [p for p in raw.split("/") if p]
    for part in parts:
        if part in (".", "..") or part.startswith(".") or "\0" in part:
            raise StoreError(f"illegal path segment {part!r} in {save!r}")
    if not parts:
        raise StoreError(f"empty diagram path: {save!r}")
    return "/".join(parts)


class Store:
    """Git-backed diagram store rooted at ``root``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        # Revisions a live checkout is based on. Amending one would move a sha
        # somebody is holding, so :meth:`_can_amend` refuses while pinned.
        self._pinned: set[str] = set()
        self._ensure_repo()

    # --- git plumbing ------------------------------------------------------

    def _git(self, *args: str, check: bool = True, cwd: Path | None = None
             ) -> subprocess.CompletedProcess:
        cmd = [
            "git",
            "-c", f"user.name={GIT_NAME}",
            "-c", f"user.email={GIT_EMAIL}",
            "-c", "core.autocrlf=false",
            "-c", "gc.auto=0",
            *args,
        ]
        proc = subprocess.run(
            cmd, cwd=str(cwd or self.root), capture_output=True, text=True,
        )
        if check and proc.returncode != 0:
            raise StoreError(
                f"git {' '.join(args[:2])} failed ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def _ensure_repo(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / ".git").exists():
            return
        # -b main rather than relying on init.defaultBranch, which is unset on
        # git 2.34 and would leave us on 'master' plus a warning on every init.
        self._git("init", "-b", "main")
        (self.root / ".gitattributes").write_text(
            "# Diagrams are canonical XML text. Never let a platform rewrite\n"
            "# line endings underneath the normalizer.\n"
            "*.drawio -text -merge=binary diff\n",
            encoding="utf-8",
        )
        self._git("add", ".gitattributes")
        self._git("commit", "-m", "init drawio store\n\nAuthor-Handle: system")

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    # --- paths -------------------------------------------------------------

    def abs_path(self, save: str) -> Path:
        return self.root / normalize_save_path(save)

    def exists(self, save: str) -> bool:
        return self.abs_path(save).is_file()

    def _require(self, save: str) -> str:
        path = normalize_save_path(save)
        if not (self.root / path).is_file():
            raise UnknownDiagram(f"no diagram at {path!r}")
        return path

    # --- reading -----------------------------------------------------------

    def list(self) -> list[dict]:
        """Every diagram in the tree, with page counts and current revision."""
        out = []
        for path in sorted(self.root.rglob("*.drawio")):
            if ".git" in path.parts:
                continue
            rel = path.relative_to(self.root).as_posix()
            try:
                out.append(self.info(rel))
            except StoreError:
                out.append({"save": rel, "error": "unreadable"})
        return out

    def info(self, save: str) -> dict:
        path = self._require(save)
        text = (self.root / path).read_text(encoding="utf-8")
        mxfile = xmlmodel.parse(text)
        revs = self.history(path, limit=1)
        tip = revs[0].as_dict() if revs else None
        return {
            "save": path,
            "bytes": len(text.encode("utf-8")),
            "pages": xmlmodel.page_summaries(mxfile),
            "rev": tip["rev"] if tip else None,
            "author": tip["author"] if tip else None,
            "label": tip["label"] if tip else None,
            "modified": tip["when"] if tip else None,
        }

    def read(self, save: str, rev: str | None = None) -> str:
        path = self._require(save) if rev is None else normalize_save_path(save)
        if rev is None:
            return (self.root / path).read_text(encoding="utf-8")
        proc = self._git("show", f"{rev}:{path}", check=False)
        if proc.returncode != 0:
            raise UnknownDiagram(f"{path!r} does not exist at revision {rev!r}")
        return proc.stdout

    def history(self, save: str, limit: int = 50) -> list[Revision]:
        """Revisions that touched this diagram, newest first."""
        path = normalize_save_path(save)
        proc = self._git(
            "log", f"-{max(1, int(limit))}", "--format=%H%x1f%ct%x1f%B%x1e",
            "--", path, check=False,
        )
        if proc.returncode != 0:
            return []
        revisions = []
        for record in proc.stdout.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, when, body = record.split("\x1f", 2)
            revisions.append(Revision(
                rev=sha,
                author=_trailer(body, "Author-Handle") or "unknown",
                label=body.splitlines()[0] if body.strip() else "",
                when=int(when),
            ))
        return revisions

    def diff(self, save: str, a: str, b: str | None = None) -> str:
        path = normalize_save_path(save)
        args = ["diff", "--no-color", a] + ([b] if b else []) + ["--", path]
        return self._git(*args, check=False).stdout

    def head_rev(self, save: str) -> str | None:
        """The revision that last changed this diagram, or ``None`` if untracked."""
        path = normalize_save_path(save)
        proc = self._git("log", "-1", "--format=%H", "--", path, check=False)
        sha = proc.stdout.strip()
        return sha or None

    def changed_since(self, save: str, base_rev: str) -> list[Revision]:
        """Revisions touching this diagram after ``base_rev`` — the drift check.

        This is the only ancestry question the store asks, and it is scoped to
        one path, which is what keeps diagrams independent inside a shared repo.
        """
        path = normalize_save_path(save)
        proc = self._git(
            "log", "--format=%H%x1f%ct%x1f%B%x1e", f"{base_rev}..HEAD", "--", path,
            check=False,
        )
        if proc.returncode != 0:
            raise StoreError(f"unknown base revision {base_rev!r}")
        revisions = []
        for record in proc.stdout.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, when, body = record.split("\x1f", 2)
            revisions.append(Revision(
                rev=sha,
                author=_trailer(body, "Author-Handle") or "unknown",
                label=body.splitlines()[0] if body.strip() else "",
                when=int(when),
            ))
        return revisions

    # --- writing -----------------------------------------------------------

    def write(self, save: str, xml: str, author: str,
              label: str | None = None, allow_amend: bool = True) -> dict:
        """Normalize and commit ``xml`` as the new content of ``save``.

        Returns ``{save, rev, changed, amended}``. ``changed=False`` means the
        canonical form was byte-identical to what is already there — the common
        case for a tab that was scrolled but not edited — and nothing was
        committed.
        """
        path = normalize_save_path(save)
        canonical = xmlmodel.normalize(xml)
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.is_file() and target.read_text(encoding="utf-8") == canonical:
            return {"save": path, "rev": self.head_rev(path), "changed": False,
                    "amended": False}

        target.write_text(canonical, encoding="utf-8")
        self._git("add", "--", path)

        amend = allow_amend and self._can_amend(path, author)
        message = _commit_message(label or f"edit {path}", author)
        args = ["commit", "-m", message]
        if amend:
            args.append("--amend")
        self._git(*args)
        return {"save": path, "rev": self._head(), "changed": True,
                "amended": amend}

    def _can_amend(self, path: str, author: str) -> bool:
        """Whether this write should fold into the tip instead of adding a commit.

        Only when the tip is a recent commit by the same author touching only
        this diagram — and never when the caller has pinned it via
        :meth:`pin_revision`, because amending moves a sha a checkout is holding.
        """
        proc = self._git("log", "-1", "--format=%H%x1f%ct%x1f%B", check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        sha, when, body = proc.stdout.split("\x1f", 2)
        if sha in self._pinned:
            return False
        if _trailer(body, "Author-Handle") != author:
            return False
        if time.time() - int(when) > SETTLE_SECONDS:
            return False
        touched = self._git(
            "show", "--name-only", "--format=", sha, check=False,
        ).stdout.split()
        return touched == [path]

    def pin_revision(self, rev: str) -> None:
        """Protect a revision from being amended away (a checkout is based on it)."""
        self._pinned.add(rev)

    def unpin_revision(self, rev: str) -> None:
        self._pinned.discard(rev)

    # --- lifecycle ---------------------------------------------------------

    def create(self, save: str, author: str, xml: str | None = None) -> dict:
        path = normalize_save_path(save)
        if (self.root / path).exists():
            raise StoreError(f"{path!r} already exists")
        return self.write(path, xml or EMPTY_DIAGRAM, author,
                          label=f"create {path}", allow_amend=False)

    def import_file(self, save: str, source: str | Path, author: str) -> dict:
        src = Path(source).expanduser()
        if not src.is_file():
            raise StoreError(f"no such file: {src}")
        return self.write(save, src.read_text(encoding="utf-8"), author,
                          label=f"import {src.name}", allow_amend=False)

    def restore(self, save: str, rev: str, author: str) -> dict:
        """Re-land an earlier revision's content as a *new* revision.

        Forward-only: history is never rewritten, so a restore can itself be
        undone by restoring what preceded it.
        """
        content = self.read(save, rev=rev)
        return self.write(save, content, author,
                          label=f"restore {normalize_save_path(save)} to {rev[:8]}",
                          allow_amend=False)

    def remove(self, save: str, author: str) -> dict:
        path = self._require(save)
        self._git("rm", "-q", "--", path)
        self._git("commit", "-m", _commit_message(f"remove {path}", author))
        return {"save": path, "rev": self._head(), "changed": True}


def _commit_message(label: str, author: str) -> str:
    return f"{label.strip().splitlines()[0]}\n\nAuthor-Handle: {author}\n"


def _trailer(body: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in reversed(body.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def default_root() -> Path:
    """Where the store lives: ``<AWM_DIR>/services/drawio/diagrams``.

    Overridable with ``AWM_DRAWIO_ROOT`` so tests and dev sandboxes get their
    own tree instead of sharing prod's.
    """
    override = os.environ.get("AWM_DRAWIO_ROOT")
    if override:
        return Path(override).expanduser()
    from awm.persistence.databases import service_db_path

    return service_db_path("drawio").parent / "diagrams"


def which_git() -> str | None:
    return shutil.which("git")
