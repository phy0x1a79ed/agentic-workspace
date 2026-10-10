"""Verb effect and category declarations shared by the gateway and service manifests."""

from __future__ import annotations

from typing import Any

EFFECTS = ("read", "queue", "write", "secret")
DEFAULT_EFFECT = "write"


def check_effect(effect: str, *, where: str = "verb") -> str:
    if effect not in EFFECTS:
        raise ValueError(f"{where}: unknown effect {effect!r} (expected one of {EFFECTS})")
    return effect


def verb_effect(spec: dict[str, Any]) -> str:
    """The manifest function's declared effect; ``write`` when it declares none."""
    effect = spec.get("effect") or DEFAULT_EFFECT
    return check_effect(effect, where=f"function {spec.get('name')!r}")


def verb_category(spec: dict[str, Any]) -> str | None:
    """The manifest function's grant category, or ``None`` when it declares none."""
    return spec.get("category") or None
