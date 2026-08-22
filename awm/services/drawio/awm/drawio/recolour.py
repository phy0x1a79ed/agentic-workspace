"""Applying a colour swap to content, whatever the content is made of.

:mod:`awm.drawio.renderspec` names the colours — it owns the URL grammar, the
canonical spelling and the parser. This module *applies* them, and it is the
only place that knows how a picture is encoded. The dependency runs one way:
``renderspec`` and ``export`` import this; this imports neither.

Three surfaces, because a colour arrives in three shapes.

**Text.** A style value, a label, the source of a referenced ``.svg``. One
compiled alternation over every source colour, applied in a single pass, so
replacement is simultaneous rather than chained.

**A ``data:`` URI.** This is how drawio stores art you imported into a diagram,
and getting at it is the whole reason this module exists — a page whose mask
lives inside an imported SVG used to ignore ``swap=`` entirely.

*The codec is sniffed from the payload, not read off the header.* drawio writes
an imported SVG as ``data:image/svg+xml,<base64>`` — the comma form, because a
``;base64`` marker would be cut in half by the style splitter, but base64
content all the same. A payload this service inlined itself is the same comma
form carrying percent-encoded text. Both spell their mime type identically, so
only the payload distinguishes them: a ``%`` means percent-encoded, a payload
drawn entirely from the base64 alphabet means base64. Whichever it came in on
is what it goes back out on, so the value keeps the shape drawio itself would
have written. A ``;base64,`` URI is never *introduced*, because the style
parser splits on that semicolon and the cell renders blank.

**Pixels.** PNG, JPEG, GIF and WebP decode through Pillow and are recoloured in
numpy. A palette image has its palette remapped, which is exact and nearly
free. Otherwise a pixel is shifted by ``target - source`` with a weight that is
1 within :data:`RASTER_TOLERANCE` of the source colour and falls off to 0 at
:data:`RASTER_SOFT_EDGE`.

Two decisions in that sentence are load-bearing. The tolerance has to be
non-zero or JPEG never matches anything: a lossy encoder turns a flat
``#ff00dc`` field into a cloud of near-magenta. And shifting by a delta rather
than filling flat is what keeps shading, dithering and compression noise
intact instead of flattening a textured region into a blob — while still
landing an exactly-flat mask exactly on the target. The falloff band is what
stops the boundary of the recoloured region from ringing.

**Nothing that did not match is ever re-encoded.** If no source colour is
present the original payload comes back verbatim. That is not an optimisation:
the view cache is keyed on a hash of the inlined document, so a re-encode that
shifted one byte would mint a new key for every embedded image on every render
and invalidate the whole store.

A swap that cannot be honoured — Pillow absent, an animated image, a payload
over the size cap — is reported rather than skipped, through the same
``problems`` channel that surfaces as ``X-Drawio-Problems``.
"""

from __future__ import annotations

import base64
import io
import re
from urllib.parse import quote, unquote_to_bytes

#: How far from a source colour a pixel may be and still be shifted in full.
#: Non-zero because JPEG never reproduces a flat field exactly.
RASTER_TOLERANCE = 32

#: Where the shift weight reaches zero. Between here and the tolerance the
#: shift is scaled linearly, so the edge of a recoloured region fades out
#: instead of leaving a hard ring one pixel wide.
RASTER_SOFT_EDGE = 96

#: The largest payload this module will decode. Mirrors ``export``'s per-image
#: inline limit; kept as its own constant because importing ``export`` here
#: would be a cycle.
MAX_PAYLOAD_BYTES = 24 * 1024 * 1024

#: Decoded-pixel ceiling, on top of Pillow's own decompression-bomb guard. A
#: recolour pass allocates a few float arrays the size of the image.
MAX_RASTER_PIXELS = 40_000_000

#: How deep an image may nest another before this stops looking. An imported
#: SVG wrapping a PNG is one level; that is the case worth having.
MAX_NESTING = 2

_RASTER_MIMES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
})

