"""Opt-in per-service config contracts.

A service may publish a JSON-Schema of its user-settable settings (for the
``config`` block of its ``ready.api`` manifest) while storing the actual
values **locally** in its own SQLite DB. The gateway aggregator folds every
opted-in service's contract + live values into one settings surface; this
module is the service-side half a service splices into its adapter in a few
lines.

Opt-in is the default-off: a service that never builds a :class:`ConfigContract`
ships no ``config`` block and never appears on the settings page.

Storage is a single JSON blob per contract, in a dedicated
``awm_settings(key, value, updated_at)`` table created lazily on first use.
There is deliberately **no** ``init_service_db`` schema-version coupling, so the
contract drops into any service untouched — and the distinct table name never
collides with a service that already owns a ``config`` table (e.g. tts presets).

The settings *shape* is a Pydantic v2 model the caller passes in. This module
stays pydantic-free: it only calls ``model_json_schema`` / ``model_validate`` /
``model_dump`` on the passed-in class, keeping ``persistence`` a leaf with no
new dependency.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from awm.persistence.databases import get_connection

# One reserved row holds the whole settings blob for a contract.
_DEFAULT_KEY = "service_config"
_TABLE = "awm_settings"


def _ensure_table(conn: Any) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )


class ConfigContract:
    """A service's opt-in config contract: schema published, values stored local.

    ``model`` is a Pydantic v2 model class describing the settable fields and
    their defaults. The contract reads/writes a single JSON blob keyed by
    ``key`` in ``service``'s own DB (``get_connection(service)``). Validation
    happens on every ``save`` so a bad value is rejected at the service, not the
    UI.
    """

    def __init__(
        self,
        service: str,
        model: type,
        *,
        title: str | None = None,
        key: str = _DEFAULT_KEY,
    ) -> None:
        self.service = service
        self.model = model
        self.title = title or service
        self.key = key

    # -- schema ----------------------------------------------------------

    def json_schema(self) -> dict[str, Any]:
        """The JSON-Schema for the settable fields (for manifest + UI form)."""
        return self.model.model_json_schema()

    def manifest_fragment(self) -> dict[str, Any]:
        """The ``config`` block for the service's API_MANIFEST.

        Its mere presence is the opt-in marker the gateway aggregator keys off;
        it also carries the title + schema so discovery needs no extra RPC.
        """
        return {"title": self.title, "schema": self.json_schema()}

    # -- storage ---------------------------------------------------------

    def _read_raw(self) -> dict[str, Any]:
        conn = get_connection(self.service)
        try:
            _ensure_table(conn)
            row = conn.execute(
                f"SELECT value FROM {_TABLE} WHERE key = ?", (self.key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {}
        try:
            data = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_raw(self, values: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(self.service)
        try:
            _ensure_table(conn)
            conn.execute(
                f"INSERT INTO {_TABLE} (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (self.key, json.dumps(values), now),
            )
            conn.commit()
        finally:
            conn.close()

    # -- typed access ----------------------------------------------------

    def load(self) -> dict[str, Any]:
        """Defaults merged with stored overrides, validated → plain dict.

        Cheap (a single-row read) so callers can resolve config per use without
        caching — a saved change takes effect on the next read with no restart.
        """
        return self.model.model_validate(self._read_raw()).model_dump(mode="json")

    def load_model(self) -> Any:
        """Same as :meth:`load` but returns the validated model instance."""
        return self.model.model_validate(self._read_raw())

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate ``patch`` (merged over stored), persist, return saved values.

        A full-object upsert: the merged object is validated as a whole, so a
        rejected value never reaches the DB.
        """
        merged = {**self._read_raw(), **(patch or {})}
        values = self.model.model_validate(merged).model_dump(mode="json")
        self._write_raw(values)
        return values

    # -- gateway-facing handlers ----------------------------------------

    def get(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """``config_get`` handler: self-describing contract + live values."""
        return {
            "title": self.title,
            "schema": self.json_schema(),
            "values": self.load(),
        }

    def set(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """``config_set`` handler: persist ``args['values']``, echo the contract."""
        patch = (args or {}).get("values") or {}
        saved = self.save(patch)
        return {"title": self.title, "schema": self.json_schema(), "values": saved}

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        """The ``config_get``/``config_set`` callables to splice into HANDLERS."""
        return {"config_get": self.get, "config_set": self.set}
