"""Per-user roots — the one place a service asks "whose data is this?".

An edge-verified caller arrives as ``X-Awm-As: user:<name>``. A service that
partitions its store by user resolves that here to a directory under the
``userdata`` project (``projects/userdata/<name>/``, one scope worktree per
user) and keeps its own indexes under ``SERVICES_DIR/<service>/users/<name>/``.

Two modes, chosen by the host:

* default — an identity that is not a known user (an agent placement slug,
  ``operator``, a peer) resolves to ``None``, and the service falls back to
  its single legacy store. This is the dev box: agents and MCP callers keep
  working untouched.
* ``AWM_USER_ROOT_STRICT=1`` — the same case is a ``PermissionError``. This is
  the public host, where nothing but a signed-in user may touch a store.

``AWM_USER_ROOT_TEMPLATE`` overrides the root location (``{user}`` is
substituted); the directory must already exist — creating users is the admin
script's job, not a side effect of a request.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator

import awm.config as _config

USER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AS_PREFIX = "user:"
TEMPLATE_ENV = "AWM_USER_ROOT_TEMPLATE"
STRICT_ENV = "AWM_USER_ROOT_STRICT"

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "awm_user", default=None)


class UnknownUser(LookupError):
    """The name is well-formed but has no root directory."""


def user_of(as_: str | None) -> str | None:
    """``"user:<name>"`` → ``"<name>"`` when the name is well-formed, else None."""
    if not as_ or not as_.startswith(AS_PREFIX):
        return None
    name = as_[len(AS_PREFIX):]
    return name if USER_RE.match(name) else None


def _template() -> str:
    return os.environ.get(TEMPLATE_ENV) or str(
        _config.WORKSPACE_ROOT / "projects" / "userdata" / "{user}")


def root_for(user: str) -> Path:
    """The user's worktree root. Raises ``UnknownUser`` when it does not exist."""
    if not USER_RE.match(user or ""):
        raise UnknownUser(user)
    root = Path(_template().format(user=user))
    if not root.is_dir():
        raise UnknownUser(user)
    return root


def users() -> list[str]:
    """Every user with an existing root, from the template's parent directory."""
    tpl = _template()
    head, _, tail = tpl.partition("{user}")
    parent = Path(head)
    if not parent.is_dir():
        return []
    out = []
    for entry in sorted(parent.iterdir()):
        if USER_RE.match(entry.name) and Path(tpl.format(user=entry.name)).is_dir():
            out.append(entry.name)
    return out


def state_dir(service: str, user: str) -> Path:
    """Where ``service`` keeps its per-user index/state (not the user's data)."""
    d = _config.SERVICES_DIR / service / "users" / user
    d.mkdir(parents=True, exist_ok=True)
    return d


def strict() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() in ("1", "true", "yes")


def resolve(as_: str | None) -> str | None:
    """The user behind ``as_``, or ``None`` for "use the legacy store".

    Strict mode turns that ``None`` into a ``PermissionError``.
    """
    user = user_of(as_)
    if user is not None:
        try:
            root_for(user)
            return user
        except UnknownUser:
            user = None
    if strict():
        raise PermissionError("no user account behind %r" % (as_,))
    return None


def current() -> str | None:
    """The user bound to the current context, if any."""
    return _current.get()


@contextlib.contextmanager
def bind(user: str | None) -> Iterator[str | None]:
    """Bind ``user`` (already resolved) for the duration of the block."""
    token = _current.set(user)
    try:
        yield user
    finally:
        _current.reset(token)


def wrap_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Run a gateway verb handler with the caller's user bound.

    The gateway threads the edge-verified ``X-Awm-As`` as ``as_``; a known
    user binds their store for the call, anything else means the legacy store
    (or, in strict mode, a refusal). The binding is a ContextVar, so it follows
    the handler into ``asyncio.to_thread``. The wrapper always takes two
    positional parameters, which is how the adapter learns to pass ``as_``.
    """
    try:
        two = len(inspect.signature(handler).parameters) >= 2
    except (TypeError, ValueError):
        two = False

    if inspect.iscoroutinefunction(handler):
        async def _async(args: dict, as_: str | None = None):
            with bind(resolve(as_)):
                return await (handler(args, as_) if two else handler(args))
        _async.__name__ = getattr(handler, "__name__", "handler")
        return _async

    def _sync(args: dict, as_: str | None = None):
        with bind(resolve(as_)):
            return handler(args, as_) if two else handler(args)
    _sync.__name__ = getattr(handler, "__name__", "handler")
    return _sync


def wrap_handlers(handlers: dict[str, Callable[..., Any]]) -> dict[str, Callable[..., Any]]:
    return {name: wrap_handler(h) for name, h in handlers.items()}
