"""Timeouts and failure taxonomy shared by both MCP stdio proxies.

Two proxies speak the same MCP surface to the same core — :mod:`mcp_stdio`
(default) and :mod:`mcp_server_sdk` (the ``AWM_MCP_SDK=1`` rollback, which
:mod:`mcp_server` selects without a redeploy). This module holds what they must
agree on, for the same reason :mod:`peer_files` is its own module: a rollback
that behaves differently from the thing it rolls back is not a rollback. They
had already drifted — the SDK proxy left its read timeout out of the retry
classification, so the failure below reached callers there with an *empty*
message while the default proxy mislabelled it.

Stdlib only, and deliberately so: :mod:`mcp_stdio` exists to keep imports off
the launch path, and an MCP client can use no tool until ``initialize`` returns.

**The ladder.** Four numbers answer four different questions, and collapsing
any two of them is how this went wrong:

===========================  =========================================
manifest per-fn ``timeout``  how long may this verb legitimately run?
                             (authoritative; enforced server-side)
``read_timeout()``           how long before we stop believing a reply
                             is coming? (a backstop, never a budget)
``catalog_read_timeout()``   the same, for a call that blocks session
                             startup and is answered in ~19ms
``RECONNECT_WINDOW``         is the daemon coming back up?
===========================  =========================================

**Reconnect window ≠ read timeout.** The window is only meaningful *before* the
request lands: on loopback a connect either succeeds or is refused at once, so
it never hangs. Applying it to a reply already owed is the category error that
made a delivered ``scope create`` report an unreachable daemon after "10.0s"
it had in fact waited 60 for. Retry the connect phase; never the response phase
— ``/invoke`` is not idempotent, and by then the core is already running it.
"""

from __future__ import annotations

import os

# The reconnect window, applied to the CONNECT phase only, so a request
# transparently survives a `systemctl restart` of the core.
RECONNECT_WINDOW = 10.0

# Above the largest per-function timeout any service manifest declares (3600s,
# the dvc service's `wait` verb), plus headroom, so the client ceiling can never
# fire before the server-side budget it is backstopping. Tests pin this.
DEFAULT_READ_TIMEOUT = 3900.0

# The catalog fetch blocks session startup and the core answers it in ~19ms, so
# it gets its own, short ceiling: a 65-minute hang there would look like a dead
# client with no diagnostic.
DEFAULT_CATALOG_READ_TIMEOUT = 30.0

_READ_ENV = "AWM_MCP_READ_TIMEOUT"
_CATALOG_ENV = "AWM_MCP_CATALOG_READ_TIMEOUT"


def _env_float(name: str, default: float) -> float:
    """Read a timeout override at call time, never at import.

    ``main()`` calls ``config.load_env_file()`` *after* this module is imported,
    so a module-level constant would miss ``$AWM_WORKSPACE/.awm/env``. The same
    reason ``AWM_AS`` is read per call.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def read_timeout() -> float:
    """Ceiling for a reply to ``POST /invoke``."""
    return _env_float(_READ_ENV, DEFAULT_READ_TIMEOUT)


def catalog_read_timeout() -> float:
    """Ceiling for a reply to ``GET /tools``."""
    return _env_float(_CATALOG_ENV, DEFAULT_CATALOG_READ_TIMEOUT)


class CoreUnreachable(RuntimeError):
    """The request never landed: the core refused or dropped the connection."""


class CoreNoReply(RuntimeError):
    """The core accepted the request and sent no reply in time.

    Emphatically *not* a failure report. The gateway hands the call to the
    owning service before it awaits anything, and nothing on that path learns
    the HTTP client left — so the operation is still running, or already done.
    """

    def __init__(self, waited_s: float, path: str):
        super().__init__(f"no reply from the awm core within {waited_s:.0f}s")
        self.waited_s = waited_s
        self.path = path


def no_reply_envelope(tool: str, verb: str | None, waited_s: float) -> dict:
    """The payload both proxies return for a :class:`CoreNoReply`.

    Says three things in order, because a model that reads only the first
    sentence must still not retry: delivered, probably succeeded, re-check.
    """
    called = f"{tool}(verb={verb!r})" if verb else tool
    hint = f"{tool}(verb='search') / {tool}(verb='resolve')" if tool else "a read verb"
    return {
        "error_class": "CoreNoReply",
        "tool": tool,
        "verb": verb,
        "waited_s": round(waited_s, 1),
        "error": (
            f"The awm core accepted {called} and sent no reply within "
            f"{waited_s:.0f}s. This is a client-side read timeout, NOT a failure "
            f"report: the request was delivered and the operation is most likely "
            f"still running or already finished on the server. Do NOT re-issue it "
            f"— create/write verbs are not idempotent. Check the current state "
            f"with a read verb on the same domain ({hint}), and see .awm/awm.log, "
            f"before doing anything else."
        ),
    }


def catalog_no_reply_message(waited_s: float) -> str:
    """Message for a catalog fetch that went unanswered. Safe to retry by hand."""
    return (f"awm core did not answer GET /tools within {waited_s:.0f}s "
            f"(the core is up — it accepted the request — but did not reply)")