#: Fallback when Pillow cannot name the format it opened.
_MIME_TO_FORMAT = {
    "image/png": "PNG", "image/jpeg": "JPEG", "image/jpg": "JPEG",
    "image/gif": "GIF", "image/webp": "WEBP", "image/bmp": "BMP",
    "image/tiff": "TIFF",
}

_B64_ONLY = re.compile(r"[A-Za-z0-9+/]+={0,2}")

#: A ``data:`` URI *inside* decoded text — an imported SVG that wraps a raster.
#: Restricted to the ``;base64`` marker form on purpose: that is what every
#: tool writes inside an SVG, and inside an SVG there is no style parser to
#: trip over the semicolon.
_NESTED_DATA_URI = re.compile(
    r"data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<payload>[A-Za-z0-9+/=\s]+)")


def _spellings(colour: str) -> list[str]:
    """Every hex spelling that denotes this colour: ``ff00ff`` and ``f0f``."""
    out = [colour]
    if colour[0] == colour[1] and colour[2] == colour[3] and colour[4] == colour[5]:
        out.append(colour[0] + colour[2] + colour[4])
    return out


def _rgb(colour: str) -> tuple[int, int, int]:
    return (int(colour[0:2], 16), int(colour[2:4], 16), int(colour[4:6], 16))


def _is_texty(mime: str) -> bool:
    mime = mime.lower()
    return (mime.startswith("text/") or mime.endswith("+xml")
            or mime in ("application/xml", "image/svg+xml", "application/json"))


class Substituter:
    """One compiled alternation over every source colour, applied in one pass.

    Simultaneity falls out of this: ``re.sub`` never rescans its own output, so
    a colour written by one swap can never be read by another. The trailing
    lookahead stops a six-digit match from eating the first six digits of an
    eight-digit value, and lets a three-digit source decline ``#abcdef``.

    Accumulates ``hits`` across every surface it is used on, and ``problems``
    for a swap it could not carry out. Problems are deduplicated: one document
    can hold hundreds of images, and "Pillow is not installed" said three
    hundred times is noise, not information.
    """

    def __init__(self, swaps: tuple[tuple[str, str], ...]):
        self.swaps = tuple(swaps)
        self.hits = 0
        self.problems: list[str] = []
        self._seen_problems: set[str] = set()
        self._lookup: dict[str, str] = {}
        alternatives: list[str] = []
        for source, target in self.swaps:
            for spelling in _spellings(source):
                alternatives.append(spelling)
                self._lookup[spelling] = target
        alternatives.sort(key=len, reverse=True)
        self._pattern = re.compile(
            "#(" + "|".join(alternatives) + r")(?![0-9a-fA-F])",
            re.IGNORECASE) if alternatives else None

    def __bool__(self) -> bool:
        return self._pattern is not None

    def report(self, message: str) -> None:
        if message not in self._seen_problems:
            self._seen_problems.add(message)
            self.problems.append(message)

    # --- text --------------------------------------------------------------

    def sub(self, text: str) -> str:
        if self._pattern is None or not text:
            return text

        def _one(match: re.Match) -> str:
            self.hits += 1
            return "#" + self._lookup[match.group(1).lower()]

        return self._pattern.sub(_one, text)

    # --- encoded payloads --------------------------------------------------

    def sub_data_uri(self, value: str, *, depth: int = 0) -> str:
        """Recolour a ``data:`` URI, keeping the codec it arrived in.

        Anything that is not a ``data:`` URI — a ``/files`` reference, an http
        URL, a stencil name — comes back untouched, so this is safe to point at
        any ``image=`` value without testing it first.
        """
        if self._pattern is None or not value:
            return value
        parsed = _parse_data_uri(value)
        if parsed is None:
            return value
        head, marker, payload = parsed
        if not payload:
            return value
        mime = head.split(";", 1)[0].strip().lower()
        raw, codec = _decode(payload, marker, mime)
        if raw is None:
            return value
        if len(raw) > MAX_PAYLOAD_BYTES:
            self.report(f"embedded {mime} is {len(raw) / 1e6:.1f} MB, over the "
                        f"{MAX_PAYLOAD_BYTES / 1e6:.0f} MB recolour limit; "
                        "left as it was")
            return value

        new_raw, hits = self._sub_bytes(raw, mime, depth=depth)
        if hits == 0:
            # The invariant: an image that did not match is never re-encoded.
            return value
        self.hits += hits
        new_payload = _encode(new_raw, codec)
        if ";" in new_payload:  # pragma: no cover — neither codec can emit one
            raise AssertionError(
                "recolour produced a payload containing ';', which drawio's "
                "style parser would cut in half")
        return f"data:{head}{marker}," + new_payload

    def sub_bytes(self, raw: bytes, mime: str) -> bytes:
        """Recolour raw bytes read off disk. Unmatched bytes come back as-is."""
        if self._pattern is None or not raw:
            return raw
        new_raw, hits = self._sub_bytes(raw, mime.lower(), depth=0)
        if hits == 0:
            return raw
        self.hits += hits
        return new_raw

    def _sub_bytes(self, raw: bytes, mime: str, *, depth: int) -> tuple[bytes, int]:
        """The branch table. Returns ``(bytes, hits)``; ``raw`` itself if none."""
        if _is_texty(mime):
            return self._sub_text_bytes(raw, depth=depth)
        if mime in _RASTER_MIMES:
            return _swap_raster(raw, mime, self.swaps, self.report)
        return raw, 0

    def _sub_text_bytes(self, raw: bytes, *, depth: int) -> tuple[bytes, int]:
        """Substitute in decoded text, then in any raster it wraps."""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        before = self.hits
        # Borrow the shared counter for this pass rather than duplicating the
        # regex, then hand the delta back so the caller decides whether to
        # re-encode at all.
        swapped = self.sub(text)
        hits = self.hits - before
        self.hits = before

        if depth < MAX_NESTING:
            def _nested(match: re.Match) -> str:
                nonlocal hits
                payload = match.group("payload")
                try:
                    inner = base64.b64decode(
                        re.sub(r"\s", "", payload), validate=False)
                except Exception:  # noqa: BLE001 — a payload we cannot read
                    return match.group(0)
                if not inner or len(inner) > MAX_PAYLOAD_BYTES:
                    return match.group(0)
                new_inner, inner_hits = self._sub_bytes(
                    inner, match.group("mime").lower(), depth=depth + 1)
                if inner_hits == 0:
                    return match.group(0)
                hits += inner_hits
                return (f'data:{match.group("mime")};base64,'
                        + base64.b64encode(new_inner).decode("ascii"))

            swapped = _NESTED_DATA_URI.sub(_nested, swapped)

        if hits == 0:
            return raw, 0
        return swapped.encode("utf-8"), hits


