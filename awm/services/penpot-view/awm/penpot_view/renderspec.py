"""The view URL's parameters, and the render variant they name.

A board render is addressed by ``/penpot-view/<file-id>/<page-id>/<board-id>``
plus a query string, and everything that makes one render differ from another
rides in that query string. This module is the single authority for it: the
prefix, the parameter grammar, the canonical spelling, and the fingerprint that
keys the cache in :mod:`awm.penpot_view.view`. Every surface — the HTTP
handler, a plugin, a future autopublish-equivalent — goes through the parser
and the formatter here, which is what stops a parameter from existing on one
surface and silently not on another.

The three path segments are plain Penpot UUIDs (``file-id``/``page-id``/
``board-id``) and never carry an encoded slash or a ``..`` segment, so they
route cleanly through httpsfront's edge, which forwards raw path bytes and
404s a path segment that re-segments when decoded. Everything else --
``scale``, ``swap``, ``crop`` -- lives in the query string on that same
principle: this module never round-trips a parameter through the path.

Two parameters beyond ``scale``, mirroring :mod:`awm.drawio.renderspec`'s
grammar so a caller who already knows drawio's query syntax knows this one:

**``swap=<from>:<to>``**, repeatable, six-digit hex, canonicalised and sorted
the same way drawio does it. What a swap rewrites in the exported SVG --
``fill``/``stroke`` style declarations -- is :mod:`awm.penpot_view.svgpost`'s
job (T8); this module only owns the grammar and the cache identity.

**``crop=<name>``**. Renders only the region covered by the shape on that
board whose name is ``<name>``. Resolving the name against the board and
rewriting the SVG's viewBox is also :mod:`awm.penpot_view.svgpost`'s job.

The parameter order in a URL never matters: both the formatter and the
fingerprint sort, so two callers asking for the same variant produce the same
URL string and the same cache key.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import quote

#: Lives here, not in :mod:`awm.penpot_view.view`, so any module that needs to
#: recognise or build a penpot-view URL can do so without importing the HTTP
#: handler, which imports this module.
VIEW_PREFIX = os.environ.get("PENPOT_VIEW_PREFIX", "/penpot-view")

#: Bounds on `scale`. The upper bound keeps the product of a board's own
#: dimensions and the factor inside six digits, which is where `%g`-style
#: formatting would switch to scientific notation and stop being a valid SVG
#: length. The lower bound keeps it a positive number: a negative width is
#: not a small picture, it is no picture.
MIN_SCALE = 0.01
MAX_SCALE = 20.0

#: Cap on swaps in one request -- see :mod:`awm.drawio.renderspec` for why.
MAX_SWAPS = 24

_HEX6 = re.compile(r"^[0-9a-f]{6}$")
_HEX3 = re.compile(r"^[0-9a-f]{3}$")

#: A Penpot object id: a UUID, lowercase, canonical dashed form. The three
#: path segments (file/page/board) are always this shape.
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class SpecError(ValueError):
    """A view parameter could not be understood. Always names the token."""


def is_uuid(token: str) -> bool:
    """Whether ``token`` is a canonical lowercase Penpot UUID.

    Used to validate the three path segments before they are trusted as
    object ids -- never a colour or a crop name accepted in their place.
    """
    return bool(_UUID.match((token or "").strip()))


@dataclass(frozen=True)
class RenderSpec:
    """Everything a view URL can say about *how* to render, canonicalised.

    Frozen and hashable so it can key a cache without anybody having to
    remember to copy it. Does not carry the file/page/board ids -- those
    identify *what* to render and are resolved separately by the handler's
    ``resolve_target``; this is only the *how*.
    """

    scale: float = 1.0
    #: ``((from, to), …)``, canonical six-digit lowercase hex, sorted.
    swaps: tuple[tuple[str, str], ...] = ()
    crop: str | None = None

    @property
    def is_plain(self) -> bool:
        """Whether this asks for exactly what an un-parameterised URL asks for.

        The plain case keeps the pre-parameter cache path, so deploying a new
        swap/crop caller does not disturb a single already-cached render.
        """
        return self.scale == 1.0 and not self.swaps and self.crop is None


DEFAULT = RenderSpec()


# --- parsing ---------------------------------------------------------------

def canonical_colour(token: str) -> str:
    """``#F0F`` / ``%23ff00ff`` / ``ff00ff`` → ``ff00ff``."""
    raw = (token or "").strip()
    text = raw
    for prefix in ("%23", "#"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
            break
    text = text.lower()
    if _HEX3.match(text):
        return "".join(c * 2 for c in text)
    if _HEX6.match(text):
        return text
    if re.fullmatch(r"[0-9a-f]{8}", text):
        raise SpecError(
            f"colour {raw!r} is eight-digit #rrggbbaa, which is not supported; "
            "use the six-digit form"
        )
    raise SpecError(
        f"colour {raw!r} is not a 3- or 6-digit hex colour (e.g. ff00ff); "
        "named colours and rgb() notation are not supported"
    )


def parse_swap(token: str) -> tuple[str, str]:
    """``ff00ff:00aa55`` → ``("ff00ff", "00aa55")``."""
    raw = (token or "").strip()
    if not raw:
        raise SpecError("empty swap= parameter; expected swap=<from>:<to>")
    if raw.count(":") != 1:
        raise SpecError(
            f"swap {raw!r} is not <from>:<to> (e.g. swap=ff00ff:00aa55)")
    left, right = raw.split(":", 1)
    return canonical_colour(left), canonical_colour(right)


def parse_swaps(tokens: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """Canonicalise, reject duplicate sources, and sort.

    A duplicate source is refused rather than resolved by a last-wins rule --
    see :mod:`awm.drawio.renderspec` for the same call.
    """
    pairs: dict[str, str] = {}
    for token in tokens:
        source, target = parse_swap(token)
        if source in pairs and pairs[source] != target:
            raise SpecError(
                f"colour {source!r} is swapped twice, to {pairs[source]!r} and "
                f"{target!r}; a source colour may only be swapped once")
        pairs[source] = target
    if len(pairs) > MAX_SWAPS:
        raise SpecError(f"{len(pairs)} swaps requested; the limit is {MAX_SWAPS}")
    return tuple(sorted(pairs.items()))


def from_query(query: Mapping[str, list[str]]) -> RenderSpec:
    """Read a spec out of a ``parse_qs`` mapping.

    Unknown parameters are ignored on purpose. ``scale`` keeps a tolerant
    fallback (a junk value renders at 1.0 rather than failing an image
    request); ``swap`` and ``crop`` do not, since they change what the
    picture *means*. The handler must therefore parse with
    ``keep_blank_values=True`` -- otherwise ``?swap=`` is silently discarded
    before this can complain about it.
    """
    scale = 1.0
    raw_scale = (query.get("scale") or [None])[0]
    if raw_scale:
        try:
            scale = float(raw_scale)
        except (TypeError, ValueError):
            scale = 1.0
        # `float()` accepts `inf`, `nan` and arbitrarily large values, and a
        # scale is a multiplier on the root element's width and height. A
        # negative, infinite or huge product is not a valid SVG length, so the
        # browser renders nothing at all -- a blank picture from a request
        # that looked fine, which is the failure this service exists to
        # refuse. Out-of-range falls back to 1.0 for the same reason an
        # unparseable value does: `scale` is a presentation hint, and refusing
        # the whole render over one is worse than ignoring it.
        if not (MIN_SCALE <= scale <= MAX_SCALE):
            scale = 1.0

    swaps = parse_swaps(query.get("swap") or ())

    crop = None
    raw_crop = query.get("crop")
    if raw_crop is not None:
        crop = (raw_crop[0] or "").strip()
        if not crop:
            raise SpecError("empty crop= parameter; expected crop=<shape name>")

    return RenderSpec(scale=scale, swaps=swaps, crop=crop)


# --- formatting --------------------------------------------------------

def to_query(spec: RenderSpec) -> str:
    """The canonical query string for a spec, without the leading ``?``.

    Sorted, so the same variant always spells itself the same way -- two
    callers producing different strings would defeat the cache as surely as a
    different render would.
    """
    parts: list[str] = []
    if spec.scale != 1.0:
        parts.append(f"scale={spec.scale:g}")
    for source, target in spec.swaps:
        parts.append(f"swap={source}:{target}")
    if spec.crop:
        parts.append(f"crop={quote(spec.crop, safe='')}")
    return "&".join(parts)


def view_url(file_id: str, page_id: str, board_id: str,
             spec: RenderSpec = DEFAULT) -> str:
    """The full ``/penpot-view/...`` path (+ query) for a board render.

    Raises :class:`SpecError` if any id is not a canonical UUID, so a caller
    never builds a URL that would 404 for a path-shape reason rather than a
    "does not exist" reason.
    """
    for name, value in (("file-id", file_id), ("page-id", page_id),
                         ("board-id", board_id)):
        if not is_uuid(value):
            raise SpecError(f"{name} {value!r} is not a Penpot UUID")
    path = f"{VIEW_PREFIX}/{file_id}/{page_id}/{board_id}"
    query = to_query(spec)
    return f"{path}?{query}" if query else path


def fingerprint(spec: RenderSpec) -> str:
    """A short, cache-key-safe token for this variant.

    ``__plain__`` for an un-parameterised render, matching
    :mod:`awm.drawio.renderspec`'s convention.
    """
    if spec.is_plain:
        return "__plain__"
    return hashlib.sha256(to_query(spec).encode("utf-8")).hexdigest()[:16]


def cache_key(file_id: str, page_id: str, board_id: str,
              spec: RenderSpec = DEFAULT) -> tuple[str, str, str, str]:
    """The full cache identity: ``(file-id, page-id, board-id, fingerprint)``.

    This is the key shape :mod:`awm.penpot_view.view`'s cache is required to
    use -- a render of a composite board must not force a re-render of the
    child boards it embeds, so the key is scoped to exactly the one board
    being asked for plus the swap/crop variant, nothing coarser.
    """
    return (file_id, page_id, board_id, fingerprint(spec))


def describe(spec: RenderSpec) -> str:
    """Human-readable, for a log line or an error message."""
    if spec.is_plain:
        return "plain"
    bits = []
    if spec.scale != 1.0:
        bits.append(f"scale {spec.scale:g}")
    if spec.swaps:
        bits.append(", ".join(f"#{a}->#{b}" for a, b in spec.swaps))
    if spec.crop:
        bits.append(f"crop {spec.crop!r}")
    return "; ".join(bits)
