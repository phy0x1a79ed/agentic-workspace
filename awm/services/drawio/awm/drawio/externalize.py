"""Convert embedded image payloads into filesystem references.

Diagrams built before this service carry their images inline, as
``image=data:image/svg+xml,…`` inside each cell's style string. That works, but
it means a re-rendered figure does not reach the diagram until somebody
re-imports it, and it inflates the document enormously — in the SCADC biomass
map, 326 embedded SVGs account for 91% of a 5.8 MB file.

Rewriting them as ``/files/<abs-path>`` references makes the diagram point at
the renderer's actual output, so re-rendering a molecule updates the figure on
reload. Export still produces self-contained output because
:mod:`awm.drawio.export` inlines the bytes on the way out.

**Matching is by exact content hash, never by name.** A filename match would be
a guess, and a wrong guess silently swaps one molecule's structure for
another's in a published figure — the kind of error that survives review because
the figure still looks right. Anything that does not match byte-for-byte is left
embedded and reported.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import unquote

log = logging.getLogger("awm.drawio.externalize")

#: Inline payloads in a style string. drawio splits styles on ``;``, so the
#: payload itself can never contain one — which is what makes this bounded.
_EMBEDDED = re.compile(r'(?P<uri>data:(?P<mime>[a-zA-Z0-9.+/-]+),(?P<payload>[^;"\']+))')

_EXTENSIONS = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}


def index_files(roots: list[str | Path],
                extensions: tuple[str, ...] = (".svg", ".png", ".jpg", ".jpeg")
                ) -> dict[str, Path]:
    """Content-hash → path, over every candidate image under ``roots``.

    First path wins on a duplicate, and paths are walked in sorted order, so the
    mapping is deterministic across runs — otherwise re-running a migration
    could point the same cell at a different (identical) file and produce a
    diff that means nothing.

    That makes **root order the caller's precedence control**, and it matters
    more than it looks. An archive directory usually holds byte-identical
    copies of what the renderer currently emits, so a reference into it
    resolves and renders correctly — and the mistake is invisible until someone
    re-renders and the figure does not change. Passing the live render
    directory ahead of (or instead of) any archive is how you avoid that;
    passing one parent directory sweeps up its archive subdirectories, whose
    names may well sort first.
    """
    index: dict[str, Path] = {}
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            log.warning("externalize: search root %s does not exist", base)
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            index.setdefault(digest, path.resolve())
    return index


def _digest_of(mime: str, payload: str) -> str:
    """Hash the decoded payload the same way :func:`index_files` hashes a file."""
    decoded = unquote(payload)
    if mime.startswith("text/") or mime == "image/svg+xml":
        return hashlib.sha256(decoded.encode("utf-8")).hexdigest()
    return hashlib.sha256(decoded.encode("latin-1", errors="replace")).hexdigest()


def externalize(xml: str, roots: list[str | Path]) -> tuple[str, dict]:
    """Rewrite matched inline payloads as ``/files/…`` references.

    Returns the rewritten document and a report. Nothing is written here; the
    caller applies the result to a checkout, so the change lands through the
    normal merge contract rather than behind anyone's back.
    """
    index = index_files(roots)
    matched: list[dict] = []
    unmatched: list[dict] = []
    saved = 0

    def replace(match: re.Match) -> str:
        nonlocal saved
        mime, payload = match.group("mime"), match.group("payload")
        if mime.startswith("data:") or "," not in match.group("uri"):
            return match.group(0)
        digest = _digest_of(mime, payload)
        target = index.get(digest)
        if target is None:
            unmatched.append({
                "mime": mime, "bytes": len(payload),
                "preview": unquote(payload)[:120],
            })
            return match.group(0)
        ref = f"/files{target.as_posix()}"
        saved += len(match.group("uri")) - len(ref)
        matched.append({"path": str(target), "was_bytes": len(match.group("uri"))})
        return ref

    rewritten = _EMBEDDED.sub(replace, xml)
    return rewritten, {
        "converted": len(matched),
        "unmatched": len(unmatched),
        "bytes_saved": saved,
        "files": sorted({m["path"] for m in matched}),
        "unmatched_detail": unmatched[:20],
        "indexed": len(index),
    }