# --- data URI codec --------------------------------------------------------

def _parse_data_uri(value: str) -> tuple[str, str, str] | None:
    """``data:<head>[;base64],<payload>`` → ``(head, marker, payload)``.

    ``head`` is kept verbatim — it can carry a ``charset`` this module has no
    business rewriting — and ``marker`` is either ``";base64"`` or empty, so
    the URI can be reassembled exactly as it arrived.
    """
    if not value[:5].lower() == "data:":
        return None
    head, sep, payload = value[5:].partition(",")
    if not sep:
        return None
    marker = ""
    if head.lower().endswith(";base64"):
        head, marker = head[:-7], ";base64"
    return head, marker, payload


def _decode(payload: str, marker: str, mime: str) -> tuple[bytes | None, str]:
    """Payload text → bytes, plus the codec name to re-encode with.

    See the module docstring for why the comma form has to be sniffed rather
    than trusted: drawio and this service both write it, and they write it
    differently.
    """
    if marker:
        try:
            return base64.b64decode(payload, validate=False), "base64"
        except Exception:  # noqa: BLE001
            return None, "base64"
    if "%" in payload:
        return unquote_to_bytes(payload), "percent"
    if len(payload) % 4 == 0 and _B64_ONLY.fullmatch(payload):
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            # A text payload that is accidentally base64-shaped would decode to
            # binary noise; requiring the decode to be readable text settles it
            # in favour of the reading that makes sense.
            if not _is_texty(mime):
                return raw, "base64"
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                return raw, "base64"
    return payload.encode("utf-8"), "percent"


