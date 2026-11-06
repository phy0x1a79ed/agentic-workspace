"""The plug-in registry: which archive sources the running services declare.

A service joins the archive with one ``[[service.archive]]`` table per source
in its ``awm-plugin.toml``. The gateway lists those tables under ``archive`` in
each row of ``awm services``, and this module reads them there, so a service
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
CLASSES = ("content", "categorical", "numeric", "datetime")
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,31}")
_READY = "ready"


@dataclass(frozen=True)
class Field:
    """One declared field. ``nested`` is the separator under which a categorical value covers the values below
    it, such as ``/`` for a scope; ``''`` for none."""

    name: str
    cls: str
    nested: str = ""


def parse_fields(table: dict[str, Any]) -> tuple[dict[str, Field], tuple[str, ...]]:
    """The ``fields`` and ``hit`` of an archive table. Raises ``ValueError`` naming the first mistake."""
    raw = table.get("fields") or {}
    if not isinstance(raw, dict):
        raise ValueError("`fields` must be a table of name = class")
    fields: dict[str, Field] = {}
    for name, spec in raw.items():
        spec = {"class": spec} if isinstance(spec, str) else spec
        if not isinstance(spec, dict):
            raise ValueError(f"field {name!r} must be a class name or a table with `class`")
        cls, nested = str(spec.get("class") or ""), str(spec.get("nested") or "")
        if not _FIELD.fullmatch(name) or name == "id":
            raise ValueError(f"field name {name!r} must match {_FIELD.pattern} and not be `id`")
        if cls not in CLASSES:
            raise ValueError(f"field {name!r} has class {cls!r}, not one of {CLASSES}")
        if nested and cls != "categorical":
            raise ValueError(f"field {name!r}: only a categorical field nests")
        fields[name] = Field(name, cls, nested)
    hit = table.get("hit") or []
    if not isinstance(hit, list) or any(h not in fields for h in hit):
        raise ValueError(f"`hit` must list declared fields; it is {hit!r}")
    return fields, tuple(hit)


@dataclass(frozen=True)
class Source:
    """One plug-in. ``export``, ``query`` and ``get`` are verbs of ``tool``; a live source with no
    ``get`` holds nothing a caller can read whole.

    ``error`` is set when the table is unusable; the source is still listed, so
    the mistake shows in ``search sources`` instead of the source vanishing.
    """

    name: str
    service: str
    mode: str
    tool: str = ""
    export: str = ""
    query: str = ""
    get: str = ""
    topic: str = ""
    optin: bool = False
    error: str = ""
    table: dict[str, Any] = field(default_factory=dict)

    def spec(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fields(self) -> dict[str, Field]:
        try:
            return parse_fields(self.table)[0]
        except ValueError:
            return {}

    @property
    def hit(self) -> tuple[str, ...]:
        try:
            return parse_fields(self.table)[1]
        except ValueError:
            return ()


def _published(tools: list[str], named: str) -> str:
    """The published ``<tool>_<verb>`` a table names, or ``''`` when no tool of the service fits.

    A published name starts with one of the service's ``tools``; the longest
    match wins, so a tool whose name holds ``_`` still splits right. A bare verb
    resolves against a service that publishes one tool.
    """
    fits = [t for t in tools if named.startswith(f"{t}_") and len(named) > len(t) + 1]
    if fits:
        return named
    if len(tools) == 1 and "_" not in named:
        return f"{tools[0]}_{named}"
    return ""


def _split(published: str, tools: list[str]) -> tuple[str, str]:
    tool = max((t for t in tools if published.startswith(f"{t}_")), key=len)
    return tool, published[len(tool) + 1:]


def _source(service: str, table: dict[str, Any], tools: list[str]) -> Source:
    name, mode = str(table.get("name") or ""), str(table.get("mode") or "")
    base = dict(name=name, service=service, mode=mode, topic=str(table.get("topic") or ""),
                optin=bool(table.get("optin")), table=table)
    if not _NAME.fullmatch(name):
        return Source(**base, error=f"source name {name!r} must match {_NAME.pattern}")
    if mode not in MODES:
        return Source(**base, error=f"mode {mode!r} is not one of {MODES}")
    try:
        fields, _ = parse_fields(table)
    except ValueError as exc:
        return Source(**base, error=str(exc))
    if mode == "index" and not any(f.cls == "content" for f in fields.values()):
        return Source(**base, error="an index source declares at least one content field")
    role = "export" if mode == "index" else "query"
    named = str(table.get(role) or "")
    published = _published(tools, named) if named else ""
    if not published:
        reason = f"names {named!r}, which the service does not publish" if named else "names none"
        return Source(**base, error=f"a {mode} source needs a `{role}` verb, and the table {reason}")
    tool, verb = _split(published, tools)
    reader = str(table.get("get") or "") if mode == "live" else ""
    get = _published([tool], reader) if reader else ""
    if reader and not get:
        return Source(**base, error=f"the table's `get` names {reader!r}, which the service does not publish under {tool!r}")
    return Source(**base, tool=tool, **{role: verb}, get=get[len(tool) + 1:] if get else "")


def services_dir() -> Path:
    return Path(os.environ.get("AWM_SERVICES_DIR") or Path(__file__).resolve().parents[3])


def manifest_tables(service: str) -> list[dict[str, Any]]:
    """The archive tables in the service folder's ``awm-plugin.toml``, for :func:`declared`."""
    try:
        plugin = tomllib.loads((services_dir() / service / "awm-plugin.toml").read_text())
    except (OSError, ValueError):
        return []
    entries = [s for s in plugin.get("service") or [] if isinstance(s, dict)]
    entry = next((s for s in entries if s.get("name") == service), entries[0] if len(entries) == 1 else {})
    return [t for t in entry.get("archive") or [] if isinstance(t, dict)]


def declared() -> dict[str, tuple[dict[str, Field], tuple[str, ...]]]:
    """``{kind: (fields, hit)}`` from every service folder's manifest on disk, running or not, so a query can
    name a kind that only another node serves. A malformed table is left out."""
    out: dict[str, tuple[dict[str, Field], tuple[str, ...]]] = {}
    try:
        folders = sorted(p for p in services_dir().iterdir() if (p / "awm-plugin.toml").is_file())
    except OSError:
        return out
    for folder in folders:
        for table in manifest_tables(folder.name):
            name = str(table.get("name") or "")
            if name and name not in out:
                try:
                    out[name] = parse_fields(table)
                except ValueError as exc:
                    log.warning("archive table %r of %r: %s", name, folder.name, exc)
    return out


def parse(entries: list[dict[str, Any]]) -> tuple[dict[str, Source], set[str]]:
    """``({name: Source}, running services)`` from the rows of ``awm services``.

    Only a service that is ``ready`` counts as running. Two services declaring
    one source name keep the first; the second is logged and left out.
    """
    sources: dict[str, Source] = {}
    running: set[str] = set()
    for entry in entries:
        if entry.get("status") != _READY:
            continue
        service = str(entry.get("name") or "")
        running.add(service)
        tables = entry.get("archive") or []
        tools = [str(t) for t in entry.get("tools") or [] if t] or [service]
        for table in tables:
            source = _source(service, table, tools)
            if source.name in sources:
                log.warning("archive source %r declared by %r is already held by %r",
                            source.name, service, sources[source.name].service)
                continue
            sources[source.name] = source
    return sources, running
