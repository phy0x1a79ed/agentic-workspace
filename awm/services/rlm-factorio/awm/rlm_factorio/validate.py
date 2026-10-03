"""Argument checks at the realm boundary, before anything reaches the engine.

Numbers may arrive as strings (shell callers send ``.5`` and ``"12"``), so a
numeric string is accepted and converted, which keeps the mod's own coercion
and every existing script working. Everything else that is out of type or range
is refused with a message naming the argument.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

COORD_MAX = 1_000_000          # the engine's map edge, in tiles
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")


class ArgError(ValueError):
    pass


def _num(key: str, v: Any) -> float:
    if isinstance(v, bool):
        raise ArgError(f"argument {key} must be a number, got a boolean")
    if isinstance(v, (int, float)):
        n = float(v)
    elif isinstance(v, str):
        try:
            n = float(v.strip())
        except ValueError:
            raise ArgError(f"argument {key} must be a number, got {v!r}") from None
    else:
        raise ArgError(f"argument {key} must be a number, got {type(v).__name__}")
    if not math.isfinite(n):
        raise ArgError(f"argument {key} must be finite, got {v!r}")
    return n


def number(lo: float, hi: float) -> Callable[[str, Any], float]:
    def check(key: str, v: Any) -> float:
        n = _num(key, v)
        if not lo <= n <= hi:
            raise ArgError(f"argument {key} must be within [{lo:g}, {hi:g}], got {n:g}")
        return n
    return check


def integer(lo: int, hi: int) -> Callable[[str, Any], int]:
    def check(key: str, v: Any) -> int:
        n = _num(key, v)
        if n != int(n):
            raise ArgError(f"argument {key} must be a whole number, got {n:g}")
        if not lo <= n <= hi:
            raise ArgError(f"argument {key} must be within [{lo}, {hi}], got {n:g}")
        return int(n)
    return check


def name(key: str, v: Any) -> str:
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise ArgError(f"argument {key} must be a prototype name "
                       f"(letters, digits, '-', '_'), got {v!r}")
    return v


def text(max_len: int) -> Callable[[str, Any], str]:
    def check(key: str, v: Any) -> str:
        if not isinstance(v, str):
            raise ArgError(f"argument {key} must be a string, got {type(v).__name__}")
        if len(v) > max_len:
            raise ArgError(f"argument {key} is {len(v)} characters, over {max_len}")
        return v
    return check


def boolean(key: str, v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0"):
        return v.strip().lower() in ("true", "1")
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise ArgError(f"argument {key} must be true or false, got {v!r}")


def one_of(*choices: str) -> Callable[[str, Any], str]:
    def check(key: str, v: Any) -> str:
        if v not in choices:
            raise ArgError(f"argument {key} must be one of {', '.join(choices)}, got {v!r}")
        return v
    return check


def names(key: str, v: Any) -> list[str]:
    """A prototype name, or a list of them."""
    items = v if isinstance(v, list) else [v]
    if not items or len(items) > 50:
        raise ArgError(f"argument {key} takes 1-50 names")
    return [name(key, item) for item in items]


COORD = number(-COORD_MAX, COORD_MAX)
COUNT = integer(1, 100_000)
DIRECTION = one_of(
    "north", "northnortheast", "northeast", "eastnortheast",
    "east", "eastsoutheast", "southeast", "southsoutheast",
    "south", "southsouthwest", "southwest", "westsouthwest",
    "west", "westnorthwest", "northwest", "northnorthwest")
SURFACE = name
PATH = text(4096)

SPECS: dict[str, dict[str, Callable[[str, Any], Any]]] = {
    "observe": {"radius": number(1, 64), "screenshot": boolean},
    "screenshot": {"x": COORD, "y": COORD, "width": integer(16, 4096),
                   "height": integer(16, 4096), "zoom": number(0.03125, 8),
                   "daytime": number(0, 1), "show_entity_info": boolean,
                   "surface": SURFACE, "file": text(200)},
    "recipes": {"search": text(100), "limit": integer(1, 500)},
    "technologies": {"search": text(100), "only_unresearched": boolean,
                     "limit": integer(1, 500)},
    "move": {"x": COORD, "y": COORD},
    "teleport": {"x": COORD, "y": COORD, "surface": SURFACE},
    "mine": {"x": COORD, "y": COORD, "name": name, "count": COUNT},
    "craft": {"recipe": name, "count": integer(1, 1000)},
    "build": {"name": name, "x": COORD, "y": COORD, "direction": DIRECTION},
    "insert": {"x": COORD, "y": COORD, "name": name, "count": COUNT, "target": name},
    "take": {"x": COORD, "y": COORD, "name": name, "count": COUNT, "target": name},
    "research": {"name": name},
    "exec_lua": {"code": text(64 * 1024), "path": PATH,
                 "max_output": integer(256, 1_048_576), "budget_ms": number(1, 1000)},
    "blueprint_stamp": {"x": COORD, "y": COORD, "blueprint": text(4 * 1024 * 1024),
                        "path": PATH, "direction": DIRECTION, "build": boolean,
                        "clear": boolean, "force_build": boolean, "surface": SURFACE,
                        "budget_ms": number(1, 1000)},
    "blueprint_capture": {"radius": number(1, 200), "x": COORD, "y": COORD,
                          "x1": COORD, "y1": COORD, "x2": COORD, "y2": COORD,
                          "label": text(200), "include_tiles": boolean,
                          "save_as": PATH, "surface": SURFACE,
                          "budget_ms": number(1, 1000)},
    "scan": {"x": COORD, "y": COORD, "radius": number(1, 4096),
             "x1": COORD, "y1": COORD, "x2": COORD, "y2": COORD,
             "surface": SURFACE, "name": names, "type": names, "force": name,
             "fields": names, "limit": integer(1, 5000), "cursor": text(200),
             "count_only": boolean},
    "world_save": {"name": text(100), "overwrite": boolean},
    "world_load": {"name": text(100)},
    "world_new": {"seed": integer(0, 2**32 - 1)},
    "pause": {"paused": boolean},
}


def check(verb: str, args: dict) -> dict:
    """Return ``args`` with every known argument checked and converted."""
    if not isinstance(args, dict):
        raise ArgError("arguments must be an object")
    spec = SPECS.get(verb)
    if not spec:
        return args
    out = dict(args)
    for key, validate in spec.items():
        if out.get(key) is not None:
            out[key] = validate(key, out[key])
    return out