def _encode(raw: bytes, codec: str) -> str:
    if codec == "base64":
        return base64.b64encode(raw).decode("ascii")
    # Empty safe-set: a ';' inside the payload — routine in SVG, which embeds
    # CSS — has to be escaped too, or the style splitter cuts the value.
    return quote(raw, safe="")


# --- pixels ----------------------------------------------------------------

def _swap_raster(raw: bytes, mime: str,
                 swaps: tuple[tuple[str, str], ...],
                 report) -> tuple[bytes, int]:
    """Recolour an encoded raster image. ``(bytes, hits)``; ``raw`` if none.

    ``hits`` counts the source colours that landed on at least one pixel, not
    the pixels themselves, so it stays comparable with the text branch's count
    and keeps the shared "no colour matched" warning meaningful.
    """
    try:
        from PIL import Image
    except ImportError:
        report("recolouring a raster image needs Pillow, which is not "
               "installed; run the service's install.sh")
        return raw, 0
    import numpy as np

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 — any decoder failure is a report
        report(f"could not decode an embedded {mime} to recolour it: {exc}")
        return raw, 0

    if getattr(img, "n_frames", 1) > 1:
        report(f"an animated {mime} was left un-recoloured; flatten it first")
        return raw, 0
    if img.width * img.height > MAX_RASTER_PIXELS:
        report(f"an embedded {mime} is {img.width}x{img.height}, over the "
               f"{MAX_RASTER_PIXELS / 1e6:.0f} megapixel recolour limit")
        return raw, 0

    fmt = img.format or _MIME_TO_FORMAT.get(mime)
    if fmt is None:  # pragma: no cover — Pillow names every format it opens
        report(f"could not tell what format an embedded {mime} is in")
        return raw, 0

    info = dict(img.info)
    if img.mode in ("P", "PA"):
        hits = _remap_palette(img, swaps)
    elif img.mode in ("1", "L", "LA", "I", "F"):
        return raw, 0  # greyscale: no colour to swap
    else:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        img, hits = _shift_pixels(Image, img, swaps, np)

    if hits == 0:
        return raw, 0

    out = io.BytesIO()
    options: dict = {}
    if fmt == "JPEG":
        if img.mode != "RGB":
            img = img.convert("RGB")
        options = {"quality": 95, "subsampling": 0}
    elif fmt == "WEBP":
        options = {"quality": 95}
    elif fmt in ("PNG", "GIF") and "transparency" in info:
        options = {"transparency": info["transparency"]}
    try:
        img.save(out, format=fmt, **options)
    except Exception as exc:  # noqa: BLE001
        report(f"recoloured an embedded {mime} but could not re-encode it: {exc}")
        return raw, 0
    return out.getvalue(), hits


def _remap_palette(img, swaps: tuple[tuple[str, str], ...]) -> int:
    """Rewrite the palette in place. Exact, and independent of image size."""
    palette = getattr(img, "palette", None)
    raw_mode = getattr(palette, "mode", "RGB") or "RGB"
    if raw_mode not in ("RGB", "RGBA"):
        raw_mode = "RGB"
    entries = img.getpalette(raw_mode)
    if not entries:
        return 0
    stride = 4 if raw_mode == "RGBA" else 3
    targets = {_rgb(source): _rgb(target) for source, target in swaps}
    matched: set[tuple[int, int, int]] = set()
    for offset in range(0, len(entries) - stride + 1, stride):
        here = (entries[offset], entries[offset + 1], entries[offset + 2])
        best, best_distance = None, None
        for source, target in targets.items():
            distance = sum((a - b) ** 2 for a, b in zip(here, source))
            if distance <= RASTER_TOLERANCE ** 2 and (
                    best_distance is None or distance < best_distance):
                best, best_distance = (source, target), distance
        if best is None:
            continue
        source, target = best
        matched.add(source)
        for channel in range(3):
            entries[offset + channel] = max(0, min(255, (
                here[channel] + target[channel] - source[channel])))
    if not matched:
        return 0
    img.putpalette(entries, rawmode=raw_mode)
    return len(matched)


