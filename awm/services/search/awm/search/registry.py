"""The plug-in registry: which archive sources the running services declare.

A service joins the archive with one ``[[archive]]`` table per source in its
``service.toml``. The gateway lists those tables under ``archive`` in each
``awm registry_list`` entry, and this module reads them there, so a service
that appears needs no change here.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("awm.search.registry")

MODES = ("index", "live")
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_READY = "ready"


@dataclass(frozen=True)
class Source:
    """One plug-in. ``export`` and ``query`` are the service-internal function names.

    ``error`` is set when the table is unusable; the source is still listed, so
    the mistake shows in ``search sources`` instead of the source vanishing.
    """

    name: str
    service: str
    mode: str
    export: str = ""
    query: str = ""
    topic: str = ""
    optin: bool = False
    error: str = ""
    table: dict[str, Any] = field(default_factory=dict)

    def spec(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "Source":
        return cls(**{k: v for k, v in spec.items() if k in cls.__dataclass_fields__})


def _internal(api: Any, service: str, published: str) -> str:
    """The internal function name behind a published verb name, or ``''``."""
    functions = [fn for fn in (api or {}).get("functions") or [] if isinstance(fn, dict) and fn.get("name")]
    for fn in functions:
        if (fn.get("tool") or f"{service}_{fn['name']}") == published:
            return fn["name"]
    return next((fn["name"] for fn in functions if fn["name"] == published), "")


def _source(service: str, table: dict[str, Any], api: Any) -> Source:
    name, mode = str(table.get("name") or ""), str(table.get("mode") or "")
    base = dict(name=name, service=service, mode=mode, topic=str(table.get("topic") or ""),
                optin=bool(table.get("optin")), table=table)
    if not _NAME.fullmatch(name):
        return Source(**base, error=f"source name {name!r} must match {_NAME.pattern}")
    if mode not in MODES:
        return Source(**base, error=f"mode {mode!r} is not one of {MODES}")
    verb = "export" if mode == "index" else "query"
    published = str(table.get(verb) or "")
    internal = _internal(api, service, published) if published else ""
    if not internal:
        reason = f"names {published!r}, which the service does not publish" if published else "names none"
        return Source(**base, error=f"a {mode} source needs a `{verb}` verb, and the table {reason}")
    return Source(**base, **{verb: internal})


def services_dir() -> Path:
    return Path(os.environ.get("AWM_SERVICES_DIR") or Path(__file__).resolve().parents[3])


def manifest_tables(service: str) -> list[dict[str, Any]]:
    """The ``[[archive]]`` tables in the service folder's ``service.toml``.

    The fallback for a gateway whose registry entries carry no ``archive`` key.
    """
    try:
        data = tomllib.loads((services_dir() / service / "service.toml").read_text())
    except (OSError, ValueError):
        return []
    return [t for t in data.get("archive") or [] if isinstance(t, dict)]


def parse(entries: list[dict[str, Any]]) -> tuple[dict[str, Source], set[str]]:
    """``({name: Source}, running services)`` from the entries of ``awm registry_list``.

    Only a service that is ``ready`` counts as running. Two services declaring
    one source name keep the first; the second is logged and left out.
    """
    sources: dict[str, Source] = {}
    running: set[str] = set()
    for entry in entries:
        if entry.get("kind") != "service" or entry.get("backend_status") != _READY:
            continue
        service = str(entry.get("name") or "")
        running.add(service)
        tables = entry.get("archive")
        if tables is None:
            tables = manifest_tables(service)
        for table in tables:
            source = _source(service, table, entry.get("api"))
            if source.name in sources:
                log.warning("archive source %r declared by %r is already held by %r",
                            source.name, service, sources[source.name].service)
                continue
            sources[source.name] = source
    return sources, running