def _shift_pixels(Image, img, swaps: tuple[tuple[str, str], ...], np):
    """Delta-shift every pixel near a source colour. ``(image, hits)``.

    Each pixel is claimed by whichever swap it is nearest to, so two swaps can
    never compound on one pixel — the same simultaneity the text branch gets
    from a single alternation.

    The work is kept proportional to the pixels that actually match, not to the
    image. A per-channel box test, short-circuited channel by channel, decides
    membership with integer comparisons; the distance, the weight and the shift
    are then computed over that subset alone. Most embedded images contain
    nothing near the mask colour, and this is what makes those nearly free.
    """
    arr = np.array(img)
    if arr.ndim != 3 or arr.shape[2] < 3:  # pragma: no cover — mode checked above
        return img, 0
    shape = arr.shape[:2]
    channels = [arr[..., index].astype(np.int16) for index in range(3)]
    opaque = arr[..., 3] > 0 if arr.shape[2] == 4 else None

    weight = None
    owner = None
    span = float(RASTER_SOFT_EDGE - RASTER_TOLERANCE)
    for index, (source, _target) in enumerate(swaps):
        source_rgb = _rgb(source)
        near = None
        for channel, level in zip(channels, source_rgb):
            close = np.abs(channel - level) <= RASTER_SOFT_EDGE
            near = close if near is None else (near & close)
            if not near.any():
                near = None
                break
        if near is None:
            continue
        if opaque is not None:
            near &= opaque
            if not near.any():
                continue

        squared = sum((channel[near].astype(np.int32) - level) ** 2
                      for channel, level in zip(channels, source_rgb))
        here = np.clip((RASTER_SOFT_EDGE - np.sqrt(squared)) / span, 0.0, 1.0)
        if weight is None:
            weight = np.zeros(shape, np.float32)
            owner = np.full(shape, -1, np.int16)
        improves = here > weight[near]
        if not improves.any():
            continue
        better = np.zeros(shape, bool)
        better[near] = improves
        weight[better] = here[improves]
        owner[better] = index

    if weight is None:
        return img, 0
    live = weight > 0
    if not live.any():
        return img, 0
    hits = len({int(i) for i in np.unique(owner[live]) if i >= 0})

    deltas = np.array(
        [[b - a for a, b in zip(_rgb(source), _rgb(target))]
         for source, target in swaps], np.int16)
    base = arr[..., :3][live].astype(np.int16)
    shift = deltas[owner[live]] * weight[live][:, None]
    arr[..., :3][live] = np.clip(base + shift, 0, 255).astype(arr.dtype)
    return Image.fromarray(arr, mode=img.mode), hits


# --- the module's public verbs ---------------------------------------------

def swap_text(text: str, swaps: tuple[tuple[str, str], ...]) -> tuple[str, int]:
    """Substitute colours in free text — a referenced SVG's source, say.

    Returns the rewritten text and the number of replacements, because a swap
    that matched nothing is not an error (a mask may live on one page and not
    another) but must not be silent either.
    """
    if not swaps:
        return text, 0
    sub = Substituter(swaps)
    return sub.sub(text), sub.hits


def swap_data_uri(value: str, swaps: tuple[tuple[str, str], ...]
                  ) -> tuple[str, int, list[str]]:
    """Recolour one ``data:`` URI. Non-URIs and non-matches come back as-is."""
    if not swaps:
        return value, 0, []
    sub = Substituter(swaps)
    return sub.sub_data_uri(value), sub.hits, sub.problems


def swap_bytes(raw: bytes, mime: str, swaps: tuple[tuple[str, str], ...]
               ) -> tuple[bytes, int, list[str]]:
    """Recolour bytes read off disk. Unmatched bytes come back byte-identical."""
    if not swaps:
        return raw, 0, []
    sub = Substituter(swaps)
    return sub.sub_bytes(raw, mime), sub.hits, sub.problems
